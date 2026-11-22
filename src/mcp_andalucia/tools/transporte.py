"""Herramientas MCP de transporte interurbano y multimodal (Renfe, CTAN y OpenSky)."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.mcpserver import MCPServer

from mcp_andalucia.clients.ctan import CTANClient
from mcp_andalucia.clients.opensky import OpenSkyClient, resolve_airport_icao
from mcp_andalucia.clients.renfe import RenfeClient
from mcp_andalucia.utils import to_ndjson


def register_transporte_tools(
    mcp: MCPServer,
    renfe: RenfeClient,
    ctan: CTANClient,
    opensky: OpenSkyClient,
) -> None:
    """Registra las 4 herramientas de transporte entre destinos en el servidor MCP."""

    @mcp.tool()
    async def buscar_trenes_renfe(
        origen: str,
        destino: str,
        fecha: str | None = None,
        limite: int = 15,
    ) -> str:
        """Busca trenes AVE, Larga Distancia y Media Distancia entre dos ciudades usando el GTFS de Renfe en DuckDB.

        `fecha` es opcional en formato `YYYY-MM-DD`. Excluye Cercanías urbanos.
        Devuelve los resultados en formato NDJSON.
        """
        res = await renfe.search_trains(origen=origen, destino=destino, fecha=fecha, limite=limite)
        return to_ndjson(
            res.get("trenes", []),
            meta={
                "fuente": "Renfe GTFS AV/LD/MD + GTFS-RT (DuckDB)",
                "origen": origen,
                "destino": destino,
                "fecha": res.get("fecha"),
                "cobertura": res.get("cobertura"),
                "mensaje": res.get("mensaje"),
                "devueltos": len(res.get("trenes", [])),
            },
        )

    @mcp.tool()
    async def buscar_autobuses_ctan(
        origen: str,
        destino: str,
        provincia_o_consorcio: str | None = None,
        limite: int = 20,
    ) -> str:
        """Busca autobuses interurbanos dentro de los 9 Consorcios Metropolitanos de Andalucía (CTAN).

        Ideal para conectar una capital con municipios cercanos (ej. Sevilla <-> Carmona, Málaga <-> Nerja).
        No cubre autobuses urbanos ni líneas interprovinciales de larga distancia.
        Devuelve los resultados en formato NDJSON.
        """
        res = await ctan.search_horarios(
            origen=origen,
            destino=destino,
            provincia_o_consorcio=provincia_o_consorcio,
            limite=limite,
        )
        return to_ndjson(
            res.get("servicios", []),
            meta={
                "fuente": "CTAN API v1",
                "cobertura": res.get("cobertura"),
                "consorcio": res.get("consorcio"),
                "origen": res.get("origen", origen),
                "destino": res.get("destino", destino),
                "mensaje": res.get("mensaje"),
                "total_servicios": res.get("total_servicios", 0),
                "devueltos": len(res.get("servicios", [])),
            },
        )

    @mcp.tool()
    async def consultar_conectividad_aerea(
        origen: str | None = None,
        destino: str | None = None,
        horas_atras: int = 36,
    ) -> str:
        """Consulta conectividad aérea reciente en aeropuertos andaluces mediante OpenSky Network.

        Acepta ciudad o código IATA/ICAO (ej. 'Sevilla', 'SVQ', 'LEZL', 'Málaga', 'AGP', 'Madrid', 'MAD').
        Nota: refleja tráfico real reciente observado, no horarios futuros ni precios de billetes.
        """
        res = await opensky.get_recent_connectivity(
            origen=origen,
            destino=destino,
            horas_atras=horas_atras,
        )
        return to_ndjson(
            res.get("vuelos", []),
            meta={
                "fuente": "OpenSky Network",
                "cobertura": res.get("cobertura"),
                "aeropuerto_consultado": res.get("aeropuerto_consultado"),
                "nota": res.get("nota") or res.get("mensaje") or res.get("error"),
                "devueltos": len(res.get("vuelos", [])),
            },
        )

    @mcp.tool()
    async def recomendar_transporte_entre_destinos(
        origen: str,
        destino: str,
        fecha: str | None = None,
    ) -> str:
        """Compara opciones reales de transporte interurbano entre dos destinos (tren Renfe, bus CTAN y vuelos OpenSky).

        Diseñada para enlazar etapas de un itinerario turístico sin transporte urbano.
        Devuelve un NDJSON con las opciones encontradas por cada modo y recomendaciones de cobertura.
        """
        # Lanzamos Renfe y CTAN siempre; OpenSky solo si al menos uno tiene aeropuerto conocido
        has_airport = bool(resolve_airport_icao(origen) and resolve_airport_icao(destino))

        tasks: list[Any] = [
            renfe.search_trains(origen=origen, destino=destino, fecha=fecha, limite=8),
            ctan.search_horarios(origen=origen, destino=destino, limite=8),
        ]
        if has_airport:
            tasks.append(opensky.get_recent_connectivity(origen=origen, destino=destino, horas_atras=36))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        renfe_res = results[0] if not isinstance(results[0], Exception) else {"cobertura": False, "trenes": [], "error": str(results[0])}
        ctan_res = results[1] if not isinstance(results[1], Exception) else {"cobertura": False, "servicios": [], "error": str(results[1])}
        opensky_res = (
            results[2]
            if has_airport and not isinstance(results[2], Exception)
            else {"cobertura": False, "vuelos": []}
        )

        records: list[dict[str, Any]] = []
        for t in renfe_res.get("trenes", []):
            records.append({"modo": "tren_renfe", **t})
        for b in ctan_res.get("servicios", []):
            records.append({"modo": "autobus_ctan", **b})
        for v in opensky_res.get("vuelos", [])[:5]:
            records.append({"modo": "vuelo_opensky", **v})

        avisos: list[str] = []
        if not records:
            avisos.append(
                "No se encontraron conexiones directas en Renfe AVE/LD/MD ni en los consorcios metropolitanos CTAN. "
                "Para trayectos interprovinciales sin tren directo puede ser necesario autobús de larga distancia o vehículo."
            )

        return to_ndjson(
            records,
            meta={
                "origen": origen,
                "destino": destino,
                "fecha": fecha,
                "resumen_modos": {
                    "tren_renfe": len(renfe_res.get("trenes", [])),
                    "autobus_ctan": len(ctan_res.get("servicios", [])),
                    "vuelo_opensky": len(opensky_res.get("vuelos", [])),
                },
                "avisos": avisos,
            },
        )
