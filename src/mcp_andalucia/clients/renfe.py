"""Cliente para horarios GTFS (AVE/LD/MD) y tiempo real de Renfe usando DuckDB + NDJSON."""

from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from mcp_andalucia.config import settings
from mcp_andalucia.db import DuckDBStore, get_store
from mcp_andalucia.logging_config import get_logger
from mcp_andalucia.utils import normalize_text

logger = get_logger("clients.renfe")

GTFS_TABLES = (
    "stops",
    "routes",
    "trips",
    "stop_times",
    "calendar",
    "calendar_dates",
)

WEEKDAY_COLUMNS = {
    0: "monday",
    1: "tuesday",
    2: "wednesday",
    3: "thursday",
    4: "friday",
    5: "saturday",
    6: "sunday",
}


class RenfeClient:
    """Gestiona la descarga del GTFS AVE/LD/MD de Renfe, su carga en DuckDB vía NDJSON y consultas SQL."""

    def __init__(self, store: DuckDBStore | None = None) -> None:
        self.gtfs_url = settings.renfe_gtfs_url
        self.rt_url = settings.renfe_rt_trips_url
        self.store = store or get_store()

    async def ensure_gtfs_loaded(self, force_refresh: bool = False) -> dict[str, int]:
        """Descarga el GTFS de Renfe si expiró el TTL, lo exporta a NDJSON y lo carga en DuckDB."""
        cache_key = "renfe:gtfs_av_ld_md"
        if not force_refresh and self.store.is_fresh(cache_key, settings.renfe_gtfs_ttl_seconds):
            try:
                cnt = self.store.execute("SELECT COUNT(*) FROM renfe_stops")[0][0]
                if cnt > 0:
                    return {"stops": int(cnt)}
            except Exception:
                pass

        t0 = time.perf_counter()
        logger.info("Descargando GTFS de Renfe AV/LD/MD desde %s", self.gtfs_url)
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(self.gtfs_url)
            resp.raise_for_status()
            zip_bytes = resp.content
        logger.info("GTFS de Renfe descargado: %.2f MB (%.1f ms)", len(zip_bytes) / 1048576, (time.perf_counter() - t0) * 1000)

        counts: dict[str, int] = {}
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            available = set(zf.namelist())
            for table in GTFS_TABLES:
                filename = f"{table}.txt"
                if filename not in available:
                    continue
                ndjson_path = settings.ndjson_dir / f"renfe_{table}.ndjson"
                with zf.open(filename) as raw_file:
                    text_stream = io.TextIOWrapper(raw_file, encoding="utf-8-sig", errors="replace")
                    reader = csv.DictReader(text_stream)
                    self._write_gtfs_ndjson(reader, ndjson_path, add_norm=(table == "stops"))
                counts[table] = self.store.load_ndjson_table(f"renfe_{table}", ndjson_path)

        self.store.mark_fresh(cache_key, counts.get("stops", 0))
        return counts

    def _write_gtfs_ndjson(
        self, reader: csv.DictReader, out_path: Path, add_norm: bool = False
    ) -> None:
        with out_path.open("w", encoding="utf-8") as f:
            for row in reader:
                clean_row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
                if add_norm and "stop_name" in clean_row:
                    clean_row["norm_name"] = normalize_text(clean_row["stop_name"])
                f.write(json.dumps(clean_row, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _find_matching_stops(self, query: str) -> list[dict[str, Any]]:
        q_norm = normalize_text(query)
        if not q_norm:
            return []
        rows = self.store.execute_dicts(
            """
            SELECT stop_id, stop_name, stop_lat, stop_lon, norm_name
            FROM renfe_stops
            WHERE norm_name LIKE ?
            ORDER BY LENGTH(stop_name) ASC
            LIMIT 15
            """,
            [f"%{q_norm}%"],
        )
        return rows

    async def get_realtime_delays(self) -> dict[str, int]:
        """Obtiene retrasos en segundos por trip_id desde el feed JSON en tiempo real de Renfe LD."""
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(self.rt_url)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            return {}

        delays: dict[str, int] = {}
        for entity in data.get("entity", []):
            tu = entity.get("tripUpdate") or entity.get("trip_update") or {}
            trip_id = (tu.get("trip") or {}).get("tripId") or (tu.get("trip") or {}).get("trip_id")
            if not trip_id:
                continue
            stop_updates = tu.get("stopTimeUpdate") or tu.get("stop_time_update") or []
            for stu in stop_updates:
                dep = stu.get("departure") or stu.get("arrival") or {}
                if "delay" in dep:
                    delays[str(trip_id)] = int(dep["delay"])
                    break
        return delays

    async def search_trains(
        self,
        origen: str,
        destino: str,
        fecha: str | None = None,
        limite: int = 15,
    ) -> dict[str, Any]:
        """Busca trenes directos AVE/LD/MD entre origen y destino usando DuckDB."""
        await self.ensure_gtfs_loaded()

        orig_stops = self._find_matching_stops(origen)
        dest_stops = self._find_matching_stops(destino)

        if not orig_stops or not dest_stops:
            return {
                "cobertura": False,
                "origen_encontrado": [s["stop_name"] for s in orig_stops[:3]],
                "destino_encontrado": [s["stop_name"] for s in dest_stops[:3]],
                "mensaje": f"No se encontraron estaciones de Renfe AVE/LD/MD para '{origen}' o '{destino}'.",
                "trenes": [],
            }

        orig_ids = [str(s["stop_id"]) for s in orig_stops[:5]]
        dest_ids = [str(s["stop_id"]) for s in dest_stops[:5]]

        orig_ph = ",".join(["?"] * len(orig_ids))
        dest_ph = ",".join(["?"] * len(dest_ids))

        # Inspeccionar columnas reales en renfe_trips y renfe_routes para evitar errores si faltan opcionales
        trip_cols = {r[0] for r in self.store.execute("DESCRIBE renfe_trips")}
        route_cols = {r[0] for r in self.store.execute("DESCRIBE renfe_routes")}
        existing_tables = {r[0] for r in self.store.execute("SHOW TABLES")}

        route_expr_parts = [f"r.{c}" for c in ("route_long_name", "route_short_name") if c in route_cols]
        route_expr = f"COALESCE({', '.join(route_expr_parts)}, '')" if route_expr_parts else "''"

        trip_expr_parts = [f"t.{c}" for c in ("trip_short_name", "trip_headsign") if c in trip_cols]
        trip_expr = f"COALESCE({', '.join(trip_expr_parts)}, '')" if trip_expr_parts else "''"

        date_filter_sql = ""
        date_params: list[Any] = []
        if fecha and ("renfe_calendar" in existing_tables or "renfe_calendar_dates" in existing_tables):
            try:
                dt = datetime.strptime(fecha.strip(), "%Y-%m-%d")
                ymd = dt.strftime("%Y%m%d")
                weekday_col = WEEKDAY_COLUMNS[dt.weekday()]
                or_clauses: list[str] = []
                if "renfe_calendar" in existing_tables:
                    or_clauses.append(
                        f"""
                        EXISTS (
                            SELECT 1 FROM renfe_calendar c
                            WHERE CAST(c.service_id AS VARCHAR) = CAST(t.service_id AS VARCHAR)
                              AND CAST(c.start_date AS VARCHAR) <= ?
                              AND CAST(c.end_date AS VARCHAR) >= ?
                              AND CAST(c.{weekday_col} AS INTEGER) = 1
                        )
                        """
                    )
                    date_params.extend([ymd, ymd])
                if "renfe_calendar_dates" in existing_tables:
                    or_clauses.append(
                        """
                        EXISTS (
                            SELECT 1 FROM renfe_calendar_dates cd
                            WHERE CAST(cd.service_id AS VARCHAR) = CAST(t.service_id AS VARCHAR)
                              AND CAST(cd.date AS VARCHAR) = ?
                              AND CAST(cd.exception_type AS INTEGER) = 1
                        )
                        """
                    )
                    date_params.append(ymd)

                if or_clauses:
                    date_filter_sql += f" AND ({' OR '.join(or_clauses)})"
                if "renfe_calendar_dates" in existing_tables:
                    date_filter_sql += """
                    AND NOT EXISTS (
                        SELECT 1 FROM renfe_calendar_dates cd_ex
                        WHERE CAST(cd_ex.service_id AS VARCHAR) = CAST(t.service_id AS VARCHAR)
                          AND CAST(cd_ex.date AS VARCHAR) = ?
                          AND CAST(cd_ex.exception_type AS INTEGER) = 2
                    )
                    """
                    date_params.append(ymd)
            except ValueError:
                pass

        sql = f"""
        SELECT
            t.trip_id,
            {route_expr} AS linea,
            {trip_expr} AS servicio,
            so.stop_name AS estacion_origen,
            sd.stop_name AS estacion_destino,
            CAST(sto.departure_time AS VARCHAR) AS hora_salida,
            CAST(std.arrival_time AS VARCHAR) AS hora_llegada
        FROM renfe_stop_times sto
        JOIN renfe_stop_times std
          ON CAST(sto.trip_id AS VARCHAR) = CAST(std.trip_id AS VARCHAR)
         AND CAST(sto.stop_sequence AS INTEGER) < CAST(std.stop_sequence AS INTEGER)
        JOIN renfe_trips t
          ON CAST(t.trip_id AS VARCHAR) = CAST(sto.trip_id AS VARCHAR)
        LEFT JOIN renfe_routes r
          ON CAST(r.route_id AS VARCHAR) = CAST(t.route_id AS VARCHAR)
        JOIN renfe_stops so
          ON CAST(so.stop_id AS VARCHAR) = CAST(sto.stop_id AS VARCHAR)
        JOIN renfe_stops sd
          ON CAST(sd.stop_id AS VARCHAR) = CAST(std.stop_id AS VARCHAR)
        WHERE CAST(sto.stop_id AS VARCHAR) IN ({orig_ph})
          AND CAST(std.stop_id AS VARCHAR) IN ({dest_ph})
          {date_filter_sql}
        ORDER BY sto.departure_time ASC
        LIMIT ?
        """
        params = orig_ids + dest_ids + date_params + [max(1, min(limite, 50))]
        rows = self.store.execute_dicts(sql, params)

        # Enriquecer con retrasos en tiempo real si la fecha es hoy o no se especificó
        delays: dict[str, int] = {}
        today_str = datetime.now().strftime("%Y-%m-%d")
        if rows and (fecha is None or fecha == today_str):
            delays = await self.get_realtime_delays()

        trenes: list[dict[str, Any]] = []
        for row in rows:
            item: dict[str, Any] = {
                "linea": row["linea"] or row["servicio"],
                "estacion_origen": row["estacion_origen"],
                "hora_salida": row["hora_salida"],
                "estacion_destino": row["estacion_destino"],
                "hora_llegada": row["hora_llegada"],
            }
            trip_id = str(row["trip_id"])
            if trip_id in delays:
                item["retraso_min"] = round(delays[trip_id] / 60)
            trenes.append(item)

        return {
            "cobertura": True,
            "fecha": fecha or "todos los servicios (sin filtro de fecha)",
            "total_encontrados": len(trenes),
            "trenes": trenes,
        }
