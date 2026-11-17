# STH Prices

Aplicación web para extraer precios diarios oficiales de metales (Oro AM/PM, Plata, Cobre, Plomo, Zinc, Níquel, Estaño) y generar archivos Excel. Fuentes (v4.0): LBMA para preciosos (con SGM y Westmetall de respaldo) y Westmetall (LME Cash-Settlement) para metales base; [Transamine](https://www.transamine.com/price-and-review.html) solo como último recurso, ya que dejó de actualizar en julio 2026.

## Requisitos Previos

- **Python 3.9+** (verificar con `python3 --version`)
- **Google Chrome** instalado
- **macOS** (instrucciones específicas para Mac)

## Instalación

### 1. Crear directorio de trabajo

```bash
mkdir -p ~/Downloads/sth_prices
cd ~/Downloads/sth_prices
```

### 2. Copiar los archivos

Coloca estos archivos en el directorio:
- `sth_prices_server.py`
- `sth_prices_app.html`

### 3. Instalar dependencias Python

```bash
pip3 install flask flask-cors selenium pandas openpyxl webdriver-manager requests beautifulsoup4 lxml
```

> **Nota:** Usar `pip3`, NO `pip` en Mac.

## Uso

### 1. Iniciar el servidor

```bash
cd ~/Downloads/sth_prices
python3 sth_prices_server.py
```

Deberías ver:
```
==================================================
STH Prices API Server
==================================================
Puerto: 8080
URL: http://localhost:8080
==================================================
```

### 2. Abrir la interfaz web

En otra terminal:

```bash
open ~/Downloads/sth_prices/sth_prices_app.html
```

O simplemente haz doble clic en el archivo `sth_prices_app.html`.

### 3. Extraer precios

1. Selecciona la fecha de inicio
2. Selecciona la fecha de fin (máximo 60 días de rango)
3. Haz clic en "Extraer Precios"
4. Espera a que se procesen todas las fechas
5. Haz clic en "Descargar Excel" para obtener el archivo

## Troubleshooting

### Error: "No se puede conectar al servidor"

**Verificar que el servidor esté corriendo:**
```bash
lsof -i :8080
```

Si no hay resultado, el servidor no está corriendo. Inícialo con:
```bash
python3 sth_prices_server.py
```

### Error: Puerto ocupado

**Verificar qué proceso usa el puerto:**
```bash
lsof -i :8080
```

**Matar el proceso (si es necesario):**
```bash
kill -9 <PID>
```

### Nota sobre puerto 5000

En macOS Monterey y posteriores, el puerto 5000 está ocupado por **AirPlay Receiver**. Por eso esta aplicación usa el puerto **8080**.

Si necesitas liberar el puerto 5000:
1. Ve a Preferencias del Sistema → Compartir
2. Desmarca "AirPlay Receiver"

**Sin embargo, recomendamos mantener el puerto 8080** ya configurado.

### Error: Chrome/ChromeDriver no encontrado

**Verificar Chrome:**
```bash
ls /Applications/Google\ Chrome.app
```

Si no está instalado, descárgalo de [google.com/chrome](https://www.google.com/chrome/).

**ChromeDriver** se instala automáticamente con `webdriver-manager`.

### Error: Timeout o página no carga

Posibles causas:
- Conexión a internet lenta
- Transamine bloqueando requests
- ChromeDriver desactualizado

**Solución:**
```bash
pip3 install --upgrade webdriver-manager
```

### Error: ModuleNotFoundError

Reinstalar dependencias:
```bash
pip3 install --upgrade flask flask-cors selenium pandas openpyxl webdriver-manager
```

## Estructura de Archivos

```
sth_prices/
├── sth_prices_server.py   # Servidor Flask (puerto 8080)
├── sth_prices_app.html    # Interfaz web
└── README.md              # Este archivo
```

## Metales Extraídos

| Metal | Campo | Unidad |
|-------|-------|--------|
| Oro PM | oro_pm | USD/oz |
| Plata | plata | USD/oz |
| Cobre | cobre | USD/lb |
| Plomo | plomo | USD/lb |
| Zinc | zinc | USD/lb |

## Archivo Excel

El archivo generado incluye:
- **Hoja:** "Precios"
- **Columnas:** Fecha, Oro PM, Plata, Cobre, Plomo, Zinc
- **Formato:** Encabezados con estilo, anchos de columna ajustados
- **Nombre:** `sth_prices_YYYYMMDD_HHMMSS.xlsx`

## Limitaciones

- Máximo 60 días por consulta (para evitar sobrecarga)
- Pausa de 2 segundos entre cada request
- Timeout de 10 segundos por página
- Requiere conexión a internet activa

## Notas Técnicas

- **Puerto:** 8080 (hardcoded, NO cambiar)
- **Scraping:** Selenium con Chrome headless
- **ChromeDriver:** Instalación automática vía webdriver-manager
- **CORS:** Habilitado para permitir requests desde archivo local

## API Endpoints

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/` | Información del API |
| POST | `/extract_prices` | Extraer precios (requiere fecha_inicio, fecha_fin) |
| POST | `/generate_excel` | Generar Excel (requiere datos) |

### Ejemplo de Request

```bash
curl -X POST http://localhost:8080/extract_prices \
  -H "Content-Type: application/json" \
  -d '{"fecha_inicio": "2024-01-01", "fecha_fin": "2024-01-05"}'
```

## Soporte

Si encuentras problemas:
1. Verifica los requisitos previos
2. Consulta la sección de Troubleshooting
3. Revisa los logs del servidor en la terminal

---

**Puerto del servidor: 8080** (NO usar puerto 5000 en Mac)

## v4.1.0 / filtro de atípicos por vecinos y alertas

- El filtro de atípicos ya no compara contra la mediana de todo el rango (tiraba precios reales, p. ej. la plata del 19 al 30 de enero 2026 al pedir rangos largos). Ahora compara cada valor contra la mediana de sus días vecinos (5 antes y 5 después) sobre la tabla completa de la fuente.
- `/extract_prices` regresa `hay_alertas` y `alertas` con los datos que NO se están considerando:
  - `valor_descartado`: la fuente publicó el dato pero el filtro lo tiró (indica si otra fuente lo repuso o quedó vacío).
  - `sin_precio`: la fecha existe pero uno o más metales quedaron vacíos y no entran al promedio.
  - `dia_sin_datos`: día hábil del rango sin ningún precio (festivo o aún no publicado).
- `/generate_excel` acepta `alertas` y las escribe en la hoja "Alertas". La página las muestra en un recuadro arriba de los resultados.

## v4.2.0 / referencia de precios por correo y SD Bullion

- **Referencia:** STH recibe por correo un Excel con el fixing LBMA (oro AM/PM, plata) y el LME cash (Cu, Pb, Zn, Sn) diario. Es confidencial y **no va en este repo**: `actualizar_referencia.py` lo lee en la Mac (cache de Spark o archivos indicados), extrae solo fecha y precio y escribe `data/referencia_precios.json` (en `.gitignore`). Por cada mes usa la versión más reciente del archivo y descarta fines de semana.
- **El motor** carga la referencia de `REFERENCIA_URL` (variable de entorno; con `REFERENCIA_TOKEN` si es un repo privado de GitHub, se reutiliza 10 min) o, si no hay URL, del archivo local. Si una fuente en línea no coincide con la referencia, gana la referencia; si falta un dato, lo rellena. Todo sale en `alertas` (`corregido_con_referencia`, `rellenado_con_referencia`, `dias_desde_referencia`).
- **SD Bullion:** para días posteriores a la referencia, sirve de tercera opinión de preciosos: rellena huecos (`rellenado_con_sdbullion`) y avisa si no coincide (`fuentes_no_coinciden`), sin corregir.
- `/extract_prices` regresa además `referencia_hasta`.
- Para usarla en Render: crear un repo **privado** con `referencia_precios.json`, poner en Render `REFERENCIA_URL` (raw del archivo) y `REFERENCIA_TOKEN` (token de solo lectura), y correr `STH_REFERENCIA_REPO=<clon local> python3 actualizar_referencia.py --publicar` cuando llegue un correo nuevo.

## v4.3.0 / correcciones verificadas y orden de fuentes

- Orden: LBMA (bloqueado desde servidores) / SGM + Westmetall, referencia (si está configurada), Transamine para huecos, **correcciones verificadas**, y SD Bullion para preciosos que sigan faltando.
- `CORRECCIONES_VERIFICADAS`: valores puntuales de fixing donde las fuentes públicas traen errores, confirmados con dos fuentes independientes; `None` marca días sin fixing (PM del 24 y 31 de diciembre, media jornada en Londres). Se aplican siempre, también en Render sin referencia.
- SD Bullion ya no crea días que ninguna otra fuente publicó (festivos de Londres) ni avisa en fechas verificadas; los rellenos se resumen en un aviso por metal.
- Verificado contra la referencia privada: 2024-01-02 a 2026-11-09 sin diferencias en valores diarios ni en promedios mensuales (la referencia no trae plata de ene-2024 a mar-2025; esa plata viene solo de SD Bullion).

## v4.3.1 / solo avisos accionables

- `alertas` (página y hoja "Alertas" del Excel) trae solo lo que requiere revisión: un metal sin precio en un día ya publicado, un valor descartado que nadie repuso o fuentes que no coinciden.
- Lo informativo (festivos, días sin fixing, valores ya corregidos o rellenados) pasa a `notas`, que solo regresa el API para auditoría.

- v4.3.2: con la referencia activa, SD Bullion también rellena preciosos que la referencia no trae (plata ene-2024 a mar-2025), sin cuestionar sus valores.
