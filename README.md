# MCP Turismo y Transporte de Andalucía

Servidor **MCP (Model Context Protocol)** construido con **FastAPI**, **DuckDB**, **NDJSON** y **uv** para planificar itinerarios turísticos por Andalucía utilizando fuentes oficiales de datos abiertos:

- **Oferta turística (OpenRTA)**: [API Registro de Turismo de Andalucía](https://datos.juntadeandalucia.es/api/v0/openrta/openapi.json)
- **Declaraciones de Interés Turístico en Andalucía**: [Catálogo de Fiestas, Romerías y Municipios Turísticos (Junta de Andalucía)](https://www.juntadeandalucia.es/datosabiertos/portal/dataset/declaraciones-de-interes-turistico-en-andalucia/resource/827cb3e2-fe05-4c37-92d3-ed31f2935f35)
- **Datos Municipales SIMA ("Andalucía pueblo a pueblo")**: [Fichas de los 785 municipios del IECA (`smex99.xls`)](https://www.juntadeandalucia.es/institutodeestadisticaycartografia/sima/datos/smex99.xls)
- **Banderas Azules de Andalucía**: [Playas, puertos deportivos y embarcaciones (`banderas_azules_2026-27.json`)](https://www.juntadeandalucia.es/datosabiertos/portal/dataset/6b186c12-2fd9-450d-a325-02052e2fe352/resource/e93bbb0c-5b31-4c59-8cf0-801b59fea253/download/banderas_azules_2026-27.json)
- **Autobuses interurbanos (CTAN)**: [Red de Consorcios de Transporte de Andalucía](https://api.ctan.es/doc/)
- **Trenes AVE / Larga Distancia / Media Distancia (Renfe)**: [Open Data Renfe (GTFS + GTFS-RT)](https://data.renfe.com/dataset?res_format=JSON)
- **Conectividad aérea (OpenSky Network)**: [OpenSky REST API](https://opensky-network.org)
- **Transporte urbano (TUSSAM Sevilla, EMT Málaga y NAP Ministerio de Transportes)**:
  - [API-TUSSAM (`686f6c61/API-TUSSAM`)](https://github.com/686f6c61/API-TUSSAM)
  - [Datos Abiertos EMT Málaga](https://datosabiertos.malaga.eu/dataset?tags=EMT)
  - [Punto de Acceso Nacional (NAP) — AUCORSA Córdoba, Granada, Metros andaluces](https://nap.transportes.gob.es)

> **Nota**: Todas las respuestas de colecciones utilizan formato **NDJSON** (una línea `{"_meta": ...}` seguida de una línea JSON por registro). Los catálogos, redes urbanas y el GTFS de Renfe se almacenan en **DuckDB** vía ficheros **NDJSON**, con motor de cercanía **Bounding Box + Haversine** y resolución de direcciones en 3 niveles (coordenadas exactas → callejero DuckDB → geocodificación OSM cacheada).

## Instalación y ejecución

```bash
# Instalar dependencias
uv sync

# Iniciar servidor FastAPI (MCP Streamable HTTP en /mcp y /health)
uv run mcp-andalucia

# O iniciar en modo stdio para clientes locales (Claude Desktop, Cursor, Antigravity)
uv run mcp-andalucia --transport stdio
```

### Variables de entorno opcionales (`MCP_ANDALUCIA_*`)

- `MCP_ANDALUCIA_DATA_DIR`: directorio de caché DuckDB y NDJSON (por defecto `~/.cache/mcp-andalucia`, en Docker `/data`).
- `MCP_ANDALUCIA_RESPONSE_CACHE_TTL_SECONDS`: TTL en segundos para caché de respuestas de búsqueda/horarios (por defecto `900`, 15 min).
- `MCP_ANDALUCIA_LOG_LEVEL`: nivel de logs (`DEBUG`, `INFO`, `WARNING`, `ERROR`; por defecto `INFO`).
- `MCP_ANDALUCIA_LOG_FORMAT`: formato de logs (`text` o `json`; por defecto `text`).
- `MCP_ANDALUCIA_NAP_API_KEY`: clave `ApiKey` del Punto de Acceso Nacional (`nap.transportes.gob.es`) para cargar GTFS urbanos (Córdoba AUCORSA, Granada, Metros, etc.).
- `MCP_ANDALUCIA_OPENSKY_CLIENT_ID` y `MCP_ANDALUCIA_OPENSKY_CLIENT_SECRET`: credenciales OAuth2 opcionales para OpenSky.
- `MCP_ANDALUCIA_HOST` / `MCP_ANDALUCIA_PORT`: host y puerto HTTP (por defecto `127.0.0.1:8000`).

## Ejecución con Docker

```bash
# Construir y levantar con docker compose (persiste DuckDB/NDJSON en volumen)
docker compose up -d --build

# O directamente desde GitHub Container Registry (una vez publicada la imagen)
docker run -d --name mcp-andalucia -p 8000:8000 -v mcp_andalucia_data:/data ghcr.io/<tu-usuario>/mcp-andalucia:latest
```

El servidor quedará disponible en `http://127.0.0.1:8000/mcp` (Streamable HTTP) y `http://127.0.0.1:8000/health`.

## Herramientas MCP incluidas (12 tools)

### Turismo (OpenRTA)
1. `buscar_alojamientos`: hoteles, apartamentos, casas rurales, campings y VUT.
2. `buscar_guias_turisticos`: guías oficiales con filtro por idioma y contacto directo.
3. `buscar_actividades_turisticas`: turismo activo, ecoturismo y ocio por palabra clave.
4. `buscar_restauracion`: restauración y catering turístico.
5. `buscar_oficinas_turismo`: oficinas y puntos de información turística.
6. `explorar_destino`: radiografía paralela completa de un destino para planificar itinerarios.
7. `obtener_detalle_elemento`: ficha completa por `id` con coordenadas WGS84 (`lat`, `lon`).
8. `buscar_en_registro`: comodín para otros tipos, consulta de catálogos y estado del dataset.
9. `buscar_declaraciones_interes_turistico`: consulta en DuckDB (`read_csv` con delimitador `=`) de fiestas, romerías, semanas santas y municipios declarados de interés turístico.
10. `consultar_municipios_sima`: ficha de los 785 municipios del SIMA (`smex99.xls`: coordenadas oficiales, altitud, población, plazas hoteleras, patrimonio IAPH + enlace a Guía Digital del IAPH y las 124 variables) y búsqueda de pueblos cercanos con Haversine en DuckDB.
11. `buscar_banderas_azules`: consulta en DuckDB (`read_json_auto`) de playas, puertos deportivos y embarcaciones con Bandera Azul en Andalucía (última edición o histórico por año).

### Transporte entre destinos
9. `buscar_trenes_renfe`: consulta SQL en DuckDB sobre el GTFS AVE/LD/MD de Renfe + retrasos en tiempo real.
10. `buscar_autobuses_ctan`: horarios interurbanos en los 9 Consorcios Metropolitanos de Andalucía.
11. `consultar_conectividad_aerea`: rutas recientes observadas en aeropuertos andaluces vía OpenSky.
12. `recomendar_transporte_entre_destinos`: comparativa multimodal (Renfe + CTAN + OpenSky) entre dos destinos.

### Transporte urbano local (con Haversine en DuckDB)
13. `buscar_paradas_urbanas_cercanas`: paradas de autobús/metro cercanas a coordenadas `lat`/`lon` o a una calle/CP (TUSSAM Sevilla, EMT Málaga, AUCORSA Córdoba, Granada, Metros).
14. `consultar_tiempo_real_bus_urbano`: tiempos de llegada en tiempo real en Sevilla (TUSSAM) y posición GPS en tiempo real en Málaga (EMT).
15. `consultar_linea_urbana`: recorrido completo y paradas ordenadas de cualquier línea urbana.
16. `catalogo_nap_transporte_urbano`: listado e indexación bajo demanda en DuckDB de cualquier dataset GTFS andaluz del NAP.

## Tests

```bash
uv run pytest -v
```
