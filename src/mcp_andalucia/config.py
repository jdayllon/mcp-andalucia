"""Configuración del servidor MCP de oferta turística y transporte de Andalucía."""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración cargada desde variables de entorno o valores por defecto."""

    model_config = SettingsConfigDict(
        env_prefix="MCP_ANDALUCIA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "MCP Turismo y Transporte de Andalucía"
    version: str = "0.1.0"
    host: str = "127.0.0.1"
    port: int = 8000

    # Hosts y orígenes permitidos por la protección anti DNS-rebinding del transporte HTTP MCP.
    # Incluye el dominio público del despliegue y los valores locales habituales.
    allowed_hosts: list[str] = [
        "openrta-mcp.gui11o.ovh",
        "openrta-mcp.gui11o.ovh:*",
        "*.gui11o.ovh",
        "*.gui11o.ovh:*",
        "192.168.100.181",
        "192.168.100.181:*",
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "[::1]",
        "[::1]:*",
    ]
    allowed_origins: list[str] = [
        "https://openrta-mcp.gui11o.ovh",
        "https://openrta-mcp.gui11o.ovh:*",
        "https://*.gui11o.ovh",
        "http://192.168.100.181",
        "http://192.168.100.181:*",
        "http://localhost",
        "http://localhost:*",
        "http://127.0.0.1",
        "http://127.0.0.1:*",
        "http://[::1]",
        "http://[::1]:*",
    ]

    # Directorio local de caché (DuckDB y ficheros NDJSON / GTFS)
    data_dir: Path = Path.home() / ".cache" / "mcp-andalucia"
    duckdb_filename: str = "andalucia.duckdb"

    # URLs de las APIs públicas
    openrta_base_url: str = "https://datos.juntadeandalucia.es/api/v0/openrta"
    ctan_base_url: str = "https://api.ctan.es/v1"
    renfe_gtfs_url: str = "https://ssl.renfe.com/gtransit/Fichero_AV_LD/google_transit.zip"
    renfe_rt_trips_url: str = "https://gtfsrt.renfe.com/trip_updates_LD.json"
    opensky_base_url: str = "https://opensky-network.org/api"
    opensky_token_url: str = (
        "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
    )

    # Credenciales opcionales para OpenSky (OAuth2 client credentials)
    opensky_client_id: str | None = None
    opensky_client_secret: str | None = None

    # APIs de transporte urbano (TUSSAM Sevilla, EMT Málaga y NAP Ministerio de Transportes)
    tussam_db_url: str = "https://raw.githubusercontent.com/686f6c61/API-TUSSAM/main/data/tussam.db"
    tussam_base_url: str = "https://reddelineas.tussam.es"
    tussam_proxy_url: str | None = None
    emt_malaga_lineas_paradas_url: str = (
        "https://datosabiertos.malaga.eu/recursos/transporte/EMT/EMTLineasYParadas/lineasyparadas.csv"
    )
    emt_malaga_realtime_url: str = (
        "https://datosabiertos.malaga.eu/recursos/transporte/EMT/EMTlineasUbicaciones/lineasyubicaciones.geojson"
    )
    nap_base_url: str = "https://nap.transportes.gob.es/api"
    nap_api_key: str | None = None
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"

    # Declaraciones de Interés Turístico (Portal Datos Abiertos Junta) y SIMA (IECA)
    declaraciones_resource_url: str = (
        "https://www.juntadeandalucia.es/datosabiertos/portal/dataset/"
        "declaraciones-de-interes-turistico-en-andalucia/resource/827cb3e2-fe05-4c37-92d3-ed31f2935f35"
    )
    declaraciones_csv_fallback_url: str = (
        "https://www.juntadeandalucia.es/datosabiertos/portal/dataset/"
        "9c1437e9-98bd-41e5-84cf-6bf678037b1b/resource/827cb3e2-fe05-4c37-92d3-ed31f2935f35/"
        "download/dataset_interes_turistico_07072026.csv"
    )
    sima_municipios_xls_url: str = (
        "https://www.juntadeandalucia.es/institutodeestadisticaycartografia/sima/datos/smex99.xls"
    )

    # TTL de caché en segundos
    catalog_ttl_seconds: int = 86400  # 24h para catálogos OpenRTA y CTAN
    renfe_gtfs_ttl_seconds: int = 86400  # 24h para GTFS estático de Renfe
    response_cache_ttl_seconds: int = 900  # 15 min para respuestas de búsqueda/horarios
    http_timeout_seconds: float = 20.0

    # Logging
    log_level: str = "INFO"
    log_format: str = "text"  # "text" o "json"

    @property
    def duckdb_path(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / self.duckdb_filename

    @property
    def ndjson_dir(self) -> Path:
        path = self.data_dir / "ndjson"
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
