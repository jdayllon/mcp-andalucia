"""Tests unitarios para utilidades, formato NDJSON, conversión UTM y DuckDBStore."""

import json
from pathlib import Path

from mcp_andalucia.db import DuckDBStore
from mcp_andalucia.utils import (
    clean_record,
    normalize_text,
    resolve_from_catalog,
    to_ndjson,
    utm_to_latlon,
)


def test_normalize_and_resolve_catalog():
    catalog = ["-", "MÁLAGA", "SEVILLA", "ALCALA DE GUADAIRA", "CÁDIZ"]
    assert normalize_text("  Málaga  ") == "malaga"
    assert resolve_from_catalog("malaga", catalog) == "MÁLAGA"
    assert resolve_from_catalog("Alcalá de Guadaíra", catalog) == "ALCALA DE GUADAIRA"
    assert resolve_from_catalog("cadiz", catalog) == "CÁDIZ"
    assert resolve_from_catalog("-", catalog) is None


def test_utm_to_latlon_sevilla():
    # Coordenadas reales de HOTEL SEVILLA en OpenRTA (EPSG:25830)
    coords = utm_to_latlon("234948.41174785414", "4142794.2937005567", "25830")
    assert coords is not None
    lat, lon = coords
    assert 37.3 < lat < 37.5
    assert -6.1 < lon < -5.8


def test_clean_record_and_ndjson():
    raw = {"id": 1, "name": "Hotel Sol", "email": "-", "specialties": "No disponible", "phone": None}
    cleaned = clean_record(raw)
    assert cleaned == {"id": 1, "name": "Hotel Sol"}

    ndjson_str = to_ndjson([cleaned], meta={"total": 1})
    lines = ndjson_str.splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"_meta": {"total": 1}}
    assert json.loads(lines[1]) == {"id": 1, "name": "Hotel Sol"}


def test_duckdb_store_catalog_and_ndjson_load(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("mcp_andalucia.config.settings.data_dir", tmp_path)
    store = DuckDBStore(db_path=tmp_path / "test.duckdb")

    store.save_catalog(
        "openrta:provinces",
        [{"value": "SEVILLA", "norm_value": "sevilla", "meta": {"doc_count": 100}}],
    )
    assert store.is_fresh("catalog:openrta:provinces", 60)
    loaded = store.get_catalog("openrta:provinces")
    assert len(loaded) == 1
    assert loaded[0]["value"] == "SEVILLA"
    assert loaded[0]["meta"]["doc_count"] == 100

    # Comprobar carga directa de NDJSON en DuckDB
    ndjson_file = tmp_path / "sample.ndjson"
    ndjson_file.write_text('{"id": 10, "ciudad": "Granada"}\n{"id": 20, "ciudad": "Córdoba"}\n', encoding="utf-8")
    count = store.load_ndjson_table("sample_cities", ndjson_file)
    assert count == 2
    rows = store.execute_dicts("SELECT ciudad FROM sample_cities WHERE id = 20")
    assert rows == [{"ciudad": "Córdoba"}]

    # Comprobar response_cache con TTL
    store.set_cached_response("openrta:test:1", "openrta", {"hits": 3}, ttl_seconds=60)
    assert store.get_cached_response("openrta:test:1") == {"hits": 3}
    stats = store.cache_stats()
    assert stats["response_cache_entries"] >= 1
    assert stats["cached_catalogs"] >= 1
    store.close()
