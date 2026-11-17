#!/usr/bin/env python3
"""
STH Prices Server v3.0
Servidor Flask para extraer precios de metales desde Transamine
Puerto: 8080

CORREGIDO: Extrae datos de spans, no de tablas HTML
"""

import os
import math
import time
import tempfile
import re
import json
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

import requests
from bs4 import BeautifulSoup

import pandas as pd
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill

app = Flask(__name__)
CORS(app)

TRANSAMINE_URL = "https://www.transamine.com/price-and-review.html"
REQUEST_TIMEOUT = 30
PAUSE_BETWEEN_MONTHS = 1

# Feeds JSON públicos de LBMA (fuente de RESPALDO para Au/Ag).
# Solo se consultan cuando Transamine devuelve nulls en oro/plata.
# Cada registro: {"d": "YYYY-MM-DD", "v": [USD, GBP, EUR]}
LBMA_FEEDS = {
    'oro_am': 'https://prices.lbma.org.uk/json/gold_am.json',
    'oro_pm': 'https://prices.lbma.org.uk/json/gold_pm.json',
    'plata': 'https://prices.lbma.org.uk/json/silver.json',
}
LBMA_TIMEOUT = 30

# Respaldos SECUNDARIOS para preciosos, usados solo cuando LBMA no responde
# (p. ej. desde la red de Render, donde prices.lbma.org.uk no contesta):
#  - SGM (Servicio Geologico Mexicano, gob.mx): tabla acumulada del anio en
#    curso. Oro = LBMA PM fix, Plata = LBMA silver fix. Validado 11-sep-2026
#    contra el feed LBMA: plata identica, oro 4 typos aislados en 175 dias.
#  - Westmetall: tabla "Gold London Fixing" (USD/ozt) = LBMA AM fix.
#    Validado 11-sep-2026: 3 typos aislados en 174 dias.
# Ambos solo rellenan lo que Transamine y LBMA dejaron en null.
SGM_URL = "https://www.sgm.gob.mx/EconomiaMinera/PreciosAcumulados?opcion={opcion}"
SGM_OPCIONES = {
    'oro_pm': 'acumuladoOro',
    'plata': 'acumuladoPlata',
}
SGM_TIMEOUT = 30
MESES_ES = {'ene': 1, 'feb': 2, 'mar': 3, 'abr': 4, 'may': 5, 'jun': 6,
            'jul': 7, 'ago': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dic': 12}
MESES_ES_LARGO = {'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5,
                  'junio': 6, 'julio': 7, 'agosto': 8, 'septiembre': 9,
                  'octubre': 10, 'noviembre': 11, 'diciembre': 12}
WESTMETALL_GOLD_AM_FIELD = 'USD_ozt_London'

# Tablas publicas de Westmetall (fuente de RESPALDO para metales BASE).
# Republican el LME Cash-Settlement oficial diario / validado identico a
# Transamine (Pb 10-jul-2026: 1851 en ambos). Cubren el anio en curso.
# Solo se consultan cuando Transamine deja nulls en metales base.
WESTMETALL_URL = "https://www.westmetall.com/en/markdaten.php?action=table&field={field}"
WESTMETALL_FIELDS = {
    'cobre': 'LME_Cu_cash',
    'plomo': 'LME_Pb_cash',
    'zinc': 'LME_Zn_cash',
    'niquel': 'LME_Ni_cash',
    'estano': 'LME_Sn_cash',
}
WESTMETALL_TIMEOUT = 30
MONTHS_EN = {'January': 1, 'February': 2, 'March': 3, 'April': 4, 'May': 5,
             'June': 6, 'July': 7, 'August': 8, 'September': 9,
             'October': 10, 'November': 11, 'December': 12}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Connection': 'keep-alive',
}

# Para los feeds JSON de LBMA (detras de Cloudflare) se piden como JSON
LBMA_HEADERS = dict(HEADERS, **{
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://www.lbma.org.uk/',
})


def clean_value(value):
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
    if isinstance(value, str):
        value = value.strip()
        if value.lower() in ['nan', 'none', 'null', 'n/a', '-', '']:
            return None
    return value


def clean_dict(d):
    if isinstance(d, dict):
        return {k: clean_dict(v) for k, v in d.items()}
    elif isinstance(d, list):
        return [clean_dict(item) for item in d]
    else:
        return clean_value(d)


def parse_price(price_str):
    if not price_str:
        return None
    try:
        cleaned = price_str.replace('$', '').replace(',', '').replace(' ', '').strip()
        if cleaned.lower() in ['n/a', '-', '', 'none', 'nan']:
            return None
        return float(cleaned)
    except (ValueError, AttributeError):
        return None


def get_months_in_range(start_date, end_date):
    months = []
    current = start_date.replace(day=1)
    while current <= end_date:
        months.append(current.strftime('%Y-%m'))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months


def extract_month_prices(session, year_month):
    """
    Extrae precios de un mes desde Transamine (desde v4.0 solo ULTIMO
    RECURSO: Transamine dejo de actualizar preciosos en jul-2026).
    La página usa spans con clases text_price, no tablas HTML.
    """
    url = f"{TRANSAMINE_URL}?choix_date={year_month}"
    month_data = {}
    averages = {}
    
    try:
        response = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'lxml')
        
        # Buscar el contenedor principal
        table_listing = soup.find('div', id='table_listing')
        
        if not table_listing:
            print(f"  No se encontró table_listing para {year_month}")
            return month_data, averages
        
        # Encontrar todas las secciones (h4 seguido de div)
        current_metal = None
        
        # Iterar por los elementos hijos
        for element in table_listing.children:
            if element.name == 'h4':
                # Es un encabezado de metal
                metal_text = element.get_text().strip().lower()
                if 'copper' in metal_text:
                    current_metal = 'cobre'
                elif 'lead' in metal_text:
                    current_metal = 'plomo'
                elif 'zinc' in metal_text:
                    current_metal = 'zinc'
                elif 'nickel' in metal_text:
                    current_metal = 'niquel'
                elif 'gold' in metal_text:
                    current_metal = 'oro'
                elif 'silver' in metal_text:
                    current_metal = 'plata'
                elif 'tin' in metal_text:
                    current_metal = 'estano'
                elif 'cobalt' in metal_text:
                    current_metal = 'cobalto'
                else:
                    current_metal = None
                    
            elif element.name == 'div' and current_metal:
                # Es el contenedor de precios para el metal actual
                
                # Extraer promedio
                avg_span = element.find('span', class_='average_price')
                if avg_span:
                    avg_values = avg_span.find_all('span')
                    if current_metal == 'oro' and len(avg_values) >= 3:
                        # Oro tiene AM, MEAN, PM - tomamos PM (índice 2)
                        averages[f'{current_metal}_pm'] = parse_price(avg_values[2].get_text())
                        averages[f'{current_metal}_am'] = parse_price(avg_values[0].get_text())
                        averages[f'{current_metal}_mean'] = parse_price(avg_values[1].get_text())
                    elif avg_values:
                        averages[current_metal] = parse_price(avg_values[0].get_text())
                
                # Extraer precios diarios
                price_spans = element.find_all('span', class_='text_price')
                
                for price_span in price_spans:
                    strong = price_span.find('strong')
                    if not strong:
                        continue
                    
                    date_text = strong.get_text().strip()
                    
                    # Validar formato de fecha YYYY-MM-DD
                    if not re.match(r'\d{4}-\d{2}-\d{2}', date_text):
                        continue

                    # Descartar fechas de OTRO mes: cuando el feed de un metal
                    # esta roto, Transamine muestra el bloque del mes anterior
                    # (ej. Gold con fechas de junio en la pagina de julio) y esas
                    # filas parciales pisarian las filas completas del mes
                    # correcto en all_prices.update()
                    if not date_text.startswith(year_month):
                        continue
                    
                    # Inicializar fecha si no existe
                    if date_text not in month_data:
                        month_data[date_text] = {
                            'fecha': date_text,
                            'oro_pm': None,
                            'oro_am': None,
                            'plata': None,
                            'cobre': None,
                            'plomo': None,
                            'zinc': None,
                            'niquel': None,
                            'estano': None
                        }
                    
                    # Extraer valores
                    value_spans = price_span.find_all('span')
                    
                    if current_metal == 'oro' and len(value_spans) >= 3:
                        # Oro: AM, MEAN, PM
                        month_data[date_text]['oro_am'] = parse_price(value_spans[0].get_text())
                        month_data[date_text]['oro_pm'] = parse_price(value_spans[2].get_text())
                    elif value_spans:
                        price_value = parse_price(value_spans[0].get_text())
                        month_data[date_text][current_metal] = price_value
        
        print(f"  {year_month}: {len(month_data)} días extraídos")
        if averages:
            print(f"  Promedios: {averages}")
        
        return month_data, averages
        
    except requests.exceptions.Timeout:
        print(f"  Timeout para {year_month}")
        return month_data, averages
    except requests.exceptions.RequestException as e:
        print(f"  Error de conexión para {year_month}: {e}")
        return month_data, averages
    except Exception as e:
        print(f"  Error extrayendo precios para {year_month}: {e}")
        import traceback
        traceback.print_exc()
        return month_data, averages


def fetch_lbma_backup(start_str, end_str):
    """
    Descarga los fixes LBMA (Au AM/PM y Ag, en USD) para el rango de fechas.
    Devuelve dict {fecha: {'oro_am': x, 'oro_pm': y, 'plata': z}}.
    Es fuente de RESPALDO: solo se llama si Transamine trae nulls.
    Si un feed falla, se omite ese campo sin tumbar la respuesta.
    """
    backup = {}
    for field, url in LBMA_FEEDS.items():
        try:
            resp = requests.get(url, headers=LBMA_HEADERS, timeout=LBMA_TIMEOUT)
            resp.raise_for_status()
            for record in resp.json():
                d = record.get('d')
                if not d or d < start_str or d > end_str:
                    continue
                v = record.get('v') or []
                usd = v[0] if len(v) >= 1 else None
                if usd in (None, 0):
                    continue
                backup.setdefault(d, {})[field] = float(usd)
            print(f"  LBMA respaldo: {field} OK")
        except Exception as e:
            print(f"  LBMA respaldo: error en {field}: {e}")
    return backup


def _descartar_atipicos(valores, tolerancia=0.35, ventana=5, descartados=None):
    """
    Quita typos de transcripcion (ej. Westmetall 02-ene-2026: 1386.85 en vez
    de 4386.85): descarta valores que se alejen mas de `tolerancia` de la
    mediana de sus VECINOS (hasta `ventana` dias antes y despues).
    v4.1: antes comparaba contra la mediana de todo el rango y tiraba
    precios reales cuando el metal se movia fuerte en el anio (plata de
    ene-2026 a 99-118 USD/oz contra mediana anual de ~68).
    Devuelve un dict nuevo {fecha: valor}. Si se pasa la lista
    `descartados`, agrega ahi (fecha, valor, mediana_vecinos) de cada valor
    tirado, para avisarlo en la respuesta (campo "alertas").
    """
    fechas = sorted(f for f, v in valores.items() if v is not None)
    limpio = {}
    for i, fecha in enumerate(fechas):
        v = valores[fecha]
        vecinos = sorted(valores[x] for x in fechas[max(0, i - ventana):i + ventana + 1]
                         if x != fecha)
        if len(vecinos) >= 4:
            mediana = vecinos[len(vecinos) // 2]
            if abs(v - mediana) / mediana > tolerancia:
                print(f"  Respaldo secundario: descartado valor atipico {fecha}={v} (mediana vecinos {mediana})")
                if descartados is not None:
                    descartados.append((fecha, v, mediana))
                continue
        limpio[fecha] = v
    return limpio


def fetch_sgm_backup(start_str, end_str, descartes=None):
    """
    Descarga la tabla acumulada del anio en curso del SGM (gob.mx) para
    oro (= LBMA PM fix) y plata (= LBMA silver fix), en USD/oz.
    Devuelve {fecha: {'oro_pm': x, 'plata': y}}. Solo cubre el anio en curso
    (la pagina no tiene selector de anio). Si una tabla falla, se omite.
    """
    backup = {}
    for field, opcion in SGM_OPCIONES.items():
        try:
            url = SGM_URL.format(opcion=opcion)
            resp = requests.get(url, headers=HEADERS, timeout=SGM_TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'lxml')
            # Anio: viene en el hidden "fechaUltima" ("10 septiembre 2026")
            year = None
            hidden = soup.find('input', attrs={'name': 'fechaUltima'})
            texto = hidden.get('value', '') if hidden else soup.get_text(' ', strip=True)
            m = re.search(r'(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})', texto)
            if m and m.group(2).lower() in MESES_ES_LARGO:
                year = int(m.group(3))
            if year is None:
                raise ValueError('no se pudo determinar el anio de la tabla')
            valores = {}
            for tr in soup.find_all('tr'):
                cells = [td.get_text(' ', strip=True) for td in tr.find_all('td')]
                cells = [c for c in cells if c != '']
                if len(cells) != 2:
                    continue
                dm = re.match(r'^([a-z]{3})\s+(\d{1,2})$', cells[0].lower())
                if not dm or dm.group(1) not in MESES_ES:
                    continue
                fecha = "%04d-%02d-%02d" % (year, MESES_ES[dm.group(1)], int(dm.group(2)))
                value = parse_price(cells[1].replace('\xa0', ''))
                if value is None or value <= 0:
                    continue
                valores[fecha] = value
            # Atipicos sobre la tabla completa (vecinos reales) y luego el rango
            tirados = []
            valores = {f: v for f, v in _descartar_atipicos(valores, descartados=tirados).items()
                       if start_str <= f <= end_str}
            if descartes is not None:
                descartes.extend(
                    {'fecha': f, 'campo': field, 'valor': v, 'mediana_vecinos': med, 'fuente': 'sgm'}
                    for f, v, med in tirados if start_str <= f <= end_str)
            for fecha, v in valores.items():
                backup.setdefault(fecha, {})[field] = v
            print(f"  SGM respaldo: {field} OK ({len(valores)} fechas)")
        except Exception as e:
            print(f"  SGM respaldo: error en {field}: {e}")
    return backup


def fetch_westmetall_gold_am(start_str, end_str, descartes=None):
    """
    Descarga la tabla "Gold London Fixing" (USD/ozt) de Westmetall, que
    republica el LBMA AM fix de oro. Devuelve {fecha: {'oro_am': x}}.
    """
    backup = {}
    try:
        url = WESTMETALL_URL.format(field=WESTMETALL_GOLD_AM_FIELD)
        resp = requests.get(url, headers=HEADERS, timeout=WESTMETALL_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'lxml')
        valores = {}
        for tr in soup.find_all('tr'):
            cells = [td.get_text().strip() for td in tr.find_all('td')]
            if len(cells) < 2:
                continue
            m = re.match(r'(\d{1,2})\.\s+([A-Za-z]+)\s+(\d{4})', cells[0])
            if not m or m.group(2) not in MONTHS_EN:
                continue
            fecha = "%04d-%02d-%02d" % (
                int(m.group(3)), MONTHS_EN[m.group(2)], int(m.group(1)))
            value = parse_price(cells[1])
            if value is None or value <= 0:
                continue
            valores[fecha] = value
        # Atipicos sobre la tabla completa (vecinos reales) y luego el rango
        tirados = []
        valores = {f: v for f, v in _descartar_atipicos(valores, descartados=tirados).items()
                   if start_str <= f <= end_str}
        if descartes is not None:
            descartes.extend(
                {'fecha': f, 'campo': 'oro_am', 'valor': v, 'mediana_vecinos': med, 'fuente': 'westmetall'}
                for f, v, med in tirados if start_str <= f <= end_str)
        for fecha, v in valores.items():
            backup.setdefault(fecha, {})['oro_am'] = v
        print(f"  Westmetall respaldo: oro_am OK ({len(valores)} fechas)")
    except Exception as e:
        print(f"  Westmetall respaldo: error en oro_am: {e}")
    return backup


def fetch_westmetall_backup(start_str, end_str):
    """
    Descarga el LME Cash-Settlement diario desde las tablas publicas de
    Westmetall para los metales base. Devuelve {fecha: {metal: valor}}.
    Es fuente de RESPALDO: solo se llama si Transamine trae nulls en base.
    Si una tabla falla, se omite ese metal sin tumbar la respuesta.
    """
    backup = {}
    for metal, field in WESTMETALL_FIELDS.items():
        try:
            url = WESTMETALL_URL.format(field=field)
            resp = requests.get(url, headers=HEADERS, timeout=WESTMETALL_TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'lxml')
            n = 0
            for tr in soup.find_all('tr'):
                cells = [td.get_text().strip() for td in tr.find_all('td')]
                if len(cells) < 2:
                    continue
                m = re.match(r'(\d{1,2})\.\s+([A-Za-z]+)\s+(\d{4})', cells[0])
                if not m or m.group(2) not in MONTHS_EN:
                    continue
                fecha = "%04d-%02d-%02d" % (
                    int(m.group(3)), MONTHS_EN[m.group(2)], int(m.group(1)))
                if fecha < start_str or fecha > end_str:
                    continue
                value = parse_price(cells[1])  # columna Cash-Settlement
                if value is None:
                    continue
                backup.setdefault(fecha, {})[metal] = value
                n += 1
            print(f"  Westmetall respaldo: {metal} OK ({n} fechas)")
        except Exception as e:
            print(f"  Westmetall respaldo: error en {metal}: {e}")
    return backup


PRECIOUS_FIELDS = ['oro_am', 'oro_pm', 'plata']
BASE_FIELDS = ['cobre', 'plomo', 'zinc', 'niquel', 'estano']


def _fila_nueva(fecha):
    return {'fecha': fecha, 'oro_pm': None, 'oro_am': None, 'plata': None,
            'cobre': None, 'plomo': None, 'zinc': None, 'niquel': None,
            'estano': None}


def _dias_habiles(start_str, end_str):
    out = []
    day = datetime.strptime(start_str, '%Y-%m-%d')
    end_dt = datetime.strptime(end_str, '%Y-%m-%d')
    while day <= end_dt:
        if day.weekday() < 5:
            out.append(day.strftime('%Y-%m-%d'))
        day += timedelta(days=1)
    return out


def build_official_rows(start_str, end_str):
    """
    Fuentes PRINCIPALES (v4.0, Transamine dejo de actualizar en jul-2026):
      - Preciosos: LBMA (feeds JSON). Si LBMA no responde (p. ej. desde la
        red de Render), SGM (oro PM + plata) y Westmetall (oro AM).
      - Base: Westmetall (LME Cash-Settlement).
    Devuelve (rows {fecha: fila}, fuentes_preciosos, fuentes_base, descartes).
    `descartes` = valores que el filtro de atipicos tiro dentro del rango.
    """
    rows = {}
    descartes = []
    fuentes_prec = set()
    fuentes_base = set()

    prec = fetch_lbma_backup(start_str, end_str)
    if prec:
        fuentes_prec.add('lbma')
    else:
        print("  LBMA sin respuesta: usando SGM (oro PM, plata) y Westmetall (oro AM)")
        prec = fetch_sgm_backup(start_str, end_str, descartes)
        if prec:
            fuentes_prec.add('sgm')
        wm_au = fetch_westmetall_gold_am(start_str, end_str, descartes)
        if wm_au:
            fuentes_prec.add('westmetall')
        for fecha, vals in wm_au.items():
            prec.setdefault(fecha, {}).update(vals)
    for fecha, vals in prec.items():
        row = rows.setdefault(fecha, _fila_nueva(fecha))
        for f in PRECIOUS_FIELDS:
            if vals.get(f) is not None:
                row[f] = vals[f]

    base = fetch_westmetall_backup(start_str, end_str)
    if base:
        fuentes_base.add('westmetall')
    for fecha, vals in base.items():
        row = rows.setdefault(fecha, _fila_nueva(fecha))
        for f in BASE_FIELDS:
            if vals.get(f) is not None:
                row[f] = vals[f]

    return rows, fuentes_prec, fuentes_base, descartes


def _hay_huecos(rows, start_str, end_str):
    """True si alguna fila trae un metal en null o falta algun dia habil
    del rango (festivos incluidos: se intenta y si nadie publico, queda
    fuera)."""
    for row in rows.values():
        for f in PRECIOUS_FIELDS + BASE_FIELDS:
            if row.get(f) is None:
                return True
    return any(ds not in rows for ds in _dias_habiles(start_str, end_str))


def fill_from_transamine(rows, months, start_str, end_str):
    """
    ULTIMO RECURSO: Transamine solo rellena campos en null y agrega fechas
    que ninguna fuente oficial publico (p. ej. anios anteriores, donde las
    tablas de Westmetall no llegan). Nunca sobreescribe. Devuelve
    (fuentes_preciosos, fuentes_base) con lo que realmente aporto.
    """
    fuentes_prec = set()
    fuentes_base = set()
    session = requests.Session()
    for i, month in enumerate(months):
        print(f"  Transamine (ultimo recurso) {month}...")
        month_prices, _ = extract_month_prices(session, month)
        for fecha, vals in month_prices.items():
            if fecha < start_str or fecha > end_str:
                continue
            row = rows.setdefault(fecha, _fila_nueva(fecha))
            for f in PRECIOUS_FIELDS + BASE_FIELDS:
                if row.get(f) is None and vals.get(f) is not None:
                    row[f] = vals[f]
                    (fuentes_prec if f in PRECIOUS_FIELDS else fuentes_base).add('transamine')
        if i < len(months) - 1:
            time.sleep(PAUSE_BETWEEN_MONTHS)
    session.close()
    return fuentes_prec, fuentes_base


NOMBRES_CAMPO = {'oro_am': 'Oro AM', 'oro_pm': 'Oro PM', 'plata': 'Plata',
                 'cobre': 'Cobre', 'plomo': 'Plomo', 'zinc': 'Zinc',
                 'niquel': 'Níquel', 'estano': 'Estaño'}


def build_alertas(rows, descartes, start_str, end_str, ya_explicado=frozenset()):
    """
    Avisos de datos que NO estan entrando a la respuesta ni a los promedios:
      - valor_descartado: una fuente si publico el dato pero el filtro de
        atipicos lo tiro (se indica si otra fuente lo repuso o quedo vacio).
      - sin_precio: la fecha existe pero uno o mas metales quedaron vacios.
      - dia_sin_datos: dia habil del rango sin ningun precio (festivo o aun
        no publicado).
    Cada alerta trae `mensaje` listo para mostrar.
    """
    alertas = []
    ya_avisado = set(ya_explicado)
    for d in sorted(descartes, key=lambda x: (x['fecha'], x['campo'])):
        final = (rows.get(d['fecha']) or {}).get(d['campo'])
        nombre = NOMBRES_CAMPO.get(d['campo'], d['campo'])
        if final is None:
            cola = "el día quedó SIN precio y no entra al promedio"
            ya_avisado.add((d['fecha'], d['campo']))
        else:
            cola = f"se usó {final} de otra fuente"
        alertas.append({
            'tipo': 'valor_descartado', 'fecha': d['fecha'], 'campo': d['campo'],
            'valor': d['valor'], 'fuente': d['fuente'], 'valor_final': final,
            'mensaje': (f"{d['fecha']} {nombre}: se descartó {d['valor']} de {d['fuente']} "
                        f"por atípico (mediana de días vecinos {d['mediana_vecinos']}); {cola}"),
        })
    for fecha in sorted(rows):
        vacios = [f for f in PRECIOUS_FIELDS + BASE_FIELDS
                  if rows[fecha].get(f) is None and (fecha, f) not in ya_avisado]
        if vacios:
            nombres = ', '.join(NOMBRES_CAMPO.get(f, f) for f in vacios)
            alertas.append({
                'tipo': 'sin_precio', 'fecha': fecha, 'campos': vacios,
                'mensaje': f"{fecha}: sin precio de {nombres}; no entra al promedio de ese metal",
            })
    for fecha in _dias_habiles(start_str, end_str):
        if fecha not in rows:
            alertas.append({
                'tipo': 'dia_sin_datos', 'fecha': fecha,
                'mensaje': f"{fecha}: día hábil sin ningún precio (festivo o aún no publicado)",
            })
    alertas.sort(key=lambda a: a['fecha'])
    return alertas


# ---------------------------------------------------------------------------
# REFERENCIA (v4.2): precios LBMA/LME de un archivo de referencia que STH
# recibe por correo. El archivo es confidencial: se genera en la Mac con
# actualizar_referencia.py y NUNCA va en este repo (publico). El motor lo lee
#   1) de REFERENCIA_URL (p. ej. raw de un repo PRIVADO, con REFERENCIA_TOKEN)
#   2) o, si no hay URL, del archivo local data/referencia_precios.json
# Cuando la referencia y una fuente en linea no coinciden, gana la referencia
# y sale alerta. Para dias posteriores a la referencia, SD Bullion sirve de
# tercera opinion (solo avisa o rellena huecos, nunca corrige).
# ---------------------------------------------------------------------------
REFERENCIA_LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'data', 'referencia_precios.json')
REFERENCIA_TTL = 600  # segundos que se reutiliza la referencia descargada
TOLERANCIA = {'oro_am': 0.005, 'oro_pm': 0.005, 'plata': 0.006,
              'cobre': 0.01, 'plomo': 0.01, 'zinc': 0.01, 'niquel': 0.01,
              'estano': 0.01}
SDBULLION_URLS = {
    'oro': "https://sdbullion.com/gold-prices-{year}",
    'plata': "https://sdbullion.com/silver-prices-{year}",
}
_MESES_ABR = {m: i + 1 for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}
_ref_cache = {'t': 0.0, 'datos': None}


def cargar_referencia():
    """Devuelve {'hasta', 'desde', 'precios': {fecha: {campo: valor}}} o None."""
    url = os.environ.get('REFERENCIA_URL')
    if url:
        if _ref_cache['datos'] is not None and time.time() - _ref_cache['t'] < REFERENCIA_TTL:
            return _ref_cache['datos']
        try:
            hdrs = {'Accept': 'application/json'}
            token = os.environ.get('REFERENCIA_TOKEN')
            if token:
                hdrs['Authorization'] = f'token {token}'
            resp = requests.get(url, headers=hdrs, timeout=30)
            resp.raise_for_status()
            datos = resp.json()
            _ref_cache.update(t=time.time(), datos=datos)
            print(f"  Referencia: {datos.get('total_fechas')} fechas hasta {datos.get('hasta')}")
            return datos
        except Exception as e:
            print(f"  Referencia: error al descargar: {e}")
            return _ref_cache['datos']
    if os.path.exists(REFERENCIA_LOCAL):
        try:
            with open(REFERENCIA_LOCAL, encoding='utf-8') as fh:
                return json.load(fh)
        except Exception as e:
            print(f"  Referencia local ilegible: {e}")
    return None


def aplicar_referencia(rows, ref, start_str, end_str):
    """Corrige y rellena `rows` con la referencia.
    Devuelve (ajustes, usada_preciosos, usada_base)."""
    ajustes = []
    nuevas = []
    usada = set()
    for fecha, vals in sorted((ref or {}).get('precios', {}).items()):
        if fecha < start_str or fecha > end_str:
            continue
        if fecha not in rows:
            rows[fecha] = _fila_nueva(fecha)
            nuevas.append(fecha)
        row = rows[fecha]
        for campo, v in vals.items():
            if campo not in TOLERANCIA or v is None:
                continue
            actual = row.get(campo)
            nombre = NOMBRES_CAMPO.get(campo, campo)
            if actual is None:
                row[campo] = v
                usada.add(campo)
                if fecha not in nuevas:
                    ajustes.append({
                        'tipo': 'rellenado_con_referencia', 'fecha': fecha, 'campo': campo,
                        'valor_final': v,
                        'mensaje': f"{fecha} {nombre}: faltaba en las fuentes en línea; se tomó {v} de la referencia",
                    })
            elif abs(actual - v) > TOLERANCIA[campo]:
                row[campo] = v
                usada.add(campo)
                ajustes.append({
                    'tipo': 'corregido_con_referencia', 'fecha': fecha, 'campo': campo,
                    'valor': actual, 'valor_final': v,
                    'mensaje': (f"{fecha} {nombre}: la fuente en línea dio {actual} y la referencia "
                                f"{v}; se usó {v}"),
                })
    if nuevas:
        ajustes.append({
            'tipo': 'dias_desde_referencia', 'fecha': nuevas[0], 'fechas': nuevas,
            'mensaje': (f"{len(nuevas)} día(s) entre {nuevas[0]} y {nuevas[-1]} se tomaron de la "
                        f"referencia porque las fuentes en línea no los publican"),
        })
    return (ajustes, bool(usada & set(PRECIOUS_FIELDS)), bool(usada & set(BASE_FIELDS)))


# Valores de fixing LBMA / LME VERIFICADOS (v4.3). Son dias donde las
# fuentes publicas en linea (SGM, Westmetall, Transamine, SD Bullion) traen
# un error o no coinciden entre si; el valor correcto se confirmo con al menos
# dos fuentes independientes (referencia privada + publica o prensa). El motor
# los aplica siempre, aun sin la referencia (p. ej. en Render), y SD Bullion
# ya no avisa en estas fechas. Solo valores puntuales, no el historico.
# None = ese dia NO hubo fixing (media jornada en Londres, 24 y 31 de dic):
# se deja vacio aunque alguna fuente traiga un precio.
CORRECCIONES_VERIFICADAS = {
    '2024-01-15': {"cobre": 8238.5},
    '2024-08-28': {"estano": 32475.0},
    '2024-12-06': {"zinc": 3062.0},
    '2024-12-24': {"oro_pm": None},
    '2024-12-31': {"oro_pm": None},
    '2025-03-17': {"zinc": 2948.5},
    '2025-07-25': {"oro_am": 3346.6, "oro_pm": 3343.5, "plata": 38.735},
    '2025-10-03': {"oro_am": 3860.7, "oro_pm": 3885.7, "plata": 47.605},
    '2025-12-03': {"oro_am": 4200.9},
    '2025-12-23': {"oro_am": 4481.85},
    '2025-12-24': {"oro_pm": None, "plata": 72.18},
    '2025-12-31': {"oro_pm": None, "plata": 71.99},
    '2026-01-05': {"oro_am": 4436.7, "oro_pm": 4456.4},
    '2026-01-06': {"oro_am": 4452.75},
    '2026-01-22': {"oro_pm": 4832.05},
    '2026-03-05': {"oro_pm": 5104.05},
    '2026-03-24': {"oro_am": 4419.4},
    '2026-04-15': {"oro_pm": 4823.25},
    '2026-04-20': {"oro_am": 4784.1},
    '2026-04-21': {"oro_pm": 4776.75},
    '2026-04-23': {"plata": 74.55},
    '2026-05-11': {"plata": 80.4},
    '2026-05-13': {"plata": 86.7},
    '2026-05-15': {"plata": 78.74},
    '2026-06-11': {"plata": 63.8},
    '2026-06-12': {"oro_am": 4223.05},
    '2026-06-30': {"oro_pm": 4026.05},
    '2026-07-31': {"plata": 57.73},
    '2026-08-11': {"oro_pm": 4383.35},
}


def aplicar_correcciones(rows, start_str, end_str):
    """Aplica CORRECCIONES_VERIFICADAS. Devuelve (ajustes, verificados)."""
    ajustes = []
    verificados = set()
    for fecha, vals in sorted(CORRECCIONES_VERIFICADAS.items()):
        if fecha < start_str or fecha > end_str:
            continue
        row = rows.setdefault(fecha, _fila_nueva(fecha))
        for campo, v in vals.items():
            verificados.add((fecha, campo))
            actual = row.get(campo)
            nombre = NOMBRES_CAMPO.get(campo, campo)
            if v is None:
                row[campo] = None
                ajustes.append({
                    'tipo': 'sin_fixing', 'fecha': fecha, 'campo': campo,
                    'valor': actual, 'valor_final': None,
                    'mensaje': (f"{fecha} {nombre}: ese día no hubo fixing (media jornada en "
                                f"Londres); no entra al promedio"
                                + (f" (una fuente traía {actual}, no se usó)" if actual is not None else "")),
                })
                continue
            if actual is not None and abs(actual - v) <= TOLERANCIA[campo]:
                continue
            row[campo] = v
            antes = 'faltaba' if actual is None else f'la fuente en línea dio {actual}'
            ajustes.append({
                'tipo': 'corregido_verificado', 'fecha': fecha, 'campo': campo,
                'valor': actual, 'valor_final': v,
                'mensaje': f"{fecha} {nombre}: {antes}; se usó {v} (valor verificado con dos fuentes)",
            })
    return ajustes, verificados


def fetch_sdbullion(start_str, end_str):
    """Tablas diarias de SD Bullion (fixing LBMA AM/PM y plata) para el rango.
    Devuelve {fecha: {'oro_am', 'oro_pm', 'plata'}}. Si falla, {}."""
    out = {}
    years = range(int(start_str[:4]), int(end_str[:4]) + 1)
    for metal, plantilla in SDBULLION_URLS.items():
        for year in years:
            try:
                resp = requests.get(plantilla.format(year=year), headers=HEADERS, timeout=30)
                resp.raise_for_status()
                texto = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', resp.text))
                if metal == 'oro':
                    patron = r'\b([A-Z][a-z]{2}) (\d{1,2}), %d \$([\d,]+\.\d+) \$([\d,]+\.\d+)' % year
                else:
                    patron = r'\b([A-Z][a-z]{2}) (\d{1,2}), %d \$([\d,]+\.\d+)' % year
                n = 0
                for m in re.finditer(patron, texto):
                    if m.group(1) not in _MESES_ABR:
                        continue
                    fecha = "%04d-%02d-%02d" % (year, _MESES_ABR[m.group(1)], int(m.group(2)))
                    if fecha < start_str or fecha > end_str:
                        continue
                    d = out.setdefault(fecha, {})
                    if metal == 'oro':
                        d['oro_am'] = float(m.group(3).replace(',', ''))
                        d['oro_pm'] = float(m.group(4).replace(',', ''))
                    else:
                        d['plata'] = float(m.group(3).replace(',', ''))
                    n += 1
                print(f"  SD Bullion {metal} {year}: {n} fechas")
            except Exception as e:
                print(f"  SD Bullion {metal} {year}: error {e}")
    return out


def verificar_sdbullion(rows, desde_str, end_str, verificados=frozenset()):
    """Tercera opinion para dias que la referencia aun no cubre: rellena
    huecos de preciosos y avisa si no coincide (nunca corrige)."""
    ajustes = []
    usada = False
    rellenos = {}
    sin_mercado = []
    sd = fetch_sdbullion(desde_str, end_str)
    for fecha, vals in sorted(sd.items()):
        row = rows.get(fecha)
        if row is None:
            # Ninguna otra fuente (ni LME) publico ese dia: es festivo en
            # Londres y SD Bullion repite un precio. No se crea el dia.
            sin_mercado.append(fecha)
            continue
        for campo, v in vals.items():
            if (fecha, campo) in verificados:
                continue
            nombre = NOMBRES_CAMPO.get(campo, campo)
            actual = row.get(campo) if row else None
            if actual is None:
                row[campo] = v
                usada = True
                rellenos.setdefault(campo, []).append(fecha)
            elif abs(actual - v) > TOLERANCIA[campo]:
                ajustes.append({
                    'tipo': 'fuentes_no_coinciden', 'fecha': fecha, 'campo': campo,
                    'valor': v, 'valor_final': actual,
                    'mensaje': (f"{fecha} {nombre}: fuentes no coinciden / en línea {actual} vs "
                                f"SD Bullion {v}; se dejó {actual}, verificar contra LBMA"),
                })
    # Un aviso por metal (en anios anteriores SD Bullion es la fuente de todo
    # el anio y un aviso por dia seria ruido)
    for campo, fechas in rellenos.items():
        nombre = NOMBRES_CAMPO.get(campo, campo)
        ajustes.append({
            'tipo': 'rellenado_con_sdbullion', 'fecha': fechas[0], 'campo': campo,
            'fechas': fechas,
            'mensaje': (f"{nombre}: {len(fechas)} día(s) entre {fechas[0]} y {fechas[-1]} se "
                        f"tomaron de SD Bullion porque las fuentes principales no los publican"
                        + (f" ({', '.join(fechas)})" if len(fechas) <= 5 else "")),
        })
    if sin_mercado:
        ajustes.append({
            'tipo': 'sdbullion_sin_mercado', 'fecha': sin_mercado[0], 'fechas': sin_mercado,
            'mensaje': (f"SD Bullion trae precio en {len(sin_mercado)} día(s) sin mercado en Londres "
                        f"({', '.join(sin_mercado)}); no se usaron"),
        })
    return ajustes, usada


def _dia_siguiente(fecha_str):
    return (datetime.strptime(fecha_str, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')


# Avisos que NO requieren accion (v4.3.1): festivos, dias sin fixing,
# valores ya corregidos o rellenados con otra fuente. Van en `notas` (para
# auditoria) y no se muestran en la pagina ni en el Excel.
AVISOS_INFORMATIVOS = {
    'dia_sin_datos', 'sin_fixing', 'corregido_verificado',
    'corregido_con_referencia', 'rellenado_con_referencia', 'dias_desde_referencia',
    'rellenado_con_sdbullion', 'sdbullion_sin_mercado',
}


def separar_avisos(avisos):
    """Devuelve (alertas, notas). Alerta = algo que el usuario debe revisar:
    un dato que falta en un dia ya publicado, un valor descartado que nadie
    repuso o fuentes que no coinciden."""
    hoy = datetime.now().strftime('%Y-%m-%d')
    alertas, notas = [], []
    for a in avisos:
        tipo = a.get('tipo')
        informativo = (
            tipo in AVISOS_INFORMATIVOS
            or (tipo == 'valor_descartado' and a.get('valor_final') is not None)
            or (tipo == 'sin_precio' and a.get('fecha', '') >= hoy)  # aun no publicado
        )
        (notas if informativo else alertas).append(a)
    return alertas, notas


def compute_averages(rows, months):
    """Promedios mensuales calculados desde las filas diarias DEL RANGO
    (mes en curso o rango parcial = promedio parcial)."""
    averages = {}
    for month in months:
        mrows = [r for r in rows.values() if str(r.get('fecha', '')).startswith(month)]
        avgs = {}
        for f in PRECIOUS_FIELDS + BASE_FIELDS:
            vals = [r[f] for r in mrows if r.get(f) is not None]
            avgs[f] = round(sum(vals) / len(vals), 2) if vals else None
        if avgs.get('oro_am') and avgs.get('oro_pm'):
            avgs['oro_mean'] = round((avgs['oro_am'] + avgs['oro_pm']) / 2, 2)
        else:
            avgs['oro_mean'] = None
        averages[month] = avgs
    return averages


@app.route('/diag')
def diag():
    """
    Diagnostico de conectividad hacia cada fuente desde ESTE servidor.
    Sirve para saber, sin ver logs, cual fuente no responde (p. ej. LBMA
    desde Render). No devuelve precios, solo estatus por fuente.
    """
    hoy = datetime.now()
    mes = hoy.strftime('%Y-%m')
    fuentes = {
        'transamine': (f"{TRANSAMINE_URL}?choix_date={mes}", HEADERS),
        'lbma_silver': (LBMA_FEEDS['plata'], LBMA_HEADERS),
        'sgm_plata': (SGM_URL.format(opcion=SGM_OPCIONES['plata']), HEADERS),
        'westmetall_gold_am': (WESTMETALL_URL.format(field=WESTMETALL_GOLD_AM_FIELD), HEADERS),
        'westmetall_pb': (WESTMETALL_URL.format(field=WESTMETALL_FIELDS['plomo']), HEADERS),
    }
    out = {}
    for nombre, (url, hdrs) in fuentes.items():
        t0 = time.time()
        try:
            r = requests.get(url, headers=hdrs, timeout=30)
            out[nombre] = {
                'ok': r.ok,
                'http': r.status_code,
                'bytes': len(r.content),
                'segundos': round(time.time() - t0, 1),
                'server': r.headers.get('server'),
            }
        except Exception as e:
            out[nombre] = {
                'ok': False,
                'error': f"{type(e).__name__}: {e}",
                'segundos': round(time.time() - t0, 1),
            }
    return jsonify({'status': 'ok', 'fecha': hoy.strftime('%Y-%m-%d %H:%M'), 'fuentes': out})


@app.route('/')
def index():
    return jsonify({
        'status': 'ok',
        'message': 'STH Prices API',
        'version': '4.3.1',
        'endpoints': {
            '/': 'GET - Este mensaje',
            '/diag': 'GET - Estatus de conectividad a cada fuente de precios',
            '/extract_prices': 'POST - Extraer precios (fecha_inicio, fecha_fin)',
            '/generate_excel': 'POST - Generar Excel con precios'
        }
    })


@app.route('/extract_prices', methods=['POST'])
def extract_prices():
    try:
        data = request.get_json()
        
        if not data:
            return jsonify(clean_dict({'error': 'No se recibieron datos JSON'})), 400
        
        fecha_inicio = data.get('fecha_inicio')
        fecha_fin = data.get('fecha_fin')
        
        if not fecha_inicio or not fecha_fin:
            return jsonify(clean_dict({'error': 'Se requieren fecha_inicio y fecha_fin'})), 400
        
        try:
            start_date = datetime.strptime(fecha_inicio, '%Y-%m-%d')
            end_date = datetime.strptime(fecha_fin, '%Y-%m-%d')
        except ValueError:
            return jsonify(clean_dict({'error': 'Formato de fecha inválido. Use YYYY-MM-DD'})), 400
        
        if start_date > end_date:
            return jsonify(clean_dict({'error': 'fecha_inicio debe ser menor o igual a fecha_fin'})), 400
        
        months = get_months_in_range(start_date, end_date)
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
        print(f"Extrayendo {len(months)} mes(es): {', '.join(months)}")

        # 1) Fuentes oficiales: LBMA (o SGM + Westmetall) y Westmetall LME
        rows, fuentes_prec, fuentes_base, descartes = build_official_rows(start_str, end_str)

        # 2) Referencia (archivo por correo, si esta configurada): corrige y
        #    rellena con el fixing LBMA / LME.
        ajustes = []
        ref = cargar_referencia()
        ref_hasta = (ref or {}).get('hasta')
        if ref:
            a, usada_prec, usada_base = aplicar_referencia(rows, ref, start_str, end_str)
            ajustes += a
            if usada_prec:
                fuentes_prec.add('referencia')
            if usada_base:
                fuentes_base.add('referencia')

        # 3) Transamine solo como ultimo recurso para huecos (en anios
        #    anteriores es la que trae los dias de LME)
        if _hay_huecos(rows, start_str, end_str):
            print("  Huecos tras fuentes oficiales: consultando Transamine")
            tp, tb = fill_from_transamine(rows, months, start_str, end_str)
            fuentes_prec |= tp
            fuentes_base |= tb

        # 4) Valores verificados (siempre, aun sin referencia)
        a, verificados = aplicar_correcciones(rows, start_str, end_str)
        ajustes += a

        # 5) SD Bullion para preciosos en dias que la referencia aun no trae:
        #    rellena huecos de dias con mercado y avisa si no coincide
        if not ref_hasta or end_str > ref_hasta:
            desde = start_str if not ref_hasta else max(start_str, _dia_siguiente(ref_hasta))
            a, usada = verificar_sdbullion(rows, desde, end_str, verificados)
            ajustes += a
            if usada:
                fuentes_prec.add('sdbullion')

        results = [rows[d] for d in sorted(rows)]
        all_averages = compute_averages(rows, months)
        fuente_preciosos = '+'.join(sorted(fuentes_prec)) if fuentes_prec else 'sin_datos'
        fuente_base = '+'.join(sorted(fuentes_base)) if fuentes_base else 'sin_datos'

        clean_results = clean_dict(results)
        sin_fix = {(x['fecha'], x['campo']) for x in ajustes if x['tipo'] == 'sin_fixing'}
        alertas = build_alertas(rows, descartes, start_str, end_str, sin_fix) + ajustes
        alertas.sort(key=lambda x: x['fecha'])
        alertas, notas = separar_avisos(alertas)
        for a in alertas:
            print(f"  ALERTA: {a['mensaje']}")

        print(f"Extracción completada: {len(clean_results)} fechas con datos "
              f"(preciosos: {fuente_preciosos}, base: {fuente_base})")

        return jsonify({
            'status': 'ok',
            'total_fechas': len(clean_results),
            'meses_descargados': len(months),
            'fuente_preciosos': fuente_preciosos,
            'fuente_base': fuente_base,
            'promedios': clean_dict(all_averages),
            'referencia_hasta': ref_hasta,
            'hay_alertas': bool(alertas),
            'alertas': clean_dict(alertas),
            'notas': clean_dict(notas),
            'datos': clean_results
        })
        
    except Exception as e:
        print(f"Error en extract_prices: {e}")
        import traceback
        traceback.print_exc()
        return jsonify(clean_dict({'error': str(e)})), 500


@app.route('/generate_excel', methods=['POST'])
def generate_excel():
    try:
        data = request.get_json()
        
        if not data or 'datos' not in data:
            return jsonify(clean_dict({'error': 'Se requiere campo "datos" con precios'})), 400
        
        datos = data['datos']
        
        if not datos:
            return jsonify(clean_dict({'error': 'No hay datos para generar Excel'})), 400
        
        df = pd.DataFrame(datos)
        
        column_mapping = {
            'fecha': 'Fecha',
            'oro_pm': 'Oro PM (USD/oz)',
            'oro_am': 'Oro AM (USD/oz)',
            'plata': 'Plata (USD/oz)',
            'cobre': 'Cobre (USD/MT)',
            'plomo': 'Plomo (USD/MT)',
            'zinc': 'Zinc (USD/MT)',
            'niquel': 'Níquel (USD/MT)',
            'estano': 'Estaño (USD/MT)'
        }
        
        columns_to_use = [col for col in column_mapping.keys() if col in df.columns]
        df = df[columns_to_use]
        df = df.rename(columns=column_mapping)
        
        wb = Workbook()
        ws = wb.active
        ws.title = "Precios"
        
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="6B46C1", end_color="6B46C1", fill_type="solid")
        header_alignment = Alignment(horizontal="center", vertical="center")
        thin_border = Border(
            left=Side(style='thin'),
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin')
        )
        
        for r_idx, row in enumerate(dataframe_to_rows(df, index=False, header=True), 1):
            for c_idx, value in enumerate(row, 1):
                cell = ws.cell(row=r_idx, column=c_idx, value=clean_value(value))
                cell.border = thin_border
                
                if r_idx == 1:
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = header_alignment
                else:
                    cell.alignment = Alignment(horizontal="center")
        
        for column_cells in ws.columns:
            length = max(len(str(cell.value or "")) for cell in column_cells)
            ws.column_dimensions[column_cells[0].column_letter].width = length + 4
        
        # Hoja "Alertas": datos no considerados (si el cliente los manda)
        alertas = data.get('alertas') or []
        if alertas:
            wa = wb.create_sheet("Alertas")
            wa.append(['Fecha', 'Tipo', 'Detalle'])
            for cell in wa[1]:
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_alignment
                cell.border = thin_border
            for a in alertas:
                wa.append([str(a.get('fecha', '')), str(a.get('tipo', '')), str(a.get('mensaje', ''))])
            wa.column_dimensions['A'].width = 14
            wa.column_dimensions['B'].width = 20
            wa.column_dimensions['C'].width = 110

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"sth_prices_{timestamp}.xlsx"
        temp_dir = tempfile.gettempdir()
        filepath = os.path.join(temp_dir, filename)
        
        wb.save(filepath)
        
        return send_file(
            filepath,
            as_attachment=True,
            download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        
    except Exception as e:
        print(f"Error en generate_excel: {e}")
        return jsonify(clean_dict({'error': str(e)})), 500


if __name__ == '__main__':
    print("=" * 50)
    print("STH Prices API Server v4.3.1")
    print("=" * 50)
    print("Puerto: 8080")
    print("URL: http://localhost:8080")
    print("=" * 50)
    print("CORREGIDO: Lee spans en lugar de tablas")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=8080, debug=True)
