# STH Prices

Aplicación web para extraer automáticamente precios de metales (Oro PM, Plata, Cobre, Plomo y Zinc) desde [Transamine](https://www.transamine.com/price-and-review.html) y generar archivos Excel.

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
