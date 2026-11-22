"""Configuración centralizada de logging (siempre hacia stderr para no interferir con MCP stdio)."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from mcp_andalucia.config import settings


class JsonFormatter(logging.Formatter):
    """Formateador de logs en JSON (una línea por evento, compatible con NDJSON/Docker)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def setup_logging() -> logging.Logger:
    """Inicializa el logger raíz de `mcp_andalucia` escribiendo en stderr."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logger = logging.getLogger("mcp_andalucia")
    logger.setLevel(level)
    logger.propagate = False

    # Evitar duplicar handlers si se llama más de una vez
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)

    if settings.log_format.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    logger.addHandler(handler)
    return logger


def get_logger(name: str) -> logging.Logger:
    """Devuelve un sub-logger bajo `mcp_andalucia`."""
    root = logging.getLogger("mcp_andalucia")
    if not root.handlers:
        setup_logging()
    return logging.getLogger(f"mcp_andalucia.{name}")
