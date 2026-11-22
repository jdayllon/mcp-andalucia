"""Tests para el servidor MCP, registro de las 12 tools, FastAPI y lógica de clientes."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcp_andalucia.app import app
from mcp_andalucia.clients.opensky import resolve_airport_icao
from mcp_andalucia.clients.renfe import RenfeClient
from mcp_andalucia.db import DuckDBStore
from mcp_andalucia.server import create_mcp_server


@pytest.mark.asyncio
async def test_mcp_server_registers_all_12_tools():
    mcp = create_mcp_server()
    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}

    expected = {
        "buscar_alojamientos",
        "buscar_guias_turisticos",
        "buscar_actividades_turisticas",
        "buscar_restauracion",
        "buscar_oficinas_turismo",
        "explorar_destino",
        "obtener_detalle_elemento",
        "buscar_en_registro",
        "buscar_trenes_renfe",
        "buscar_autobuses_ctan",
        "consultar_conectividad_aerea",
        "recomendar_transporte_entre_destinos",
    }
    assert expected == tool_names

    prompts = await mcp.list_prompts()
    assert any(p.name == "planificar_itinerario_andalucia" for p in prompts)


def test_fastapi_health_endpoint():
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


def test_resolve_airport_icao():
    assert resolve_airport_icao("Sevilla") == "LEZL"
    assert resolve_airport_icao("AGP") == "LEMG"
    assert resolve_airport_icao("Granada") == "LEGR"
    assert resolve_airport_icao("LEJR") == "LEJR"


@pytest.mark.asyncio
async def test_renfe_duckdb_query_from_ndjson(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("mcp_andalucia.config.settings.data_dir", tmp_path)
    store = DuckDBStore(db_path=tmp_path / "renfe_test.duckdb")

    # Creamos tablas GTFS mínimas en NDJSON y las cargamos en DuckDB
    ndjson_dir = tmp_path / "ndjson"
    ndjson_dir.mkdir(parents=True, exist_ok=True)

    stops = [
        {"stop_id": "51003", "stop_name": "SEVILLA-SANTA JUSTA", "norm_name": "sevilla-santa justa", "stop_lat": "37.39", "stop_lon": "-5.97"},
        {"stop_id": "50500", "stop_name": "CORDOBA", "norm_name": "cordoba", "stop_lat": "37.88", "stop_lon": "-4.78"},
    ]
    routes = [{"route_id": "R1", "route_short_name": "AVE", "route_long_name": "AVE Sevilla - Madrid"}]
    trips = [{"trip_id": "T1", "route_id": "R1", "service_id": "S1", "trip_short_name": "AVE 02101"}]
    stop_times = [
        {"trip_id": "T1", "stop_id": "51003", "stop_sequence": "1", "departure_time": "09:00:00", "arrival_time": "09:00:00"},
        {"trip_id": "T1", "stop_id": "50500", "stop_sequence": "2", "departure_time": "09:45:00", "arrival_time": "09:45:00"},
    ]
    calendar = [{"service_id": "S1", "monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1", "friday": "1", "saturday": "1", "sunday": "1", "start_date": "20260101", "end_date": "20261231"}]
    calendar_dates = [{"service_id": "S1", "date": "20261225", "exception_type": "2"}]

    for name, rows in [
        ("stops", stops),
        ("routes", routes),
        ("trips", trips),
        ("stop_times", stop_times),
        ("calendar", calendar),
        ("calendar_dates", calendar_dates),
    ]:
        p = ndjson_dir / f"renfe_{name}.ndjson"
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        store.load_ndjson_table(f"renfe_{name}", p)

    store.mark_fresh("renfe:gtfs_av_ld_md", 2)

    renfe = RenfeClient(store=store)
    res = await renfe.search_trains(origen="Sevilla", destino="Córdoba", fecha="2026-11-23")
    assert res["cobertura"] is True
    assert len(res["trenes"]) == 1
    assert res["trenes"][0]["hora_salida"] == "09:00:00"
    assert res["trenes"][0]["hora_llegada"] == "09:45:00"
    store.close()
