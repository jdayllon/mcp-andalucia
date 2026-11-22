"""Cliente unificado de transporte urbano (TUSSAM Sevilla, EMT Málaga y NAP Ministerio de Transportes)
con motor espacial Bounding Box + Haversine y escalera de geocodificación en 3 niveles sobre DuckDB + NDJSON.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from mcp_andalucia.config import settings
from mcp_andalucia.db import DuckDBStore, get_store
from mcp_andalucia.logging_config import get_logger
from mcp_andalucia.utils import bounding_box, haversine_m, normalize_text

logger = get_logger("clients.urbano")

# Datasets GTFS urbanos preconfigurados en el NAP para Andalucía (ficheroId oficial)
NAP_CITY_PRESETS: dict[str, int] = {
    "cordoba": 1573,       # Autobús urbano de Córdoba (AUCORSA)
    "granada": 1564,       # Autobús urbano de Granada
    "metro_sevilla": 1583, # Metro de Sevilla
    "metro_malaga": 1497,  # Metro de Málaga
    "metro_granada": 1568, # Metro de Granada
}

ANDALUSIA_KEYWORDS = (
    "andaluc",
    "almer",
    "cadiz",
    "cordoba",
    "granada",
    "huelva",
    "jaen",
    "malaga",
    "sevilla",
    "aucorsa",
    "tussam",
    "jerez",
    "marbella",
    "chiclana",
    "algeciras",
)


class UrbanoClient:
    """Gestiona redes de autobús/metro urbano en DuckDB + NDJSON, cercanía Haversine y tiempo real."""

    def __init__(self, store: DuckDBStore | None = None) -> None:
        self.store = store or get_store()

    def _normalize_city(self, ciudad: str) -> str:
        c = normalize_text(ciudad).replace(" ", "_")
        aliases = {
            "tussam": "sevilla",
            "emt_malaga": "malaga",
            "emt": "malaga",
            "aucorsa": "cordoba",
        }
        return aliases.get(c, c)

    async def ensure_city_loaded(self, ciudad: str, force_refresh: bool = False) -> int:
        """Asegura que las paradas y líneas de una ciudad estén indexadas en DuckDB vía NDJSON."""
        city = self._normalize_city(ciudad)
        cache_key = f"urbano:city:{city}"
        if not force_refresh and self.store.is_fresh(cache_key, settings.catalog_ttl_seconds):
            cnt = self.store.execute(
                "SELECT COUNT(*) FROM urban_stops WHERE ciudad = ?", [city]
            )[0][0]
            if cnt > 0:
                return int(cnt)

        if city == "sevilla":
            return await self._load_tussam_sevilla(cache_key)
        if city == "malaga":
            return await self._load_emt_malaga(cache_key)
        if city in NAP_CITY_PRESETS:
            return await self.load_nap_gtfs(city, NAP_CITY_PRESETS[city])

        # Comprobar si ya fue cargada manualmente desde el NAP con ese nombre
        cnt = self.store.execute("SELECT COUNT(*) FROM urban_stops WHERE ciudad = ?", [city])[0][0]
        return int(cnt)

    def _save_city_ndjson_and_duckdb(
        self,
        city: str,
        stops: list[dict[str, Any]],
        line_stops: list[dict[str, Any]],
        cache_key: str,
    ) -> int:
        """Escribe los ficheros NDJSON de una ciudad y actualiza las tablas `urban_stops` y `urban_line_stops`."""
        stops_path = settings.ndjson_dir / f"urban_stops_{city}.ndjson"
        lines_path = settings.ndjson_dir / f"urban_line_stops_{city}.ndjson"

        with stops_path.open("w", encoding="utf-8") as f:
            for s in stops:
                f.write(json.dumps(s, ensure_ascii=False, separators=(",", ":")) + "\n")

        with lines_path.open("w", encoding="utf-8") as f:
            for ls in line_stops:
                f.write(json.dumps(ls, ensure_ascii=False, separators=(",", ":")) + "\n")

        with self.store._lock:
            self.store._conn.execute("DELETE FROM urban_stops WHERE ciudad = ?", [city])
            self.store._conn.execute("DELETE FROM urban_line_stops WHERE ciudad = ?", [city])
            if stops:
                self.store._conn.execute(
                    """
                    INSERT INTO urban_stops (ciudad, codigo, nombre, direccion, cp, lat, lon, lineas, norm_search)
                    SELECT ciudad, CAST(codigo AS VARCHAR), nombre, direccion, CAST(cp AS VARCHAR),
                           CAST(lat AS DOUBLE), CAST(lon AS DOUBLE), lineas, norm_search
                    FROM read_ndjson_auto(?)
                    """,
                    [str(stops_path)],
                )
            if line_stops:
                self.store._conn.execute(
                    """
                    INSERT INTO urban_line_stops (
                        ciudad, linea, nombre_linea, sentido, orden,
                        codigo_parada, nombre_parada, direccion, lat, lon
                    )
                    SELECT ciudad, CAST(linea AS VARCHAR), nombre_linea, CAST(sentido AS VARCHAR),
                           CAST(orden AS INTEGER), CAST(codigo_parada AS VARCHAR), nombre_parada,
                           direccion, CAST(lat AS DOUBLE), CAST(lon AS DOUBLE)
                    FROM read_ndjson_auto(?)
                    """,
                    [str(lines_path)],
                )
        self.store.mark_fresh(cache_key, len(stops))
        logger.info(
            "Urbano '%s' cargado en DuckDB vía NDJSON: %d paradas, %d paradas-línea",
            city,
            len(stops),
            len(line_stops),
        )
        return len(stops)

    async def _load_tussam_sevilla(self, cache_key: str) -> int:
        """Descarga `tussam.db` de `686f6c61/API-TUSSAM`, lo convierte a NDJSON y lo indexa en DuckDB."""
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(settings.tussam_db_url)
            resp.raise_for_status()
            db_bytes = resp.content
        logger.info("Descargado tussam.db (%d bytes, %.1f ms)", len(db_bytes), (time.perf_counter() - t0) * 1000)

        with tempfile.NamedTemporaryFile(suffix=".db", delete=True) as tmp:
            tmp.write(db_bytes)
            tmp.flush()
            conn = sqlite3.connect(tmp.name)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            paradas_rows = cur.execute(
                "SELECT codigo, nombre, latitud, longitud, calle, numero, codigo_postal, direccion_completa FROM paradas"
            ).fetchall()
            lineas_map = {
                str(r["numero"]): str(r["nombre"])
                for r in cur.execute("SELECT numero, nombre FROM lineas").fetchall()
            }
            rel_rows = cur.execute(
                "SELECT parada_codigo, linea_numero, sentido, orden FROM paradas_lineas ORDER BY linea_numero, sentido, orden"
            ).fetchall()
            conn.close()

        parada_dict: dict[str, dict[str, Any]] = {}
        lineas_por_parada: dict[str, list[str]] = {}
        for rel in rel_rows:
            p_cod = str(rel["parada_codigo"])
            l_num = str(rel["linea_numero"])
            lst = lineas_por_parada.setdefault(p_cod, [])
            if l_num not in lst:
                lst.append(l_num)

        stops: list[dict[str, Any]] = []
        for r in paradas_rows:
            cod = str(r["codigo"])
            nombre = (r["nombre"] or "").strip()
            direccion = (r["direccion_completa"] or r["calle"] or nombre).strip()
            cp = str(r["codigo_postal"] or "").strip()
            lat = float(r["latitud"])
            lon = float(r["longitud"])
            lineas_str = ",".join(lineas_por_parada.get(cod, []))
            item = {
                "ciudad": "sevilla",
                "codigo": cod,
                "nombre": nombre,
                "direccion": direccion,
                "cp": cp,
                "lat": lat,
                "lon": lon,
                "lineas": lineas_str,
                "norm_search": normalize_text(f"{cod} {nombre} {direccion} {cp}"),
            }
            stops.append(item)
            parada_dict[cod] = item

        line_stops: list[dict[str, Any]] = []
        for rel in rel_rows:
            p_cod = str(rel["parada_codigo"])
            l_num = str(rel["linea_numero"])
            p_info = parada_dict.get(p_cod)
            if not p_info:
                continue
            line_stops.append(
                {
                    "ciudad": "sevilla",
                    "linea": l_num,
                    "nombre_linea": lineas_map.get(l_num, l_num),
                    "sentido": str(rel["sentido"]),
                    "orden": int(rel["orden"]),
                    "codigo_parada": p_cod,
                    "nombre_parada": p_info["nombre"],
                    "direccion": p_info["direccion"],
                    "lat": p_info["lat"],
                    "lon": p_info["lon"],
                }
            )

        return self._save_city_ndjson_and_duckdb("sevilla", stops, line_stops, cache_key)

    async def _load_emt_malaga(self, cache_key: str) -> int:
        """Descarga `lineasyparadas.csv` de EMT Málaga, lo convierte a NDJSON y lo indexa en DuckDB."""
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(settings.emt_malaga_lineas_paradas_url)
            resp.raise_for_status()
            csv_text = resp.text
        logger.info("Descargado EMT Málaga lineasyparadas.csv (%.1f ms)", (time.perf_counter() - t0) * 1000)

        reader = csv.DictReader(io.StringIO(csv_text))
        stops_map: dict[str, dict[str, Any]] = {}
        lineas_por_parada: dict[str, list[str]] = {}
        line_stops: list[dict[str, Any]] = []

        for row in reader:
            cod_parada = (row.get("codParada") or "").strip()
            linea = (row.get("userCodLinea") or row.get("codLineaStr") or row.get("codLinea") or "").strip()
            if not cod_parada or not linea:
                continue
            try:
                lat = float(row.get("lat") or 0)
                lon = float(row.get("lon") or 0)
            except ValueError:
                continue
            if lat == 0.0 or lon == 0.0:
                continue

            nombre_parada = (row.get("nombreParada") or "").strip()
            direccion = (row.get("direccion") or nombre_parada).strip()
            nombre_linea = (row.get("nombreLinea") or linea).strip()
            sentido = str(row.get("sentido") or "1").strip()
            try:
                orden = int(float(row.get("orden") or 0))
            except ValueError:
                orden = 0

            lst = lineas_por_parada.setdefault(cod_parada, [])
            if linea not in lst:
                lst.append(linea)

            if cod_parada not in stops_map:
                stops_map[cod_parada] = {
                    "ciudad": "malaga",
                    "codigo": cod_parada,
                    "nombre": nombre_parada,
                    "direccion": direccion,
                    "cp": "",
                    "lat": lat,
                    "lon": lon,
                }

            line_stops.append(
                {
                    "ciudad": "malaga",
                    "linea": linea,
                    "nombre_linea": nombre_linea,
                    "sentido": sentido,
                    "orden": orden,
                    "codigo_parada": cod_parada,
                    "nombre_parada": nombre_parada,
                    "direccion": direccion,
                    "lat": lat,
                    "lon": lon,
                }
            )

        stops: list[dict[str, Any]] = []
        for cod, s in stops_map.items():
            s["lineas"] = ",".join(lineas_por_parada.get(cod, []))
            s["norm_search"] = normalize_text(f"{cod} {s['nombre']} {s['direccion']}")
            stops.append(s)

        return self._save_city_ndjson_and_duckdb("malaga", stops, line_stops, cache_key)

    async def load_nap_gtfs(self, ciudad: str, fichero_id: int) -> int:
        """Descarga un fichero GTFS-ZIP del NAP usando `ApiKey` y lo indexa en DuckDB + NDJSON."""
        if not settings.nap_api_key:
            raise ValueError(
                "Falta configurar MCP_ANDALUCIA_NAP_API_KEY para descargar conjuntos GTFS del NAP."
            )

        city = self._normalize_city(ciudad)
        url = f"{settings.nap_base_url.rstrip('/')}/Fichero/download/{fichero_id}"
        t0 = time.perf_counter()
        async with httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=True,
            headers={"ApiKey": settings.nap_api_key, "User-Agent": "mcp-andalucia/0.2"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            zip_bytes = resp.content
        logger.info(
            "Descargado GTFS NAP ficheroId=%d para '%s' (%d bytes, %.1f ms)",
            fichero_id,
            city,
            len(zip_bytes),
            (time.perf_counter() - t0) * 1000,
        )

        stops_raw: dict[str, dict[str, Any]] = {}
        routes_map: dict[str, tuple[str, str]] = {}
        rep_trip_for_route_dir: dict[tuple[str, str], str] = {}
        trip_to_route_dir: dict[str, tuple[str, str]] = {}

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            if "stops.txt" in names:
                with zf.open("stops.txt") as f:
                    for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace")):
                        sid = (row.get("stop_id") or "").strip()
                        try:
                            lat = float(row.get("stop_lat") or 0)
                            lon = float(row.get("stop_lon") or 0)
                        except ValueError:
                            continue
                        if not sid or lat == 0.0 or lon == 0.0:
                            continue
                        sname = (row.get("stop_name") or sid).strip()
                        sdesc = (row.get("stop_desc") or sname).strip()
                        stops_raw[sid] = {
                            "ciudad": city,
                            "codigo": sid,
                            "nombre": sname,
                            "direccion": sdesc,
                            "cp": "",
                            "lat": lat,
                            "lon": lon,
                        }

            if "routes.txt" in names:
                with zf.open("routes.txt") as f:
                    for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace")):
                        rid = (row.get("route_id") or "").strip()
                        short = (row.get("route_short_name") or rid).strip()
                        long_n = (row.get("route_long_name") or short).strip()
                        if rid:
                            routes_map[rid] = (short, long_n)

            if "trips.txt" in names:
                with zf.open("trips.txt") as f:
                    for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace")):
                        tid = (row.get("trip_id") or "").strip()
                        rid = (row.get("route_id") or "").strip()
                        d_id = str(row.get("direction_id") or "1").strip()
                        if tid and rid:
                            key = (rid, d_id)
                            if key not in rep_trip_for_route_dir:
                                rep_trip_for_route_dir[key] = tid
                                trip_to_route_dir[tid] = key

            lineas_por_parada: dict[str, list[str]] = {}
            line_stops: list[dict[str, Any]] = []
            if "stop_times.txt" in names and trip_to_route_dir:
                with zf.open("stop_times.txt") as f:
                    for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace")):
                        tid = (row.get("trip_id") or "").strip()
                        if tid not in trip_to_route_dir:
                            continue
                        sid = (row.get("stop_id") or "").strip()
                        s_info = stops_raw.get(sid)
                        if not s_info:
                            continue
                        rid, d_id = trip_to_route_dir[tid]
                        short, long_n = routes_map.get(rid, (rid, rid))
                        try:
                            seq = int(float(row.get("stop_sequence") or 0))
                        except ValueError:
                            seq = 0

                        lst = lineas_por_parada.setdefault(sid, [])
                        if short not in lst:
                            lst.append(short)

                        line_stops.append(
                            {
                                "ciudad": city,
                                "linea": short,
                                "nombre_linea": long_n,
                                "sentido": d_id,
                                "orden": seq,
                                "codigo_parada": sid,
                                "nombre_parada": s_info["nombre"],
                                "direccion": s_info["direccion"],
                                "lat": s_info["lat"],
                                "lon": s_info["lon"],
                            }
                        )

        stops: list[dict[str, Any]] = []
        for sid, s in stops_raw.items():
            s["lineas"] = ",".join(lineas_por_parada.get(sid, []))
            s["norm_search"] = normalize_text(f"{sid} {s['nombre']} {s['direccion']}")
            stops.append(s)

        return self._save_city_ndjson_and_duckdb(city, stops, line_stops, f"urbano:city:{city}")

    async def resolve_location(
        self,
        ciudad: str,
        lat: float | None = None,
        lon: float | None = None,
        direccion_o_lugar: str | None = None,
        codigo_postal: str | None = None,
    ) -> dict[str, Any]:
        """Escalera de resolución geográfica en 3 niveles:
        1. Coordenadas directas (`lat`, `lon`).
        2. Callejero local de paradas en DuckDB (por calle/nombre o código postal).
        3. Geocodificación bajo demanda en Nominatim OpenStreetMap con caché persistente en DuckDB.
        """
        city = self._normalize_city(ciudad)
        if lat is not None and lon is not None:
            return {
                "lat": float(lat),
                "lon": float(lon),
                "precision": "coordenadas_exactas",
                "referencia": f"({lat:.5f}, {lon:.5f})",
            }

        await self.ensure_city_loaded(city)

        # Nivel 2: Callejero de paradas en DuckDB
        if direccion_o_lugar:
            q_norm = normalize_text(direccion_o_lugar)
            # Limpiar prefijos típicos de vías en OpenRTA ("CL ", "C/ ", "AV ", "PZ ")
            for prefix in ("calle ", "cl ", "c/ ", "avenida ", "avda ", "av ", "plaza ", "pz ", "paseo "):
                if q_norm.startswith(prefix):
                    q_norm = q_norm[len(prefix) :].strip()
            if q_norm:
                rows = self.store.execute_dicts(
                    """
                    SELECT AVG(lat) AS lat, AVG(lon) AS lon, COUNT(*) AS n, MIN(direccion) AS ejemplo
                    FROM urban_stops
                    WHERE ciudad = ? AND norm_search LIKE ?
                    """,
                    [city, f"%{q_norm}%"],
                )
                if rows and rows[0]["n"] and int(rows[0]["n"]) > 0:
                    return {
                        "lat": float(rows[0]["lat"]),
                        "lon": float(rows[0]["lon"]),
                        "precision": "callejero_duckdb",
                        "referencia": f"{rows[0]['ejemplo']} ({rows[0]['n']} paradas coincidentes)",
                    }

        if codigo_postal:
            cp_clean = codigo_postal.strip()
            rows = self.store.execute_dicts(
                """
                SELECT AVG(lat) AS lat, AVG(lon) AS lon, COUNT(*) AS n
                FROM urban_stops
                WHERE ciudad = ? AND cp = ?
                """,
                [city, cp_clean],
            )
            if rows and rows[0]["n"] and int(rows[0]["n"]) > 0:
                return {
                    "lat": float(rows[0]["lat"]),
                    "lon": float(rows[0]["lon"]),
                    "precision": "codigo_postal_duckdb",
                    "referencia": f"CP {cp_clean}",
                }

        # Nivel 3: Geocodificación Nominatim cacheada en DuckDB
        query_parts = [p for p in (direccion_o_lugar, codigo_postal, ciudad, "Andalucía, España") if p]
        full_query = ", ".join(query_parts)
        q_key = normalize_text(full_query)

        cached_geo = self.store.execute_dicts(
            "SELECT lat, lon, display_name, precision FROM geocoding_cache WHERE query_norm = ?",
            [q_key],
        )
        if cached_geo:
            g = cached_geo[0]
            return {
                "lat": float(g["lat"]),
                "lon": float(g["lon"]),
                "precision": g["precision"],
                "referencia": g["display_name"],
            }

        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                headers={"User-Agent": "mcp-andalucia/0.2 (+https://github.com/jdayllon/mcp-andalucia)"},
            ) as client:
                resp = await client.get(
                    settings.nominatim_url,
                    params={"q": full_query, "format": "json", "limit": 1, "countrycodes": "es"},
                )
                if resp.status_code == 200 and resp.json():
                    hit = resp.json()[0]
                    g_lat = float(hit["lat"])
                    g_lon = float(hit["lon"])
                    disp = hit.get("display_name", full_query)
                    prec = "geocodificado_osm_direccion" if direccion_o_lugar else "geocodificado_osm_municipio"
                    with self.store._lock:
                        self.store._conn.execute(
                            """
                            INSERT INTO geocoding_cache (query_norm, lat, lon, display_name, precision, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT (query_norm) DO UPDATE SET
                                lat = excluded.lat, lon = excluded.lon, updated_at = excluded.updated_at
                            """,
                            [q_key, g_lat, g_lon, disp, prec, time.time()],
                        )
                    return {"lat": g_lat, "lon": g_lon, "precision": prec, "referencia": disp}
        except Exception as exc:
            logger.warning("Fallo en geocodificación Nominatim para '%s': %s", full_query, exc)

        raise ValueError(
            f"No se pudo determinar la ubicación en '{ciudad}'. Proporciona `lat` y `lon` o una calle/parada válida."
        )

    async def find_nearby_stops(
        self,
        ciudad: str,
        lat: float | None = None,
        lon: float | None = None,
        direccion_o_lugar: str | None = None,
        codigo_postal: str | None = None,
        radio_metros: int = 400,
        limite: int = 8,
        incluir_tiempo_real: bool = True,
    ) -> dict[str, Any]:
        """Busca paradas cercanas combinando Bounding Box + distancia Haversine en SQL dentro de DuckDB."""
        city = self._normalize_city(ciudad)
        await self.ensure_city_loaded(city)

        loc = await self.resolve_location(
            ciudad=city,
            lat=lat,
            lon=lon,
            direccion_o_lugar=direccion_o_lugar,
            codigo_postal=codigo_postal,
        )
        lat0, lon0 = loc["lat"], loc["lon"]
        radio = max(50, min(radio_metros, 5000))
        lat_min, lat_max, lon_min, lon_max = bounding_box(lat0, lon0, radio * 1.15)

        sql = """
        SELECT
            codigo,
            nombre,
            direccion,
            cp,
            lat,
            lon,
            lineas,
            CAST(ROUND(
                6371000.0 * 2.0 * ASIN(LEAST(1.0, SQRT(
                    POWER(SIN(RADIANS(lat - ?) / 2.0), 2) +
                    COS(RADIANS(?)) * COS(RADIANS(lat)) *
                    POWER(SIN(RADIANS(lon - ?) / 2.0), 2)
                )))
            ) AS INTEGER) AS distancia_metros
        FROM urban_stops
        WHERE ciudad = ?
          AND lat BETWEEN ? AND ?
          AND lon BETWEEN ? AND ?
        """
        rows = self.store.execute_dicts(
            sql,
            [lat0, lat0, lon0, city, lat_min, lat_max, lon_min, lon_max],
        )
        filtered = [r for r in rows if int(r["distancia_metros"]) <= radio]
        filtered.sort(key=lambda r: int(r["distancia_metros"]))
        paradas = filtered[: max(1, min(limite, 25))]

        if incluir_tiempo_real and paradas and city in ("sevilla", "malaga"):
            top_paradas = paradas[:3]
            rt_tasks = [self.get_realtime_stop(city, str(p["codigo"])) for p in top_paradas]
            rt_results = await asyncio.gather(*rt_tasks, return_exceptions=True)
            for p, rt in zip(top_paradas, rt_results):
                if isinstance(rt, dict) and "estimaciones" in rt:
                    p["tiempo_real"] = rt["estimaciones"][:5]

        return {
            "ciudad": city,
            "ubicacion_resuelta": loc,
            "radio_metros": radio,
            "total_en_radio": len(filtered),
            "paradas": paradas,
        }

    async def get_realtime_stop(
        self,
        ciudad: str,
        codigo_parada: str,
        linea: str | None = None,
    ) -> dict[str, Any]:
        """Consulta tiempos de llegada o posición de autobuses en tiempo real para Sevilla (TUSSAM) o Málaga (EMT)."""
        city = self._normalize_city(ciudad)
        if city == "sevilla":
            return await self._get_realtime_sevilla(codigo_parada, linea)
        if city == "malaga":
            return await self._get_realtime_malaga(codigo_parada, linea)
        return {
            "ciudad": city,
            "codigo_parada": codigo_parada,
            "disponible": False,
            "mensaje": f"El tiempo real está disponible para 'sevilla' (TUSSAM) y 'malaga' (EMT). Para '{city}' usa los horarios y recorridos GTFS.",
            "estimaciones": [],
        }

    async def _get_realtime_sevilla(self, codigo_parada: str, linea_filtro: str | None = None) -> dict[str, Any]:
        cache_key = f"tussam:rt:{codigo_parada}"
        cached = self.store.get_cached_response(cache_key)
        if cached is not None:
            estimaciones = cached.get("estimaciones", [])
            if linea_filtro:
                estimaciones = [e for e in estimaciones if normalize_text(e.get("linea")) == normalize_text(linea_filtro)]
            return {**cached, "estimaciones": estimaciones}

        url = (
            f"{settings.tussam_proxy_url.rstrip('/')}/paradas/{codigo_parada}/tiempos"
            if settings.tussam_proxy_url
            else f"{settings.tussam_base_url.rstrip('/')}/API/infotus-ui/tiempos/{codigo_parada}"
        )
        try:
            async with httpx.AsyncClient(
                timeout=12.0,
                follow_redirects=False,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
                    "Accept": "application/json",
                    "Referer": "https://reddelineas.tussam.es/",
                },
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("TUSSAM tiempo real no disponible para parada %s: %s", codigo_parada, exc)
            return {
                "ciudad": "sevilla",
                "codigo_parada": codigo_parada,
                "disponible": False,
                "mensaje": f"No se pudo conectar con tiempo real de TUSSAM ({exc}).",
                "estimaciones": [],
            }

        # Si viene de API-TUSSAM proxy directo
        if "tiempos" in data and isinstance(data["tiempos"], list):
            estimaciones = data["tiempos"]
        else:
            res = data.get("result") or {}
            if isinstance(res, list):
                res = res[0] if res else {}
            estimaciones = []
            for lin in res.get("lineasCoincidentes", []):
                label = str(lin.get("labelLinea", ""))
                for est in lin.get("estimaciones", []):
                    seg = est.get("segundos")
                    if seg is None:
                        continue
                    estimaciones.append(
                        {
                            "linea": label,
                            "tiempo_minutos": max(0, int(seg) // 60),
                            "destino": (est.get("destino") or {}).get("texto", ""),
                            "distancia_metros": est.get("distancia"),
                            "vehiculo": est.get("vehiculo"),
                        }
                    )
            estimaciones.sort(key=lambda x: x["tiempo_minutos"])

        payload = {
            "ciudad": "sevilla",
            "codigo_parada": codigo_parada,
            "disponible": True,
            "estimaciones": estimaciones,
        }
        self.store.set_cached_response(cache_key, "tussam_rt", payload, ttl_seconds=60)
        if linea_filtro:
            estimaciones = [e for e in estimaciones if normalize_text(e.get("linea")) == normalize_text(linea_filtro)]
        return {**payload, "estimaciones": estimaciones}

    async def _get_realtime_malaga(self, codigo_parada: str, linea_filtro: str | None = None) -> dict[str, Any]:
        await self.ensure_city_loaded("malaga")
        stop_rows = self.store.execute_dicts(
            "SELECT codigo, nombre, lat, lon, lineas FROM urban_stops WHERE ciudad = 'malaga' AND codigo = ?",
            [str(codigo_parada)],
        )
        if not stop_rows:
            return {
                "ciudad": "malaga",
                "codigo_parada": codigo_parada,
                "disponible": False,
                "mensaje": f"No existe la parada '{codigo_parada}' en EMT Málaga.",
                "estimaciones": [],
            }

        stop = stop_rows[0]
        target_lines = {l.strip() for l in (stop.get("lineas") or "").split(",") if l.strip()}
        if linea_filtro:
            target_lines = {linea_filtro.strip()}

        cache_key = "emt_malaga:ubicaciones_rt"
        features = self.store.get_cached_response(cache_key)
        if features is None:
            try:
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                    resp = await client.get(settings.emt_malaga_realtime_url)
                    resp.raise_for_status()
                    raw = resp.json()
                    features = raw.get("features", []) if isinstance(raw, dict) else raw
                    self.store.set_cached_response(cache_key, "emt_malaga_rt", features, ttl_seconds=30)
            except Exception as exc:
                return {
                    "ciudad": "malaga",
                    "codigo_parada": codigo_parada,
                    "disponible": False,
                    "mensaje": f"Error obteniendo ubicaciones GPS de EMT Málaga: {exc}",
                    "estimaciones": [],
                }

        buses: list[dict[str, Any]] = []
        for feat in features:
            props = feat.get("properties") or feat
            cod_linea_raw = str(props.get("codLinea") or "").strip()
            cod_linea = cod_linea_raw[:-2] if cod_linea_raw.endswith(".0") else cod_linea_raw
            if target_lines and cod_linea not in target_lines and cod_linea_raw not in target_lines:
                continue
            coords = (feat.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 2:
                continue
            try:
                b_lon, b_lat = float(coords[0]), float(coords[1])
            except ValueError:
                continue
            dist = round(haversine_m(float(stop["lat"]), float(stop["lon"]), b_lat, b_lon))
            # Estimación orientativa a velocidad media urbana de 18 km/h (300 m/min)
            buses.append(
                {
                    "linea": cod_linea,
                    "cod_bus": props.get("codBus"),
                    "sentido": props.get("sentido"),
                    "distancia_metros": dist,
                    "tiempo_estimado_min": max(1, round(dist / 300)),
                    "ultima_actualizacion": props.get("last_update"),
                }
            )

        buses.sort(key=lambda b: b["distancia_metros"])
        return {
            "ciudad": "malaga",
            "codigo_parada": codigo_parada,
            "nombre_parada": stop["nombre"],
            "disponible": True,
            "estimaciones": buses[:10],
        }

    async def get_line_stops(
        self,
        ciudad: str,
        linea: str,
        sentido: str | None = None,
    ) -> dict[str, Any]:
        """Devuelve las paradas ordenadas de una línea urbana en una ciudad."""
        city = self._normalize_city(ciudad)
        await self.ensure_city_loaded(city)

        q_line = linea.strip()
        params: list[Any] = [city, q_line.lower()]
        sentido_sql = ""
        if sentido:
            sentido_sql = " AND sentido = ?"
            params.append(str(sentido).strip())

        rows = self.store.execute_dicts(
            f"""
            SELECT linea, nombre_linea, sentido, orden, codigo_parada, nombre_parada, direccion, lat, lon
            FROM urban_line_stops
            WHERE ciudad = ? AND LOWER(linea) = ? {sentido_sql}
            ORDER BY sentido ASC, orden ASC
            """,
            params,
        )
        return {
            "ciudad": city,
            "linea": linea,
            "total_paradas": len(rows),
            "paradas": rows,
        }

    async def list_nap_andalucia_datasets(self, filtro: str | None = None) -> list[dict[str, Any]]:
        """Consulta el catálogo oficial del NAP filtrando conjuntos de datos de Andalucía."""
        if not settings.nap_api_key:
            raise ValueError("Falta configurar MCP_ANDALUCIA_NAP_API_KEY.")

        cache_key = "nap:andalucia_datasets"
        datasets = self.store.get_cached_response(cache_key)
        if datasets is None:
            url = f"{settings.nap_base_url.rstrip('/')}/Fichero/GetList"
            async with httpx.AsyncClient(
                timeout=25.0,
                headers={"ApiKey": settings.nap_api_key, "Accept": "application/json"},
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                raw = resp.json()

            datasets = []
            for c in raw.get("conjuntosDatoDto", []):
                nombre = c.get("nombre") or ""
                desc = c.get("descripcion") or ""
                org = (c.get("organizacion") or {}).get("nombre") or ""
                blob = normalize_text(f"{nombre} {desc} {org}")
                if not any(k in blob for k in ANDALUSIA_KEYWORDS):
                    continue
                ficheros = [
                    {
                        "fichero_id": f.get("ficheroId"),
                        "formato": f.get("tipoFicheroNombre"),
                        "paradas": f.get("numeroParadas"),
                        "rutas": f.get("numeroRutas"),
                        "actualizado": f.get("fechaActualizacion"),
                    }
                    for f in c.get("ficherosDto", [])
                ]
                datasets.append(
                    {
                        "conjunto_id": c.get("conjuntoDatoId"),
                        "nombre": nombre,
                        "organizacion": org,
                        "tipos": [t.get("nombre") for t in c.get("tiposTransporte", [])],
                        "ficheros": ficheros,
                    }
                )
            self.store.set_cached_response(cache_key, "nap", datasets, ttl_seconds=86400)

        if filtro:
            q = normalize_text(filtro)
            datasets = [
                d
                for d in datasets
                if q in normalize_text(f"{d['nombre']} {d['organizacion']}")
            ]
        return datasets
