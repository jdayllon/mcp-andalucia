"""Herramientas MCP de transporte urbano (TUSSAM Sevilla, EMT Málaga y NAP Ministerio de Transportes)."""

from __future__ import annotations

from typing import Literal

from mcp.server.mcpserver import MCPServer

from mcp_andalucia.clients.urbano import NAP_CITY_PRESETS, UrbanoClient
from mcp_andalucia.utils import to_ndjson


def register_urbano_tools(mcp: MCPServer, urbano: UrbanoClient) -> None:
    """Registra las 4 herramientas de movilidad urbana en el servidor MCP."""

    @mcp.tool()
    async def buscar_paradas_urbanas_cercanas(
        ciudad: str = "sevilla",
        lat: float | None = None,
        lon: float | None = None,
        direccion_o_lugar: str | None = None,
        codigo_postal: str | None = None,
        radio_metros: int = 400,
        limite: int = 8,
        incluir_tiempo_real: bool = True,
    ) -> str:
        """Busca paradas de autobús/metro urbano cercanas a unas coordenadas (`lat`, `lon`) o dirección/calle.

        Soporta de serie `sevilla` (TUSSAM), `malaga` (EMT Málaga), `cordoba` (AUCORSA vía NAP),
        `granada` (Urbano Granada vía NAP), `metro_sevilla`, `metro_malaga`, `metro_granada`,
        y cualquier ciudad cargada con `catalogo_nap_transporte_urbano`.
        Si el recurso turístico no tiene coordenadas, resuelve por callejero local en DuckDB o geocodificación OSM.
        Devuelve los resultados en formato NDJSON ordenados por distancia Haversine en metros.
        """
        res = await urbano.find_nearby_stops(
            ciudad=ciudad,
            lat=lat,
            lon=lon,
            direccion_o_lugar=direccion_o_lugar,
            codigo_postal=codigo_postal,
            radio_metros=radio_metros,
            limite=limite,
            incluir_tiempo_real=incluir_tiempo_real,
        )
        return to_ndjson(
            res["paradas"],
            meta={
                "fuente": "Transporte Urbano (DuckDB Haversine)",
                "ciudad": res["ciudad"],
                "ubicacion_resuelta": res["ubicacion_resuelta"],
                "radio_metros": res["radio_metros"],
                "total_en_radio": res["total_en_radio"],
                "devueltos": len(res["paradas"]),
            },
        )

    @mcp.tool()
    async def consultar_tiempo_real_bus_urbano(
        ciudad: Literal["sevilla", "malaga"],
        codigo_parada: str,
        linea: str | None = None,
    ) -> str:
        """Consulta tiempos de espera y distancia de los próximos autobuses urbanos en tiempo real.

        - `ciudad='sevilla'`: consulta tiempos de llegada en tiempo real de TUSSAM para `codigo_parada`.
        - `ciudad='malaga'`: cruza la parada con la posición GPS en tiempo real de la flota de EMT Málaga.
        Devuelve las estimaciones en formato NDJSON.
        """
        res = await urbano.get_realtime_stop(
            ciudad=ciudad,
            codigo_parada=codigo_parada,
            linea=linea,
        )
        return to_ndjson(
            res.get("estimaciones", []),
            meta={
                "fuente": "TUSSAM Realtime" if ciudad == "sevilla" else "EMT Málaga GPS Realtime",
                "ciudad": res.get("ciudad"),
                "codigo_parada": codigo_parada,
                "nombre_parada": res.get("nombre_parada"),
                "disponible": res.get("disponible"),
                "mensaje": res.get("mensaje"),
                "devueltos": len(res.get("estimaciones", [])),
            },
        )

    @mcp.tool()
    async def consultar_linea_urbana(
        ciudad: str,
        linea: str,
        sentido: str | None = None,
    ) -> str:
        """Devuelve el recorrido completo y paradas ordenadas de una línea de autobús/metro urbano en NDJSON.

        Ejemplos: `ciudad='sevilla', linea='C1'` o `linea='T1'`; `ciudad='malaga', linea='1'`; `ciudad='cordoba', linea='3'`.
        """
        res = await urbano.get_line_stops(ciudad=ciudad, linea=linea, sentido=sentido)
        return to_ndjson(
            res["paradas"],
            meta={
                "fuente": "Transporte Urbano (DuckDB)",
                "ciudad": res["ciudad"],
                "linea": res["linea"],
                "total_paradas": res["total_paradas"],
            },
        )

    @mcp.tool()
    async def catalogo_nap_transporte_urbano(
        accion: Literal["listar", "cargar_gtfs"] = "listar",
        filtro: str | None = None,
        fichero_id: int | None = None,
        nombre_ciudad: str | None = None,
    ) -> str:
        """Consulta conjuntos de datos oficiales de Andalucía en el NAP (Ministerio de Transportes) o indexa un GTFS en DuckDB.

        - `accion='listar'`: lista los datasets de transporte en Andalucía del NAP (con sus `fichero_id` GTFS-ZIP).
        - `accion='cargar_gtfs'`: descarga el `fichero_id` GTFS-ZIP del NAP y lo indexa en DuckDB bajo `nombre_ciudad`
          para poder buscar paradas cercanas y líneas en esa red.
        """
        if accion == "cargar_gtfs":
            if not fichero_id or not nombre_ciudad:
                return to_ndjson(
                    [],
                    meta={
                        "error": "Para `accion='cargar_gtfs'` debes indicar `fichero_id` y `nombre_ciudad`.",
                        "presets_disponibles": NAP_CITY_PRESETS,
                    },
                )
            total_stops = await urbano.load_nap_gtfs(ciudad=nombre_ciudad, fichero_id=fichero_id)
            return to_ndjson(
                [{"ciudad": nombre_ciudad, "fichero_id": fichero_id, "paradas_indexadas": total_stops}],
                meta={"fuente": "NAP Ministerio de Transportes", "accion": "cargar_gtfs"},
            )

        datasets = await urbano.list_nap_andalucia_datasets(filtro=filtro)
        return to_ndjson(
            datasets,
            meta={
                "fuente": "NAP Ministerio de Transportes",
                "accion": "listar",
                "presets_automaticos": NAP_CITY_PRESETS,
                "devueltos": len(datasets),
            },
        )
