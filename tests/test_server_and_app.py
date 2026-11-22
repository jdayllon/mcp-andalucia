"""Tests para el servidor MCP, registro de las 12 tools, FastAPI y lógica de clientes."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcp_andalucia.app import app
from mcp_andalucia.clients.opensky import resolve_airport_icao
from mcp_andalucia.clients.patrimonio_municipios import PatrimonioMunicipiosClient
from mcp_andalucia.clients.renfe import RenfeClient
from mcp_andalucia.clients.urbano import UrbanoClient
from mcp_andalucia.db import DuckDBStore
from mcp_andalucia.server import create_mcp_server


@pytest.mark.asyncio
async def test_mcp_server_registers_all_20_tools():
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
        "buscar_declaraciones_interes_turistico",
        "consultar_municipios_sima",
        "buscar_banderas_azules",
        "buscar_itinerarios_costeros",
        "buscar_trenes_renfe",
        "buscar_autobuses_ctan",
        "consultar_conectividad_aerea",
        "recomendar_transporte_entre_destinos",
        "buscar_paradas_urbanas_cercanas",
        "consultar_tiempo_real_bus_urbano",
        "consultar_linea_urbana",
        "catalogo_nap_transporte_urbano",
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


@pytest.mark.asyncio
async def test_urbano_haversine_and_street_fallback_in_duckdb(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("mcp_andalucia.config.settings.data_dir", tmp_path)
    store = DuckDBStore(db_path=tmp_path / "urbano_test.duckdb")
    urbano = UrbanoClient(store=store)

    stops = [
        {
            "ciudad": "sevilla",
            "codigo": "43",
            "nombre": "Recaredo (Puerta Carmona)",
            "direccion": "Calle Recaredo 6-7",
            "cp": "41003",
            "lat": 37.389663,
            "lon": -5.984265,
            "lineas": "01,C4",
            "norm_search": "43 recaredo (puerta carmona) calle recaredo 6-7 41003",
        },
        {
            "ciudad": "sevilla",
            "codigo": "999",
            "nombre": "Lejana Cartuja",
            "direccion": "Isla de la Cartuja",
            "cp": "41092",
            "lat": 37.410000,
            "lon": -6.010000,
            "lineas": "C1",
            "norm_search": "999 lejana cartuja isla de la cartuja 41092",
        },
    ]
    line_stops = [
        {
            "ciudad": "sevilla",
            "linea": "C4",
            "nombre_linea": "Circular Interior",
            "sentido": "1",
            "orden": 1,
            "codigo_parada": "43",
            "nombre_parada": "Recaredo (Puerta Carmona)",
            "direccion": "Calle Recaredo 6-7",
            "lat": 37.389663,
            "lon": -5.984265,
        }
    ]
    urbano._save_city_ndjson_and_duckdb("sevilla", stops, line_stops, "urbano:city:sevilla")

    # 1. Búsqueda por coordenadas exactas (Nivel 1: Haversine + Bounding Box)
    res_coords = await urbano.find_nearby_stops(
        ciudad="sevilla",
        lat=37.3891,
        lon=-5.9845,
        radio_metros=300,
        incluir_tiempo_real=False,
    )
    assert res_coords["ubicacion_resuelta"]["precision"] == "coordenadas_exactas"
    assert len(res_coords["paradas"]) == 1
    assert res_coords["paradas"][0]["codigo"] == "43"
    assert res_coords["paradas"][0]["distancia_metros"] < 100

    # 2. Búsqueda sin coordenadas (Nivel 2: fallback por callejero en DuckDB)
    res_street = await urbano.find_nearby_stops(
        ciudad="sevilla",
        direccion_o_lugar="CL Recaredo",
        radio_metros=300,
        incluir_tiempo_real=False,
    )
    assert res_street["ubicacion_resuelta"]["precision"] == "callejero_duckdb"
    assert len(res_street["paradas"]) == 1
    assert res_street["paradas"][0]["codigo"] == "43"

    # 3. Consulta de línea
    res_line = await urbano.get_line_stops(ciudad="sevilla", linea="C4")
    assert res_line["total_paradas"] == 1
    assert res_line["paradas"][0]["codigo_parada"] == "43"
    store.close()


@pytest.mark.asyncio
async def test_declaraciones_and_sima_in_duckdb(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("mcp_andalucia.config.settings.data_dir", tmp_path)
    store = DuckDBStore(db_path=tmp_path / "patrimonio_test.duckdb")
    pat = PatrimonioMunicipiosClient(store=store)

    # 1. Probar carga y consulta de Declaraciones de Interés Turístico con DuckDB read_csv (delim='=')
    csv_file = tmp_path / "declaraciones_sample.csv"
    csv_file.write_text(
        "DECLARACIÓN DE INTERÉS TURÍSTICO =LOCALIDAD=PROVINCIA=FECHA DE RESOLUCIÓN=ÁMBITO=PROMOTOR=TIPO=BOLETÍN=URL =DESCRIPCIÓN\n"
        'Romería de Nuestra Señora de los Ángeles =Alajar=Huelva =24/01/1970=Autonómico=Ayuntamiento de Alajar=Fiesta==="Romería tradicional en la Peña de Arias Montano."\n'
        'Real Feria de la Vendimia=La Palma del Condado=Huelva =04/02/1972=Autonómico=Ayuntamiento=Fiesta==="Feria de septiembre dedicada al vino."\n',
        encoding="utf-8",
    )
    cnt = pat.load_declaraciones_from_csv_with_duckdb(csv_file)
    assert cnt == 2

    res_decl = await pat.search_declaraciones(provincia="Huelva", municipio="Alájar")
    assert res_decl["total_coincidencias"] == 1
    assert "Romería" in res_decl["declaraciones"][0]["nombre"]

    # 2. Probar consulta de municipios SIMA y pueblos cercanos con Haversine en DuckDB
    sima_ndjson = tmp_path / "ndjson" / "municipios_sima.ndjson"
    sima_ndjson.parent.mkdir(parents=True, exist_ok=True)
    sima_rows = [
        {
            "cod_ine": "04001",
            "municipio": "Abla",
            "provincia": "Almería",
            "lat": 37.141,
            "lon": -2.78,
            "altitud_m": 843,
            "poblacion": 1267,
            "plazas_hoteles": 20,
            "total_patrimonio_iaph": 67,
            "url_guia_iaph": "https://guiadigital.iaph.es/municipio/280/abla",
            "norm_municipio": "abla",
            "norm_provincia": "almeria",
            "indicadores_json": '{"Población total. 2025": 1267}',
        },
        {
            "cod_ine": "04002",
            "municipio": "Abrucena",
            "provincia": "Almería",
            "lat": 37.133,
            "lon": -2.797,
            "altitud_m": 976,
            "poblacion": 1200,
            "plazas_hoteles": 15,
            "total_patrimonio_iaph": 40,
            "url_guia_iaph": "https://guiadigital.iaph.es/municipio/281/abrucena",
            "norm_municipio": "abrucena",
            "norm_provincia": "almeria",
            "indicadores_json": '{"Población total. 2025": 1200}',
        },
    ]
    sima_ndjson.write_text("\n".join(json.dumps(r) for r in sima_rows) + "\n", encoding="utf-8")
    store.load_ndjson_table("municipios_sima", sima_ndjson)
    store.mark_fresh("junta:municipios_sima", 2)

    ficha = await pat.get_municipio_sima(municipio="Abla", incluir_124_variables=True)
    assert len(ficha) == 1
    assert ficha[0]["cod_ine"] == "04001"
    assert ficha[0]["indicadores_completos"]["Población total. 2025"] == 1267

    cercanos = await pat.find_nearby_municipios_sima(municipio_origen="Abla", radio_km=10.0)
    assert cercanos["total_en_radio"] == 2
    assert cercanos["municipios"][1]["municipio"] == "Abrucena"

    # 3. Probar carga y búsqueda de Banderas Azules con DuckDB read_json_auto
    banderas_file = tmp_path / "banderas_sample.json"
    banderas_file.write_text(
        json.dumps(
            [
                {
                    "Provincia": "Almería",
                    "Municipio": "Adra",
                    "Categoría": "Playa",
                    "Bandera azul": "San Nicolás",
                    "Año": "2023",
                    "Notas": "",
                },
                {
                    "Provincia": "Almería",
                    "Municipio": "Adra",
                    "Categoría": "Playa",
                    "Bandera azul": "San Nicolás",
                    "Año": "2026",
                    "Notas": "Accesible",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cnt_b = pat.load_banderas_azules_from_json_with_duckdb(banderas_file)
    assert cnt_b == 2

    res_b_latest = await pat.search_banderas_azules(municipio="Adra", solo_ultimo_anio=True)
    assert res_b_latest["anio_filtrado"] == "2026"
    assert res_b_latest["total_coincidencias"] == 1
    assert res_b_latest["banderas_azules"][0]["nombre"] == "San Nicolás"

    # 4. Probar carga XML y búsqueda de Itinerarios Turísticos Costeros en DuckDB
    sample_xml = b"""<rutaList>
        <ruta>
            <id>02</id>
            <codigo>02</codigo>
            <denominacion>Itinerario de recursos turisticos-costeros de Ayamonte</denominacion>
            <naturaleza>Terrestre</naturaleza>
            <descripcion>Ruta por Ayamonte e Isla Cristina</descripcion>
            <temaList><tema>Patrimonio arquitectonico</tema></temaList>
            <recursoList>
                <recurso>
                    <nombre>Puerto pesquero de Isla Cristina</nombre>
                    <tipo>Patrimonio arquitectonico</tipo>
                    <ubicacion>
                        <provincia-distrito>Huelva</provincia-distrito>
                        <municipio>Isla Cristina</municipio>
                    </ubicacion>
                    <categoriaList><categoria>Puerto pesquero</categoria></categoriaList>
                    <descripcion>Puerto historico sardinero y atunero.</descripcion>
                    <coordenadas>
                        <latitud>37.201917</latitud>
                        <longitud>-7.326167</longitud>
                    </coordenadas>
                </recurso>
            </recursoList>
        </ruta>
    </rutaList>"""
    cnt_c = pat.load_itinerarios_costeros_from_xml_bytes(sample_xml)
    assert cnt_c == 1

    res_cost = await pat.search_itinerarios_costeros(
        municipio="Isla Cristina",
        lat=37.200,
        lon=-7.325,
        radio_km=5.0,
    )
    assert res_cost["total_devueltos"] == 1
    assert res_cost["puntos_itinerario"][0]["nombre"] == "Puerto pesquero de Isla Cristina"
    assert res_cost["puntos_itinerario"][0]["distancia_km"] < 1.0
    store.close()
