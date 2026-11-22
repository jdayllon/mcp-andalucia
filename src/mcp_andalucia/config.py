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
