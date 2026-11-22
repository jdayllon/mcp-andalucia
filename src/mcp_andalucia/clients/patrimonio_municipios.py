"""Cliente para Declaraciones de Interés Turístico en Andalucía y Fichas Municipales SIMA (IECA),
consultados y almacenados directamente con DuckDB + NDJSON.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
import xlrd

from mcp_andalucia.config import settings
from mcp_andalucia.db import DuckDBStore, get_store
from mcp_andalucia.logging_config import get_logger
from mcp_andalucia.utils import bounding_box, normalize_text

logger = get_logger("clients.patrimonio_municipios")

RESOURCE_UUID_RE = re.compile(r"/resource/([0-9a-fA-F-]{36})")


def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", ".")
    if s in ("", "-", "*", "..", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(val: Any) -> int | None:
    f = _to_float(val)
    return int(round(f)) if f is not None else None


def _parse_latlon(coord_str: Any) -> tuple[float | None, float | None]:
    if not coord_str:
        return (None, None)
    parts = [p.strip() for p in str(coord_str).split(",")]
    if len(parts) != 2:
        return (None, None)
    return (_to_float(parts[0]), _to_float(parts[1]))


class PatrimonioMunicipiosClient:
    """Gestiona las Declaraciones de Interés Turístico y los 785 municipios del SIMA en DuckDB."""

    def __init__(self, store: DuckDBStore | None = None) -> None:
        self.store = store or get_store()

    async def resolve_declaraciones_csv_url(self, resource_or_csv_url: str | None = None) -> str:
        """Resuelve la URL pública de descarga del CSV desde la URL del recurso CKAN de la Junta."""
        url = (resource_or_csv_url or settings.declaraciones_resource_url).strip()
        if url.lower().endswith(".csv"):
            return url.replace(
                "gdc-pdpopendata-ckan.paas.junta-andalucia.es",
                "www.juntadeandalucia.es",
            )

        match = RESOURCE_UUID_RE.search(url)
        if not match:
            return settings.declaraciones_csv_fallback_url

        res_id = match.group(1)
        api_url = f"https://www.juntadeandalucia.es/datosabiertos/portal/api/3/action/resource_show?id={res_id}"
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(api_url)
                resp.raise_for_status()
                dl_url = str((resp.json().get("result") or {}).get("url") or "").strip()
                if dl_url:
                    return dl_url.replace(
                        "gdc-pdpopendata-ckan.paas.junta-andalucia.es",
                        "www.juntadeandalucia.es",
                    )
        except Exception as exc:
            logger.warning("No se pudo resolver resource_show CKAN (%s), usando URL CSV fallback", exc)

        return settings.declaraciones_csv_fallback_url

    async def ensure_declaraciones_loaded(
        self,
        url_override: str | None = None,
        force_refresh: bool = False,
    ) -> int:
        """Descarga/consulta la URL del CSV de Declaraciones de Interés Turístico con `read_csv` de DuckDB."""
        cache_key = "junta:declaraciones_interes_turistico"
        if not force_refresh and not url_override and self.store.is_fresh(cache_key, settings.catalog_ttl_seconds):
            try:
                cnt = self.store.execute("SELECT COUNT(*) FROM declaraciones_interes_turistico")[0][0]
                if cnt > 0:
                    return int(cnt)
            except Exception:
                pass

        csv_url = await self.resolve_declaraciones_csv_url(url_override)
        raw_csv_path = settings.ndjson_dir / "declaraciones_interes_turistico_raw.csv"

        t0 = time.perf_counter()
        async with httpx.AsyncClient(
            timeout=25.0,
            follow_redirects=True,
            headers={"User-Agent": "mcp-andalucia/0.3"},
        ) as client:
            resp = await client.get(csv_url)
            resp.raise_for_status()
            raw_csv_path.write_bytes(resp.content)
        logger.info(
            "CSV Declaraciones de Interés Turístico descargado desde %s (%d bytes, %.1f ms)",
            csv_url,
            len(resp.content),
            (time.perf_counter() - t0) * 1000,
        )

        return self.load_declaraciones_from_csv_with_duckdb(raw_csv_path, cache_key)

    def load_declaraciones_from_csv_with_duckdb(
        self,
        csv_source: str | Path,
        cache_key: str = "junta:declaraciones_interes_turistico",
    ) -> int:
        """Usa el motor `read_csv` de DuckDB (delim='=') para transformar y exportar las declaraciones a NDJSON."""
        self.store.load_csv_table("declaraciones_raw", csv_source, delim="=", quote='"')
        raw_cols = [r[0] for r in self.store.execute("DESCRIBE declaraciones_raw")]

        def _col(idx: int) -> str:
            return f'"{raw_cols[idx]}"' if idx < len(raw_cols) else "''"

        with self.store._lock:
            self.store._conn.execute(
                f"""
                CREATE OR REPLACE TABLE declaraciones_interes_turistico AS
                SELECT
                    TRIM(COALESCE({_col(0)}, '')) AS nombre,
                    TRIM(COALESCE({_col(1)}, '')) AS localidad,
                    TRIM(COALESCE({_col(2)}, '')) AS provincia,
                    TRIM(COALESCE({_col(3)}, '')) AS fecha_resolucion,
                    TRIM(COALESCE({_col(4)}, '')) AS ambito,
                    TRIM(COALESCE({_col(5)}, '')) AS promotor,
                    TRIM(COALESCE({_col(6)}, '')) AS tipo,
                    TRIM(COALESCE({_col(7)}, '')) AS boletin,
                    TRIM(COALESCE({_col(8)}, '')) AS url,
                    TRIM(COALESCE({_col(9)}, '')) AS descripcion
                FROM declaraciones_raw
                WHERE TRIM(COALESCE({_col(0)}, '')) <> ''
                """
            )
            self.store._conn.execute("DROP TABLE IF EXISTS declaraciones_raw")

        rows = self.store.execute_dicts("SELECT * FROM declaraciones_interes_turistico")
        ndjson_path = settings.ndjson_dir / "declaraciones_interes_turistico.ndjson"
        with ndjson_path.open("w", encoding="utf-8") as f:
            for r in rows:
                r["norm_localidad"] = normalize_text(r.get("localidad"))
                r["norm_provincia"] = normalize_text(r.get("provincia"))
                r["norm_tipo"] = normalize_text(r.get("tipo"))
                r["norm_ambito"] = normalize_text(r.get("ambito"))
                r["norm_search"] = normalize_text(
                    f"{r.get('nombre')} {r.get('localidad')} {r.get('provincia')} {r.get('tipo')} {r.get('descripcion')}"
                )
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")

        count = self.store.load_ndjson_table("declaraciones_interes_turistico", ndjson_path)
        self.store.mark_fresh(cache_key, count)
        return count

    async def search_declaraciones(
        self,
        *,
        provincia: str | None = None,
        municipio: str | None = None,
        tipo: str | None = None,
        ambito: str | None = None,
        texto: str | None = None,
        url_recurso: str | None = None,
        limite: int = 25,
    ) -> dict[str, Any]:
        """Consulta con SQL en DuckDB las Declaraciones de Interés Turístico de Andalucía."""
        await self.ensure_declaraciones_loaded(url_override=url_recurso)

        clauses: list[str] = ["1=1"]
        params: list[Any] = []

        if provincia:
            clauses.append("norm_provincia LIKE ?")
            params.append(f"%{normalize_text(provincia)}%")
        if municipio:
            clauses.append("norm_localidad LIKE ?")
            params.append(f"%{normalize_text(municipio)}%")
        if tipo:
            clauses.append("norm_tipo LIKE ?")
            params.append(f"%{normalize_text(tipo)}%")
        if ambito:
            clauses.append("norm_ambito LIKE ?")
            params.append(f"%{normalize_text(ambito)}%")
        if texto:
            clauses.append("norm_search LIKE ?")
            params.append(f"%{normalize_text(texto)}%")

        where_sql = " AND ".join(clauses)
        total_row = self.store.execute(
            f"SELECT COUNT(*) FROM declaraciones_interes_turistico WHERE {where_sql}",
            params,
        )
        total = int(total_row[0][0]) if total_row else 0

        rows = self.store.execute_dicts(
            f"""
            SELECT
                nombre,
                localidad,
                provincia,
                fecha_resolucion,
                ambito,
                promotor,
                tipo,
                boletin,
                url,
                descripcion
            FROM declaraciones_interes_turistico
            WHERE {where_sql}
            ORDER BY provincia ASC, localidad ASC, nombre ASC
            LIMIT ?
            """,
            params + [max(1, min(limite, 100))],
        )
        cleaned = [{k: v for k, v in r.items() if v not in (None, "")} for r in rows]
        return {
            "total_coincidencias": total,
            "devueltos": len(cleaned),
            "declaraciones": cleaned,
        }

    async def ensure_municipios_sima_loaded(
        self,
        url_override: str | None = None,
        force_refresh: bool = False,
    ) -> int:
        """Descarga `smex99.xls` del SIMA (IECA), lo convierte a NDJSON y lo indexa en DuckDB."""
        cache_key = "junta:municipios_sima"
        if not force_refresh and not url_override and self.store.is_fresh(cache_key, settings.catalog_ttl_seconds):
            try:
                cnt = self.store.execute("SELECT COUNT(*) FROM municipios_sima")[0][0]
                if cnt > 0:
                    return int(cnt)
            except Exception:
                pass

        xls_url = (url_override or settings.sima_municipios_xls_url).strip()
        t0 = time.perf_counter()
        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "mcp-andalucia/0.3"},
        ) as client:
            resp = await client.get(xls_url)
            resp.raise_for_status()
            xls_bytes = resp.content
        logger.info(
            "SIMA smex99.xls descargado desde %s (%d bytes, %.1f ms)",
            xls_url,
            len(xls_bytes),
            (time.perf_counter() - t0) * 1000,
        )

        return self.load_municipios_sima_from_xls_bytes(xls_bytes, cache_key)

    def load_municipios_sima_from_xls_bytes(
        self,
        xls_bytes: bytes,
        cache_key: str = "junta:municipios_sima",
    ) -> int:
        """Extrae la hoja `Datos` de `smex99.xls`, genera `municipios_sima.ndjson` y la carga en DuckDB."""
        wb = xlrd.open_workbook(file_contents=xls_bytes)
        sh = wb.sheet_by_name("Datos")

        # Buscar la fila de cabecera (donde columna 0 es 'Provincia' y columna 1 es 'CodMun')
        header_row_idx = 10
        for r_idx in range(min(20, sh.nrows)):
            c0 = str(sh.cell_value(r_idx, 0)).strip().lower()
            c1 = str(sh.cell_value(r_idx, 1)).strip().lower()
            if c0 == "provincia" and "codmun" in c1:
                header_row_idx = r_idx
                break

        headers = [str(sh.cell_value(header_row_idx, c)).strip() for c in range(sh.ncols)]

        records: list[dict[str, Any]] = []
        for r_idx in range(header_row_idx + 1, sh.nrows):
            prov = str(sh.cell_value(r_idx, 0)).strip()
            cod_mun = str(sh.cell_value(r_idx, 1)).strip()
            muni = str(sh.cell_value(r_idx, 2)).strip()
            if not prov or not cod_mun or not muni:
                continue

            lat, lon = _parse_latlon(sh.cell_value(r_idx, 6) if sh.ncols > 6 else None)

            all_vars: dict[str, Any] = {}
            for c_idx, h_name in enumerate(headers):
                if not h_name:
                    continue
                val = sh.cell_value(r_idx, c_idx)
                if isinstance(val, str):
                    val = val.strip()
                if val not in ("", "-", "*", None):
                    all_vars[h_name] = val

            b_muebles = _to_int(sh.cell_value(r_idx, 120)) if sh.ncols > 120 else None
            b_inmuebles = _to_int(sh.cell_value(r_idx, 121)) if sh.ncols > 121 else None
            b_inmat = _to_int(sh.cell_value(r_idx, 122)) if sh.ncols > 122 else None
            total_patrimonio = (b_muebles or 0) + (b_inmuebles or 0) + (b_inmat or 0)

            rec: dict[str, Any] = {
                "cod_ine": cod_mun,
                "municipio": muni,
                "provincia": prov,
                "lat": lat,
                "lon": lon,
                "superficie_km2": _to_float(sh.cell_value(r_idx, 3)) if sh.ncols > 3 else None,
                "altitud_m": _to_int(sh.cell_value(r_idx, 5)) if sh.ncols > 5 else None,
                "num_nucleos": _to_int(sh.cell_value(r_idx, 7)) if sh.ncols > 7 else None,
                "distancia_capital_km": _to_float(sh.cell_value(r_idx, 8)) if sh.ncols > 8 else None,
                "poblacion": _to_int(sh.cell_value(r_idx, 9)) if sh.ncols > 9 else None,
                "edad_media": _to_float(sh.cell_value(r_idx, 14)) if sh.ncols > 14 else None,
                "extranjeros": _to_int(sh.cell_value(r_idx, 18)) if sh.ncols > 18 else None,
                "bibliotecas": _to_int(sh.cell_value(r_idx, 33)) if sh.ncols > 33 else None,
                "centros_salud": _to_int(sh.cell_value(r_idx, 34)) if sh.ncols > 34 else None,
                "consultorios": _to_int(sh.cell_value(r_idx, 35)) if sh.ncols > 35 else None,
                "cultivo_regadio": str(sh.cell_value(r_idx, 68)).strip() if sh.ncols > 68 else None,
                "cultivo_secano": str(sh.cell_value(r_idx, 70)).strip() if sh.ncols > 70 else None,
                "taxis": _to_int(sh.cell_value(r_idx, 83)) if sh.ncols > 83 else None,
                "hoteles": _to_int(sh.cell_value(r_idx, 91)) if sh.ncols > 91 else None,
                "hostales_pensiones": _to_int(sh.cell_value(r_idx, 92)) if sh.ncols > 92 else None,
                "plazas_hoteles": _to_int(sh.cell_value(r_idx, 93)) if sh.ncols > 93 else None,
                "plazas_hostales": _to_int(sh.cell_value(r_idx, 94)) if sh.ncols > 94 else None,
                "renta_bruta_media": _to_int(sh.cell_value(r_idx, 111)) if sh.ncols > 111 else None,
                "bienes_muebles_iaph": b_muebles,
                "bienes_inmuebles_iaph": b_inmuebles,
                "bienes_inmateriales_iaph": b_inmat,
                "total_patrimonio_iaph": total_patrimonio,
                "url_guia_iaph": str(sh.cell_value(r_idx, 123)).strip() if sh.ncols > 123 else None,
                "norm_municipio": normalize_text(muni),
                "norm_provincia": normalize_text(prov),
                "indicadores_json": json.dumps(all_vars, ensure_ascii=False, separators=(",", ":")),
            }
            records.append(rec)

        ndjson_path = settings.ndjson_dir / "municipios_sima.ndjson"
        with ndjson_path.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")

        count = self.store.load_ndjson_table("municipios_sima", ndjson_path)
        self.store.mark_fresh(cache_key, count)
        return count

    async def get_municipio_sima(
        self,
        *,
        municipio: str | None = None,
        provincia: str | None = None,
        cod_ine: str | None = None,
        incluir_124_variables: bool = False,
        limite: int = 10,
    ) -> list[dict[str, Any]]:
        """Obtiene la ficha socioeconómica, geográfica y patrimonial (SIMA + IAPH) de uno o varios municipios."""
        await self.ensure_municipios_sima_loaded()

        clauses: list[str] = ["1=1"]
        params: list[Any] = []
        if cod_ine:
            clauses.append("cod_ine = ?")
            params.append(cod_ine.strip())
        if municipio:
            q_m = normalize_text(municipio)
            clauses.append("(norm_municipio = ? OR norm_municipio LIKE ?)")
            params.extend([q_m, f"%{q_m}%"])
        if provincia:
            clauses.append("norm_provincia LIKE ?")
            params.append(f"%{normalize_text(provincia)}%")

        where_sql = " AND ".join(clauses)
        rows = self.store.execute_dicts(
            f"""
            SELECT *
            FROM municipios_sima
            WHERE {where_sql}
            ORDER BY LENGTH(municipio) ASC, poblacion DESC NULLS LAST
            LIMIT ?
            """,
            params + [max(1, min(limite, 50))],
        )

        result: list[dict[str, Any]] = []
        for r in rows:
            item = {
                k: v
                for k, v in r.items()
                if not k.startswith("norm_") and k != "indicadores_json" and v not in (None, "", "-")
            }
            if incluir_124_variables and r.get("indicadores_json"):
                item["indicadores_completos"] = json.loads(r["indicadores_json"])
            result.append(item)
        return result

    async def find_nearby_municipios_sima(
        self,
        *,
        municipio_origen: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        radio_km: float = 25.0,
        limite: int = 15,
    ) -> dict[str, Any]:
        """Busca municipios andaluces cercanos usando Bounding Box + Haversine en SQL dentro de DuckDB."""
        await self.ensure_municipios_sima_loaded()

        origen_info: dict[str, Any] = {}
        if (lat is None or lon is None) and municipio_origen:
            matches = await self.get_municipio_sima(municipio=municipio_origen, limite=1)
            if matches and matches[0].get("lat") is not None and matches[0].get("lon") is not None:
                lat = float(matches[0]["lat"])
                lon = float(matches[0]["lon"])
                origen_info = {
                    "municipio": matches[0]["municipio"],
                    "provincia": matches[0]["provincia"],
                    "lat": lat,
                    "lon": lon,
                }

        if lat is None or lon is None:
            raise ValueError("Debes indicar `municipio_origen` o las coordenadas (`lat`, `lon`).")

        radius_m = max(1000.0, min(radio_km * 1000.0, 150_000.0))
        lat_min, lat_max, lon_min, lon_max = bounding_box(lat, lon, radius_m * 1.15)

        rows = self.store.execute_dicts(
            """
            SELECT
                cod_ine,
                municipio,
                provincia,
                lat,
                lon,
                altitud_m,
                poblacion,
                plazas_hoteles,
                total_patrimonio_iaph,
                url_guia_iaph,
                ROUND(
                    6371.0 * 2.0 * ASIN(LEAST(1.0, SQRT(
                        POWER(SIN(RADIANS(lat - ?) / 2.0), 2) +
                        COS(RADIANS(?)) * COS(RADIANS(lat)) *
                        POWER(SIN(RADIANS(lon - ?) / 2.0), 2)
                    ))),
                    1
                ) AS distancia_km
            FROM municipios_sima
            WHERE lat IS NOT NULL AND lon IS NOT NULL
              AND lat BETWEEN ? AND ?
              AND lon BETWEEN ? AND ?
            """,
            [lat, lat, lon, lat_min, lat_max, lon_min, lon_max],
        )

        filtered = [r for r in rows if float(r["distancia_km"]) <= radio_km]
        filtered.sort(key=lambda r: float(r["distancia_km"]))
        return {
            "origen": origen_info or {"lat": lat, "lon": lon},
            "radio_km": radio_km,
            "total_en_radio": len(filtered),
            "municipios": filtered[: max(1, min(limite, 50))],
        }

    async def ensure_banderas_azules_loaded(
        self,
        url_override: str | None = None,
        force_refresh: bool = False,
    ) -> int:
        """Descarga `banderas_azules_2026-27.json` de la Junta y lo carga en DuckDB con `read_json_auto`."""
        cache_key = "junta:banderas_azules"
        if not force_refresh and not url_override and self.store.is_fresh(cache_key, settings.catalog_ttl_seconds):
            try:
                cnt = self.store.execute("SELECT COUNT(*) FROM banderas_azules")[0][0]
                if cnt > 0:
                    return int(cnt)
            except Exception:
                pass

        json_url = (url_override or settings.banderas_azules_json_url).strip()
        raw_json_path = settings.ndjson_dir / "banderas_azules_raw.json"

        t0 = time.perf_counter()
        async with httpx.AsyncClient(
            timeout=25.0,
            follow_redirects=True,
            headers={"User-Agent": "mcp-andalucia/0.3"},
        ) as client:
            resp = await client.get(json_url)
            resp.raise_for_status()
            raw_json_path.write_bytes(resp.content)
        logger.info(
            "JSON Banderas Azules descargado desde %s (%d bytes, %.1f ms)",
            json_url,
            len(resp.content),
            (time.perf_counter() - t0) * 1000,
        )

        return self.load_banderas_azules_from_json_with_duckdb(raw_json_path, cache_key)

    def load_banderas_azules_from_json_with_duckdb(
        self,
        json_source: str | Path,
        cache_key: str = "junta:banderas_azules",
    ) -> int:
        """Carga el fichero JSON con `read_json_auto` de DuckDB, normaliza columnas y exporta a NDJSON."""
        self.store.load_json_table("banderas_azules_raw", json_source)
        with self.store._lock:
            self.store._conn.execute(
                """
                CREATE OR REPLACE TABLE banderas_azules AS
                SELECT
                    TRIM(CAST(COALESCE("Provincia", '') AS VARCHAR)) AS provincia,
                    TRIM(CAST(COALESCE("Municipio", '') AS VARCHAR)) AS municipio,
                    TRIM(CAST(COALESCE("Categoría", '') AS VARCHAR)) AS categoria,
                    TRIM(CAST(COALESCE("Bandera azul", '') AS VARCHAR)) AS nombre,
                    TRIM(CAST(COALESCE("Año", '') AS VARCHAR)) AS anio,
                    TRIM(CAST(COALESCE("Notas", '') AS VARCHAR)) AS notas
                FROM banderas_azules_raw
                WHERE TRIM(CAST(COALESCE("Bandera azul", '') AS VARCHAR)) <> ''
                """
            )
            self.store._conn.execute("DROP TABLE IF EXISTS banderas_azules_raw")

        rows = self.store.execute_dicts("SELECT * FROM banderas_azules")
        ndjson_path = settings.ndjson_dir / "banderas_azules.ndjson"
        with ndjson_path.open("w", encoding="utf-8") as f:
            for r in rows:
                r["norm_provincia"] = normalize_text(r.get("provincia"))
                r["norm_municipio"] = normalize_text(r.get("municipio"))
                r["norm_categoria"] = normalize_text(r.get("categoria"))
                r["norm_search"] = normalize_text(
                    f"{r.get('nombre')} {r.get('municipio')} {r.get('provincia')} {r.get('categoria')} {r.get('notas')}"
                )
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")

        count = self.store.load_ndjson_table("banderas_azules", ndjson_path)
        self.store.mark_fresh(cache_key, count)
        return count

    async def search_banderas_azules(
        self,
        *,
        provincia: str | None = None,
        municipio: str | None = None,
        categoria: str | None = None,
        anio: str | None = None,
        solo_ultimo_anio: bool = True,
        texto: str | None = None,
        url_recurso: str | None = None,
        limite: int = 30,
    ) -> dict[str, Any]:
        """Consulta en DuckDB las playas, puertos deportivos y embarcaciones con Bandera Azul en Andalucía."""
        await self.ensure_banderas_azules_loaded(url_override=url_recurso)

        target_year = anio.strip() if anio else None
        if not target_year and solo_ultimo_anio:
            max_row = self.store.execute("SELECT MAX(anio) FROM banderas_azules")
            if max_row and max_row[0][0]:
                target_year = str(max_row[0][0])

        clauses: list[str] = ["1=1"]
        params: list[Any] = []
        if target_year:
            clauses.append("anio = ?")
            params.append(target_year)
        if provincia:
            clauses.append("norm_provincia LIKE ?")
            params.append(f"%{normalize_text(provincia)}%")
        if municipio:
            clauses.append("norm_municipio LIKE ?")
            params.append(f"%{normalize_text(municipio)}%")
        if categoria:
            clauses.append("norm_categoria LIKE ?")
            params.append(f"%{normalize_text(categoria)}%")
        if texto:
            clauses.append("norm_search LIKE ?")
            params.append(f"%{normalize_text(texto)}%")

        where_sql = " AND ".join(clauses)
        total_row = self.store.execute(
            f"SELECT COUNT(*) FROM banderas_azules WHERE {where_sql}",
            params,
        )
        total = int(total_row[0][0]) if total_row else 0

        rows = self.store.execute_dicts(
            f"""
            SELECT nombre, categoria, municipio, provincia, anio, notas
            FROM banderas_azules
            WHERE {where_sql}
            ORDER BY anio DESC, provincia ASC, municipio ASC, nombre ASC
            LIMIT ?
            """,
            params + [max(1, min(limite, 100))],
        )
        cleaned = [{k: v for k, v in r.items() if v not in (None, "")} for r in rows]
        return {
            "anio_filtrado": target_year or "todos",
            "total_coincidencias": total,
            "devueltos": len(cleaned),
            "banderas_azules": cleaned,
        }
