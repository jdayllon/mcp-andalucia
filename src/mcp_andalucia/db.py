"""Capa de persistencia y consulta analítica con DuckDB + NDJSON."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import duckdb

from mcp_andalucia.config import settings
from mcp_andalucia.logging_config import get_logger

logger = get_logger("db")


class DuckDBStore:
    """Gestor thread-safe de DuckDB con soporte nativo para exportación/lectura NDJSON."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or settings.duckdb_path
        self._lock = threading.RLock()
        self._conn = duckdb.connect(str(self.db_path))
        self._init_meta_table()

    def _init_meta_table(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_metadata (
                    key VARCHAR PRIMARY KEY,
                    updated_at DOUBLE NOT NULL,
                    row_count INTEGER DEFAULT 0
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS catalog_cache (
                    namespace VARCHAR NOT NULL,
                    value VARCHAR NOT NULL,
                    norm_value VARCHAR NOT NULL,
                    meta_json VARCHAR,
                    PRIMARY KEY (namespace, value)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS response_cache (
                    cache_key VARCHAR PRIMARY KEY,
                    source VARCHAR NOT NULL,
                    payload_json VARCHAR NOT NULL,
                    created_at DOUBLE NOT NULL,
                    expires_at DOUBLE NOT NULL
                )
                """
            )

    def is_fresh(self, key: str, ttl_seconds: int) -> bool:
        """Comprueba si una clave de caché sigue vigente según su TTL."""
        with self._lock:
            row = self._conn.execute(
                "SELECT updated_at FROM cache_metadata WHERE key = ?", [key]
            ).fetchone()
            if not row:
                return False
            return (time.time() - float(row[0])) < ttl_seconds

    def mark_fresh(self, key: str, row_count: int = 0) -> None:
        """Actualiza la marca temporal de una clave de caché."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cache_metadata (key, updated_at, row_count)
                VALUES (?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET updated_at = excluded.updated_at, row_count = excluded.row_count
                """,
                [key, time.time(), row_count],
            )

    def save_catalog(self, namespace: str, values: list[dict[str, Any]]) -> None:
        """Guarda un catálogo en DuckDB y también como NDJSON en disco."""
        with self._lock:
            self._conn.execute("DELETE FROM catalog_cache WHERE namespace = ?", [namespace])
            rows = [
                (
                    namespace,
                    str(v["value"]),
                    str(v["norm_value"]),
                    json.dumps(v.get("meta"), ensure_ascii=False) if v.get("meta") is not None else None,
                )
                for v in values
            ]
            if rows:
                self._conn.executemany(
                    "INSERT INTO catalog_cache (namespace, value, norm_value, meta_json) VALUES (?, ?, ?, ?)",
                    rows,
                )
            self.mark_fresh(f"catalog:{namespace}", len(rows))

        ndjson_path = settings.ndjson_dir / f"catalog_{namespace.replace(':', '_')}.ndjson"
        with ndjson_path.open("w", encoding="utf-8") as f:
            for v in values:
                f.write(json.dumps(v, ensure_ascii=False, separators=(",", ":")) + "\n")

    def get_catalog(self, namespace: str) -> list[dict[str, Any]]:
        """Recupera un catálogo completo desde DuckDB."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT value, norm_value, meta_json FROM catalog_cache WHERE namespace = ? ORDER BY value",
                [namespace],
            ).fetchall()
        result: list[dict[str, Any]] = []
        for val, norm, meta_json in rows:
            result.append(
                {
                    "value": val,
                    "norm_value": norm,
                    "meta": json.loads(meta_json) if meta_json else None,
                }
            )
        return result

    def execute(self, sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        """Ejecuta una consulta SQL y devuelve todas las filas."""
        with self._lock:
            return self._conn.execute(sql, params or []).fetchall()

    def execute_dicts(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Ejecuta una consulta SQL y devuelve una lista de diccionarios."""
        with self._lock:
            cur = self._conn.execute(sql, params or [])
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def load_ndjson_table(self, table_name: str, ndjson_path: Path) -> int:
        """Crea o reemplaza una tabla en DuckDB directamente desde un fichero NDJSON."""
        t0 = time.perf_counter()
        with self._lock:
            self._conn.execute(
                f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM read_ndjson_auto(?)",
                [str(ndjson_path)],
            )
            count_row = self._conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
            count = int(count_row[0]) if count_row else 0
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.info("DuckDB tabla '%s' cargada desde NDJSON: %d filas (%.1f ms)", table_name, count, elapsed_ms)
        return count

    def get_cached_response(self, cache_key: str) -> Any | None:
        """Recupera una respuesta cacheada si no ha expirado."""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json, expires_at FROM response_cache WHERE cache_key = ?",
                [cache_key],
            ).fetchone()
            if not row:
                return None
            payload_json, expires_at = row
            if now >= float(expires_at):
                self._conn.execute("DELETE FROM response_cache WHERE cache_key = ?", [cache_key])
                return None
        return json.loads(payload_json)

    def set_cached_response(
        self, cache_key: str, source: str, payload: Any, ttl_seconds: int | None = None
    ) -> None:
        """Guarda una respuesta en DuckDB con su TTL."""
        ttl = ttl_seconds if ttl_seconds is not None else settings.response_cache_ttl_seconds
        if ttl <= 0:
            return
        now = time.time()
        expires_at = now + ttl
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO response_cache (cache_key, source, payload_json, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (cache_key) DO UPDATE SET
                    source = excluded.source,
                    payload_json = excluded.payload_json,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                [cache_key, source, payload_json, now, expires_at],
            )

    def cache_stats(self) -> dict[str, Any]:
        """Devuelve estadísticas actuales de caché en DuckDB."""
        now = time.time()
        with self._lock:
            resp_active = self._conn.execute(
                "SELECT COUNT(*) FROM response_cache WHERE expires_at > ?", [now]
            ).fetchone()[0]
            catalogs = self._conn.execute(
                "SELECT COUNT(DISTINCT namespace) FROM catalog_cache"
            ).fetchone()[0]
            tables = [r[0] for r in self._conn.execute("SHOW TABLES").fetchall()]
        return {
            "response_cache_entries": int(resp_active),
            "cached_catalogs": int(catalogs),
            "renfe_gtfs_loaded": "renfe_stop_times" in tables,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_store_instance: DuckDBStore | None = None
_store_lock = threading.Lock()


def get_store() -> DuckDBStore:
    """Devuelve la instancia singleton de DuckDBStore."""
    global _store_instance
    if _store_instance is None:
        with _store_lock:
            if _store_instance is None:
                _store_instance = DuckDBStore()
    return _store_instance
