"""Punto de entrada del paquete mcp_andalucia (modo HTTP con FastAPI o stdio)."""

from __future__ import annotations

import argparse

import uvicorn

from mcp_andalucia.config import settings
from mcp_andalucia.logging_config import setup_logging
from mcp_andalucia.server import create_mcp_server


def main() -> None:
    """Inicia el servidor MCP en modo HTTP (FastAPI) o stdio."""
    setup_logging()
    parser = argparse.ArgumentParser(description="Servidor MCP de Turismo y Transporte de Andalucía")
    parser.add_argument(
        "--transport",
        choices=("http", "stdio"),
        default="http",
        help="Transporte MCP a utilizar (por defecto: http con FastAPI en /mcp).",
    )
    parser.add_argument("--host", default=settings.host, help="Host HTTP (solo modo http).")
    parser.add_argument("--port", type=int, default=settings.port, help="Puerto HTTP (solo modo http).")
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp = create_mcp_server()
        mcp.run(transport="stdio")
    else:
        uvicorn.run("mcp_andalucia.app:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
