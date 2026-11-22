"""Aplicación FastAPI que expone el servidor MCP (Streamable HTTP) y endpoints de salud/estado."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from mcp_andalucia.clients.openrta import OpenRTAClient
from mcp_andalucia.config import settings
from mcp_andalucia.db import get_store
from mcp_andalucia.logging_config import get_logger, setup_logging
from mcp_andalucia.server import create_mcp_server
from mcp_andalucia.utils import to_ndjson

setup_logging()
logger = get_logger("http")

mcp_server = create_mcp_server()
# Montamos el app MCP en /mcp; la ruta interna debe ser "/" para que el mount en /mcp quede en /mcp
mcp_http_app = mcp_server.streamable_http_app(streamable_http_path="/")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Gestiona el ciclo de vida del session_manager de MCP y la base DuckDB."""
    store = get_store()
    logger.info("Iniciando %s v%s (DuckDB: %s)", settings.app_name, settings.version, store.db_path)
    async with mcp_server.session_manager.run():
        yield
    logger.info("Cerrando servidor y conexión DuckDB")
    store.close()


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    lifespan=lifespan,
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Registra método, ruta, código de estado y latencia de cada petición HTTP."""
    t0 = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.info("%s %s -> %d (%.1f ms)", request.method, request.url.path, response.status_code, elapsed_ms)
    return response


@app.get("/health")
async def health() -> dict[str, object]:
    """Comprobación de salud del servicio y estadísticas de caché en DuckDB."""
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.version,
        "cache": get_store().cache_stats(),
    }


@app.get("/api/status.ndjson", response_class=PlainTextResponse)
async def dataset_status_ndjson() -> PlainTextResponse:
    """Devuelve el estado del dataset OpenRTA en formato NDJSON."""
    client = OpenRTAClient()
    status = await client.get_dataset_status()
    body = to_ndjson([status], meta={"fuente": "OpenRTA", "formato": "ndjson"})
    return PlainTextResponse(content=body, media_type="application/x-ndjson")


app.mount("/mcp", mcp_http_app)
