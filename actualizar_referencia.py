#!/usr/bin/env python3
"""
Actualiza la REFERENCIA de precios del motor STH Prices a partir del Excel
"SET<anio>.xlsx" que llega por correo ("PRECIOS COMEX/LME").

El Excel es de un tercero y es confidencial: este script NO lo copia; solo
extrae fecha y precio (fixing LBMA oro AM/PM y plata, LME cash Cu/Pb/Zn/Sn)
y los guarda en data/referencia_precios.json, que esta en .gitignore y por
lo tanto nunca se sube al repo publico.

Uso:
  python3 actualizar_referencia.py                 # busca en la cache de Spark
  python3 actualizar_referencia.py archivo.xlsx    # usa archivos especificos
  python3 actualizar_referencia.py --publicar      # ademas copia el JSON al
        repo PRIVADO indicado en STH_REFERENCIA_REPO (clon local) y hace
        commit + push, para que Render lo lea via REFERENCIA_URL.

Las fechas nunca se borran: cada corrida agrega o actualiza. Si dos archivos
traen la misma fecha, gana el archivo con datos mas recientes.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

import openpyxl

AQUI = os.path.dirname(os.path.abspath(__file__))
SALIDA = os.path.join(AQUI, 'data', 'referencia_precios.json')
SPARK_CACHE = os.path.expanduser(
    '~/Library/Containers/com.readdle.SparkDesktop.appstore/Data/Library/'
    'Caches/Spark Desktop/messagesData')
CAMPOS = ('oro_am', 'oro_pm', 'plata', 'cobre', 'plomo', 'zinc', 'estano')


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def leer_set(path):
    """Devuelve {fecha: {campo: valor}} de la hoja SET$10.
    Bloque 'COMEX COPPER': R=fecha, S=LME Cu cash, X=fecha, Y=LME Sn cash.
    Bloque 'COMEX GOLD':   A=fecha, F=Gold AM fix, I=Gold PM fix,
                           M=fecha, P=Silver price, R=fecha, S=LME Pb cash,
                           X=fecha, Y=LME Zn cash."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    hoja = next((n for n in wb.sheetnames if n.upper().startswith('SET')), None)
    if hoja is None:
        raise ValueError(f'{path}: no tiene hoja SET$10')
    precios = {}

    def put(fecha, campo, valor):
        v = _num(valor)
        if isinstance(fecha, datetime) and v is not None:
            precios.setdefault(fecha.strftime('%Y-%m-%d'), {})[campo] = v

    modo = None
    for fila in wb[hoja].iter_rows(values_only=True):
        r = list(fila) + [None] * 40
        a = r[0]
        if isinstance(a, str) and 'COMEX COPPER' in a.upper():
            modo = 'cu'
            continue
        if isinstance(a, str) and 'COMEX GOLD' in a.upper():
            modo = 'au'
            continue
        if isinstance(r[10], str) and 'MONTH' in r[10].upper():
            modo = None
            continue
        if modo == 'cu':
            put(r[17], 'cobre', r[18])
            put(r[23], 'estano', r[24])
        elif modo == 'au':
            put(r[0], 'oro_am', r[5])
            put(r[0], 'oro_pm', r[8])
            put(r[12], 'plata', r[15])
            put(r[17], 'plomo', r[18])
            put(r[23], 'zinc', r[24])
    wb.close()
    return precios


def buscar_en_spark():
    return sorted(glob.glob(os.path.join(SPARK_CACHE, '*', '*', 'SET20[0-9][0-9]*.xlsx')))


def main(argv):
    publicar = '--publicar' in argv
    archivos = [a for a in argv if not a.startswith('--')] or buscar_en_spark()
    if not archivos:
        print('No encontre ningun SET<anio>.xlsx (ni en la cache de Spark).')
        return 1

    leidos = []
    for path in archivos:
        try:
            p = leer_set(path)
        except Exception as e:
            print(f'  omitido {os.path.basename(path)}: {e}')
            continue
        if p:
            leidos.append((max(p), path, p))
    if not leidos:
        print('Ningun archivo trajo precios.')
        return 1
    leidos.sort()  # el de datos mas recientes al final: gana en empates

    previo = {}
    if os.path.exists(SALIDA):
        with open(SALIDA, encoding='utf-8') as fh:
            previo = json.load(fh).get('precios', {})

    # Por cada MES se usa solo la version mas reciente del archivo que lo
    # trae (no se mezclan versiones: las viejas a veces tienen fechas mal
    # capturadas que despues se corrigen, p. ej. 2026-04-11 con los precios
    # del 13). Los meses que ya no vienen en ningun archivo se conservan.
    por_mes = {}
    for _, _, p in leidos:  # orden ascendente: el ultimo que asigna gana
        meses = {}
        for fecha, vals in p.items():
            if datetime.strptime(fecha, '%Y-%m-%d').weekday() >= 5:
                continue  # fin de semana: captura erronea
            meses.setdefault(fecha[:7], {})[fecha] = {k: v for k, v in vals.items() if k in CAMPOS}
        por_mes.update(meses)
    precios = {f: dict(v) for f, v in previo.items() if f[:7] not in por_mes}
    for mes in por_mes.values():
        precios.update(mes)

    nuevas = sorted(set(precios) - set(previo))
    cambios = sorted(f for f in previo if f in precios and precios[f] != previo[f])
    os.makedirs(os.path.dirname(SALIDA), exist_ok=True)
    datos = {
        'actualizado': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'hasta': max(precios),
        'desde': min(precios),
        'total_fechas': len(precios),
        'precios': {f: precios[f] for f in sorted(precios)},
    }
    with open(SALIDA, 'w', encoding='utf-8') as fh:
        json.dump(datos, fh, ensure_ascii=False, indent=1)
    print(f'Archivos leidos: {len(leidos)} (el mas reciente trae hasta {leidos[-1][0]})')
    print(f'Referencia: {datos["total_fechas"]} fechas, {datos["desde"]} a {datos["hasta"]}')
    print(f'Fechas nuevas: {len(nuevas)}{" / " + ", ".join(nuevas[-10:]) if nuevas else ""}')
    if cambios:
        print(f'Fechas con valores cambiados: {", ".join(cambios)}')
    print(f'Guardado en {SALIDA}')

    if publicar:
        repo = os.environ.get('STH_REFERENCIA_REPO')
        if not repo or not os.path.isdir(os.path.join(repo, '.git')):
            print('--publicar: define STH_REFERENCIA_REPO con la ruta del clon del repo PRIVADO.')
            return 1
        destino = os.path.join(repo, 'referencia_precios.json')
        shutil.copyfile(SALIDA, destino)
        subprocess.run(['git', '-C', repo, 'add', 'referencia_precios.json'], check=True)
        hay = subprocess.run(['git', '-C', repo, 'diff', '--cached', '--quiet']).returncode
        if hay == 0:
            print('Repo privado: sin cambios.')
            return 0
        subprocess.run(['git', '-C', repo, 'commit', '-m',
                        f'referencia hasta {datos["hasta"]}'], check=True)
        subprocess.run(['git', '-C', repo, 'push'], check=True)
        print('Repo privado actualizado.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
