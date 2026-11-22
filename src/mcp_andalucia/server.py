"""Definición del servidor MCPServer con herramientas de turismo y transporte de Andalucía."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from mcp_andalucia.clients.ctan import CTANClient
from mcp_andalucia.clients.openrta import CATALOG_ENDPOINTS, OpenRTAClient
from mcp_andalucia.clients.opensky import OpenSkyClient
from mcp_andalucia.clients.renfe import RenfeClient
from mcp_andalucia.config import settings
from mcp_andalucia.tools.transporte import register_transporte_tools
from mcp_andalucia.tools.turismo import register_turismo_tools
from mcp_andalucia.utils import to_ndjson


def create_mcp_server() -> MCPServer:
    """Construye y configura el servidor MCP con sus 12 tools, resources y prompts."""
    mcp = MCPServer(
        name=settings.app_name,
        version=settings.version,
        instructions=(
            "Servidor MCP para planificar itinerarios turísticos por Andalucía usando datos oficiales "
            "(OpenRTA de la Junta de Andalucía, autobuses interurbanos CTAN, trenes AVE/LD/MD Renfe "
            "y conectividad aérea OpenSky). Todas las colecciones se devuelven en formato NDJSON. "
            "No incluye transporte urbano."
        ),
    )

    openrta = OpenRTAClient()
    renfe = RenfeClient()
    ctan = CTANClient()
    opensky = OpenSkyClient()

    register_turismo_tools(mcp, openrta)
    register_transporte_tools(mcp, renfe, ctan, opensky)

    @mcp.resource("openrta://catalogos/{campo}", mime_type="application/x-ndjson")
    async def catalogo_openrta(campo: str) -> str:
        """Devuelve un catálogo de OpenRTA en formato NDJSON (provinces, municipalities, objects_type, etc.)."""
        if campo not in CATALOG_ENDPOINTS:
            return to_ndjson([], meta={"error": f"Catálogo inválido: {campo}", "validos": list(CATALOG_ENDPOINTS)})
        items = await openrta.get_catalog(campo)
        return to_ndjson(items, meta={"fuente": "OpenRTA", "catalogo": campo, "total": len(items)})

    @mcp.prompt()
    def planificar_itinerario_andalucia(
        destinos: str,
        dias: int = 3,
        estilo: str = "cultural y gastronómico",
        idioma_guia: str = "español",
    ) -> str:
        """Genera un itinerario turístico multi-destino en Andalucía con alojamientos, guías, actividades y transporte."""
        return (
            f"Diseña un itinerario turístico de {dias} días por Andalucía visitando: {destinos}.\n"
            f"Estilo del viaje: {estilo}. Idioma preferido para guías: {idioma_guia}.\n\n"
            "Instrucciones:\n"
            "1. Para cada destino, llama a `explorar_destino` para conocer la oferta real de alojamientos, "
            "guías, actividades, restauración y oficinas de turismo.\n"
            "2. Si necesitas filtrar guías por idioma, usa `buscar_guias_turisticos`, y para actividades "
            "específicas usa `buscar_actividades_turisticas`.\n"
            "3. Para enlazar cada par de destinos consecutivos, llama a `recomendar_transporte_entre_destinos` "
            "(no incluyas transporte urbano).\n"
            "4. Presenta el itinerario día a día citando únicamente establecimientos, guías, servicios y "
            "horarios reales devueltos por las herramientas."
        )

    return mcp
