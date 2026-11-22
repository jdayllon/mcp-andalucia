# MCP Turismo y Transporte de Andalucía

Servidor **MCP (Model Context Protocol)** construido con **FastAPI**, **DuckDB**, **NDJSON** y **uv** para planificar itinerarios turísticos por Andalucía utilizando fuentes oficiales de datos abiertos:

- **Oferta turística (OpenRTA)**: [API Registro de Turismo de Andalucía](https://datos.juntadeandalucia.es/api/v0/openrta/openapi.json)
- **Autobuses interurbanos (CTAN)**: [Red de Consorcios de Transporte de Andalucía](https://api.ctan.es/doc/)
- **Trenes AVE / Larga Distancia / Media Distancia (Renfe)**: [Open Data Renfe (GTFS + GTFS-RT)](https://data.renfe.com/dataset?res_format=JSON)
- **Conectividad aérea (OpenSky Network)**: [OpenSky REST API](https://opensky-network.org)

> **Nota**: No incluye transporte urbano. Todas las respuestas de colecciones utilizan formato **NDJSON** (una línea `{"_meta": ...}` seguida de una línea JSON por registro) y los catálogos y el GTFS de Renfe se almacenan en **DuckDB** vía ficheros **NDJSON**.

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

### Transporte entre destinos
9. `buscar_trenes_renfe`: consulta SQL en DuckDB sobre el GTFS AVE/LD/MD de Renfe + retrasos en tiempo real.
10. `buscar_autobuses_ctan`: horarios interurbanos en los 9 Consorcios Metropolitanos de Andalucía.
11. `consultar_conectividad_aerea`: rutas recientes observadas en aeropuertos andaluces vía OpenSky.
12. `recomendar_transporte_entre_destinos`: comparativa multimodal (Renfe + CTAN + OpenSky) entre dos destinos.

## Tests

```bash
uv run pytest -v
```
