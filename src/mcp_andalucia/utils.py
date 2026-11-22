"""Utilidades de normalización de texto, formato NDJSON y conversión de coordenadas."""

from __future__ import annotations

import json
import unicodedata
from functools import lru_cache
from typing import Any, Iterable

from pyproj import Transformer


@lru_cache(maxsize=1)
def _utm25830_to_wgs84() -> Transformer:
    """Transformador cacheado de ETRS89 / UTM zona 30N (EPSG:25830) a WGS84 (EPSG:4326)."""
    return Transformer.from_crs("EPSG:25830", "EPSG:4326", always_xy=True)


def utm_to_latlon(coord_x: str | float | None, coord_y: str | float | None, srid: str | int | None = None) -> tuple[float, float] | None:
    """Convierte coordenadas UTM (EPSG:25830 por defecto) a (lat, lon) en WGS84."""
    if coord_x is None or coord_y is None:
        return None
    try:
        x = float(str(coord_x).replace(",", ".").strip())
        y = float(str(coord_y).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None

    if x == 0.0 or y == 0.0:
        return None

    # Si ya parecen estar en grados (lat/lon), no transformar
    if -180 <= x <= 180 and -90 <= y <= 90:
        return (round(y, 6), round(x, 6))

    srid_str = str(srid).strip() if srid else "25830"
    if srid_str not in ("25830", "4326", ""):
        transformer = Transformer.from_crs(f"EPSG:{srid_str}", "EPSG:4326", always_xy=True)
    else:
        transformer = _utm25830_to_wgs84()

    lon, lat = transformer.transform(x, y)
    return (round(lat, 6), round(lon, 6))


def normalize_text(text: str | None) -> str:
    """Normaliza texto: minúsculas, sin tildes/diacríticos y espacios colapsados."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    sin_tildes = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(sin_tildes.lower().strip().split())


def is_empty_rta_value(value: Any) -> bool:
    """Indica si un valor devuelto por OpenRTA equivale a vacío."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in ("", "-", "No disponible", "null", "None", "N/A")
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def clean_record(record: dict[str, Any]) -> dict[str, Any]:
    """Elimina campos vacíos o centinelas ('-', 'No disponible', 'null') de un registro."""
    return {k: v for k, v in record.items() if not is_empty_rta_value(v)}


def to_ndjson(records: Iterable[dict[str, Any]], meta: dict[str, Any] | None = None) -> str:
    """Serializa una secuencia de diccionarios a formato NDJSON (una línea JSON por registro).

    Si se proporciona `meta`, se escribe como primera línea `{"_meta": ...}`.
    """
    lines: list[str] = []
    if meta is not None:
        lines.append(json.dumps({"_meta": meta}, ensure_ascii=False, separators=(",", ":")))
    for rec in records:
        lines.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)


def resolve_from_catalog(query: str, catalog: list[str]) -> str | None:
    """Resuelve un valor de catálogo ignorando mayúsculas y tildes.

    Prioriza coincidencia exacta normalizada; si no, coincidencia por prefijo o subcadena.
    """
    q_norm = normalize_text(query)
    if not q_norm or q_norm == "-":
        return None

    exact: list[str] = []
    prefix: list[str] = []
    contains: list[str] = []

    for item in catalog:
        if item == "-":
            continue
        i_norm = normalize_text(item)
        if i_norm == q_norm:
            exact.append(item)
        elif i_norm.startswith(q_norm):
            prefix.append(item)
        elif q_norm in i_norm:
            contains.append(item)

    if exact:
        return exact[0]
    if prefix:
        return prefix[0]
    if contains:
        return contains[0]
    return None
