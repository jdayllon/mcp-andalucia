"""Herramientas MCP de oferta turística basadas en OpenRTA (Junta de Andalucía)."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from mcp_andalucia.clients.openrta import OpenRTAClient
from mcp_andalucia.clients.patrimonio_municipios import PatrimonioMunicipiosClient
from mcp_andalucia.utils import normalize_text, to_ndjson

ALOJAMIENTO_TYPES: dict[str, str] = {
    "hotel": "Establecimiento Hotelero",
    "apartamento": "Apartamento turístico",
    "casa_rural": "Casa rural",
    "complejo_rural": "Complejo turístico rural",
    "camping": "Campamento de turismo",
    "vivienda_turistica": "Vivienda de uso turístico",
    "vtar": "Vivienda turística de alojamiento rural",
}

ACTIVIDADES_TYPES: tuple[str, ...] = (
    "Empresa de turismo activo",
    "EC. Turismo ecológico",
    "AIAT. Ocio, entretenimiento y esparcimiento",
    "AIAT. Actividades deportivas",
    "AIAT. Actividades relacionadas con el bienestar personal",
    "AIAT. Transporte turístico, autobuses, coches de caballo, alquiler de bicicletas u otros",
    "AIAT. Puertos deportivos",
)


def register_turismo_tools(
    mcp: MCPServer,
    client: OpenRTAClient,
    patrimonio: PatrimonioMunicipiosClient | None = None,
) -> None:
    """Registra las herramientas de oferta turística (OpenRTA), Declaraciones de Interés Turístico y SIMA."""
    pat_client = patrimonio or PatrimonioMunicipiosClient(store=client.store)

    @mcp.tool()
    async def buscar_alojamientos(
        provincia: str | None = None,
        municipio: str | None = None,
        tipo: Literal[
            "hotel",
            "apartamento",
            "casa_rural",
            "complejo_rural",
            "camping",
            "vivienda_turistica",
            "vtar",
        ] = "hotel",
        categoria: str | None = None,
        modalidad: str | None = None,
        ordenar_por: Literal["id", "tot_gen_places", "name"] = "id",
        direccion: Literal["ASC", "DESC"] = "ASC",
        limite: int = 10,
    ) -> str:
        """Busca alojamientos turísticos en Andalucía (hoteles, apartamentos, casas rurales, campings, VUT).

        Devuelve los resultados en formato NDJSON (primera línea `_meta` + una línea JSON por alojamiento).
        """
        object_type = ALOJAMIENTO_TYPES.get(tipo, "Establecimiento Hotelero")
        res = await client.search(
            object_type=object_type,
            province=provincia,
            municipality=municipio,
            category=categoria,
            modality=modalidad,
            order_by=ordenar_por,
            mode=direccion,
            size=limite,
        )
        return to_ndjson(
            res["results"],
            meta={
                "fuente": "OpenRTA",
                "tipo": object_type,
                "total_disponibles": res["total_hits"],
                "devueltos": res["hits"],
                "filtros": res["filtros_aplicados"],
            },
        )

    @mcp.tool()
    async def buscar_guias_turisticos(
        provincia: str | None = None,
        municipio: str | None = None,
        idioma: str | None = None,
        limite: int = 10,
    ) -> str:
        """Busca guías de turismo oficiales en Andalucía, con filtro opcional por idioma (ej. 'inglés', 'francés').

        Devuelve los resultados en formato NDJSON con idiomas y contacto directo del guía.
        """
        fetch_size = 50 if idioma else max(1, min(limite, 50))
        res = await client.search(
            object_type="Guía de turismo",
            province=provincia,
            municipality=municipio,
            size=fetch_size,
        )
        results = res["results"]
        if idioma:
            q_lang = normalize_text(idioma)
            results = [r for r in results if q_lang in normalize_text(r.get("idiomas", ""))]
        sliced = results[: max(1, min(limite, 50))]
        return to_ndjson(
            sliced,
            meta={
                "fuente": "OpenRTA",
                "tipo": "Guía de turismo",
                "total_disponibles": res["total_hits"],
                "filtro_idioma": idioma,
                "devueltos": len(sliced),
            },
        )

    @mcp.tool()
    async def buscar_actividades_turisticas(
        provincia: str | None = None,
        municipio: str | None = None,
        actividad: str | None = None,
        limite: int = 15,
    ) -> str:
        """Busca empresas de turismo activo, ecoturismo y ocio/experiencias en Andalucía.

        Permite filtrar por palabra clave de actividad (ej. 'senderismo', 'kayak', 'bicicleta', 'buceo').
        Devuelve los resultados en formato NDJSON.
        """
        # Si hay filtro de actividad, ampliamos el muestreo sobre turismo activo y ecoturismo
        target_types = (
            ("Empresa de turismo activo", "EC. Turismo ecológico")
            if actividad
            else ACTIVIDADES_TYPES[:4]
        )
        tasks = [
            client.search(
                object_type=t,
                province=provincia,
                municipality=municipio,
                size=50 if actividad else 10,
            )
            for t in target_types
        ]
        responses = await asyncio.gather(*tasks)

        combined: list[dict[str, Any]] = []
        total_hits = 0
        for r in responses:
            total_hits += int(r.get("total_hits", 0))
            combined.extend(r.get("results", []))

        if actividad:
            q_act = normalize_text(actividad)
            combined = [
                item
                for item in combined
                if q_act in normalize_text(item.get("actividades", ""))
                or q_act in normalize_text(item.get("nombre", ""))
                or q_act in normalize_text(item.get("tipo", ""))
            ]

        sliced = combined[: max(1, min(limite, 50))]
        return to_ndjson(
            sliced,
            meta={
                "fuente": "OpenRTA",
                "filtro_actividad": actividad,
                "total_aprox": total_hits,
                "devueltos": len(sliced),
            },
        )

    @mcp.tool()
    async def buscar_restauracion(
        provincia: str | None = None,
        municipio: str | None = None,
        limite: int = 10,
    ) -> str:
        """Busca establecimientos de restauración y catering turístico inscritos en OpenRTA (NDJSON)."""
        res = await client.search(
            object_type="REST. Restauración y catering turístico",
            province=provincia,
            municipality=municipio,
            size=limite,
        )
        return to_ndjson(
            res["results"],
            meta={
                "fuente": "OpenRTA",
                "tipo": "REST. Restauración y catering turístico",
                "total_disponibles": res["total_hits"],
                "devueltos": res["hits"],
            },
        )

    @mcp.tool()
    async def buscar_oficinas_turismo(
        provincia: str | None = None,
        municipio: str | None = None,
        limite: int = 10,
    ) -> str:
        """Busca oficinas de turismo y puntos de información turística en Andalucía (NDJSON)."""
        r_ofi, r_pto = await asyncio.gather(
            client.search(
                object_type="Oficina de turismo",
                province=provincia,
                municipality=municipio,
                size=limite,
            ),
            client.search(
                object_type="Punto de información turística",
                province=provincia,
                municipality=municipio,
                size=limite,
            ),
        )
        combined = (r_ofi["results"] + r_pto["results"])[: max(1, min(limite, 50))]
        return to_ndjson(
            combined,
            meta={
                "fuente": "OpenRTA",
                "total_disponibles": int(r_ofi["total_hits"]) + int(r_pto["total_hits"]),
                "devueltos": len(combined),
            },
        )

    @mcp.tool()
    async def explorar_destino(
        provincia: str | None = None,
        municipio: str | None = None,
        limite_por_bloque: int = 4,
    ) -> str:
        """Obtiene una radiografía completa de un destino andaluz para planificar un itinerario.

        Consulta en paralelo hoteles, casas rurales/apartamentos, guías turísticos, actividades,
        restauración y oficinas de turismo, devolviendo un NDJSON con bloques etiquetados.
        """
        n = max(1, min(limite_por_bloque, 10))
        bloques_config = [
            ("hoteles", "Establecimiento Hotelero"),
            ("apartamentos", "Apartamento turístico"),
            ("casas_rurales", "Casa rural"),
            ("guias", "Guía de turismo"),
            ("turismo_activo", "Empresa de turismo activo"),
            ("restauracion", "REST. Restauración y catering turístico"),
            ("oficinas_turismo", "Oficina de turismo"),
        ]
        responses = await asyncio.gather(
            *[
                client.search(
                    object_type=obj_type,
                    province=provincia,
                    municipality=municipio,
                    size=n,
                )
                for _, obj_type in bloques_config
            ]
        )

        records: list[dict[str, Any]] = []
        resumen_totales: dict[str, int] = {}
        for (bloque, _), resp in zip(bloques_config, responses):
            resumen_totales[bloque] = int(resp.get("total_hits", 0))
            for item in resp.get("results", []):
                records.append({"bloque": bloque, **item})

        # Enriquecer con ficha municipal SIMA, Declaraciones de Interés Turístico y Banderas Azules en DuckDB
        try:
            if municipio or provincia:
                sima_rows, decl_res, band_res = await asyncio.gather(
                    pat_client.get_municipio_sima(
                        municipio=municipio,
                        provincia=provincia,
                        limite=1 if municipio else 3,
                    ),
                    pat_client.search_declaraciones(
                        provincia=provincia,
                        municipio=municipio,
                        limite=n,
                    ),
                    pat_client.search_banderas_azules(
                        provincia=provincia,
                        municipio=municipio,
                        solo_ultimo_anio=True,
                        limite=n,
                    ),
                )
                for s in sima_rows:
                    records.append({"bloque": "ficha_municipal_sima", **s})
                resumen_totales["declaraciones_interes_turistico"] = decl_res["total_coincidencias"]
                for d in decl_res["declaraciones"]:
                    records.append({"bloque": "declaracion_interes_turistico", **d})
                resumen_totales["banderas_azules"] = band_res["total_coincidencias"]
                for b in band_res["banderas_azules"]:
                    records.append({"bloque": "bandera_azul", **b})
        except Exception:
            pass

        return to_ndjson(
            records,
            meta={
                "fuente": "OpenRTA + SIMA + Declaraciones Interés Turístico + Banderas Azules",
                "destino": {"provincia": provincia, "municipio": municipio},
                "totales_por_bloque": resumen_totales,
                "devueltos": len(records),
            },
        )

    @mcp.tool()
    async def obtener_detalle_elemento(id_registro: int) -> str:
        """Obtiene la ficha completa de un establecimiento, guía o servicio de OpenRTA por su `id`.

        Incluye coordenadas WGS84 (`lat`, `lon`), titular, fechas de inscripción, capacidades y contacto.
        """
        detail = await client.get_by_id(id_registro)
        return to_ndjson([detail], meta={"fuente": "OpenRTA", "id_registro": id_registro})

    @mcp.tool()
    async def buscar_en_registro(
        accion: Literal["buscar", "catalogo", "estado"] = "buscar",
        tipo_objeto: str | None = None,
        provincia: str | None = None,
        municipio: str | None = None,
        campo_catalogo: Literal[
            "provinces", "municipalities", "objects_type", "categories", "groups", "modalities"
        ] = "objects_type",
        contiene: str | None = None,
        limite: int = 15,
    ) -> str:
        """Herramienta comodín para consultar cualquier tipo de OpenRTA, sus catálogos válidos o el estado del dataset.

        - `accion='buscar'`: busca cualquier `tipo_objeto` (ej. 'Agencia de viajes', 'Campamento de turismo').
        - `accion='catalogo'`: lista valores válidos de `campo_catalogo` filtrados opcionalmente por `contiene`.
        - `accion='estado'`: devuelve total de registros y fecha de última actualización.
        """
        if accion == "estado":
            status = await client.get_dataset_status()
            return to_ndjson([status], meta={"fuente": "OpenRTA", "accion": "estado"})

        if accion == "catalogo":
            items = await client.get_catalog(campo_catalogo)
            if contiene:
                q = normalize_text(contiene)
                items = [i for i in items if q in i["norm_value"]]
            sliced = [
                {"valor": i["value"], **({"registros": i["meta"]["doc_count"]} if i.get("meta") else {})}
                for i in items[: max(1, min(limite, 100))]
            ]
            return to_ndjson(
                sliced,
                meta={
                    "fuente": "OpenRTA",
                    "accion": "catalogo",
                    "campo": campo_catalogo,
                    "total_coincidencias": len(items),
                    "devueltos": len(sliced),
                },
            )

        res = await client.search(
            object_type=tipo_objeto,
            province=provincia,
            municipality=municipio,
            size=limite,
        )
        return to_ndjson(
            res["results"],
            meta={
                "fuente": "OpenRTA",
                "accion": "buscar",
                "total_disponibles": res["total_hits"],
                "devueltos": res["hits"],
            },
        )

    @mcp.tool()
    async def buscar_declaraciones_interes_turistico(
        provincia: str | None = None,
        municipio: str | None = None,
        tipo: str | None = None,
        ambito: str | None = None,
        texto: str | None = None,
        url_recurso: str | None = None,
        limite: int = 20,
    ) -> str:
        """Consulta con DuckDB las Declaraciones de Interés Turístico de Andalucía (fiestas, romerías, semanas santas, municipios turísticos).

        Permite filtrar por `provincia`, `municipio`, `tipo` (ej. 'Fiesta'), `ambito` ('Autonómico', 'Nacional')
        o `texto` libre en nombre/descripción. Devuelve los resultados en formato NDJSON.
        """
        res = await pat_client.search_declaraciones(
            provincia=provincia,
            municipio=municipio,
            tipo=tipo,
            ambito=ambito,
            texto=texto,
            url_recurso=url_recurso,
            limite=limite,
        )
        return to_ndjson(
            res["declaraciones"],
            meta={
                "fuente": "Junta de Andalucía - Declaraciones de Interés Turístico (DuckDB)",
                "total_coincidencias": res["total_coincidencias"],
                "devueltos": res["devueltos"],
            },
        )

    @mcp.tool()
    async def consultar_municipios_sima(
        accion: Literal["ficha", "cercanos"] = "ficha",
        municipio: str | None = None,
        provincia: str | None = None,
        cod_ine: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        radio_km: float = 25.0,
        incluir_124_variables: bool = False,
        limite: int = 15,
    ) -> str:
        """Consulta en DuckDB los datos oficiales de los 785 municipios de Andalucía del SIMA ('Andalucía pueblo a pueblo', `smex99.xls`).

        - `accion='ficha'`: devuelve coordenadas del núcleo principal (`lat`, `lon`), altitud, población,
          distancia a la capital, plazas hoteleras, taxis, patrimonio histórico del IAPH (bienes muebles,
          inmuebles, inmateriales y enlace a la Guía Digital del IAPH) y opcionalmente las 124 variables completas.
        - `accion='cercanos'`: busca pueblos cercanos a `municipio` (o a `lat`/`lon`) dentro de `radio_km`
          usando cálculo espacial Haversine en DuckDB para diseñar rutas de pueblos y excursiones.
        """
        if accion == "cercanos":
            res = await pat_client.find_nearby_municipios_sima(
                municipio_origen=municipio,
                lat=lat,
                lon=lon,
                radio_km=radio_km,
                limite=limite,
            )
            return to_ndjson(
                res["municipios"],
                meta={
                    "fuente": "SIMA IECA - Andalucía pueblo a pueblo (DuckDB Haversine)",
                    "accion": "cercanos",
                    "origen": res["origen"],
                    "radio_km": res["radio_km"],
                    "total_en_radio": res["total_en_radio"],
                    "devueltos": len(res["municipios"]),
                },
            )

        rows = await pat_client.get_municipio_sima(
            municipio=municipio,
            provincia=provincia,
            cod_ine=cod_ine,
            incluir_124_variables=incluir_124_variables,
            limite=limite,
        )
        return to_ndjson(
            rows,
            meta={
                "fuente": "SIMA IECA - Andalucía pueblo a pueblo (DuckDB)",
                "accion": "ficha",
                "devueltos": len(rows),
            },
        )

    @mcp.tool()
    async def buscar_banderas_azules(
        provincia: str | None = None,
        municipio: str | None = None,
        categoria: str | None = None,
        anio: str | None = None,
        solo_ultimo_anio: bool = True,
        texto: str | None = None,
        limite: int = 30,
    ) -> str:
        """Consulta en DuckDB las playas, puertos deportivos y embarcaciones con Bandera Azul en Andalucía.

        Por defecto (`solo_ultimo_anio=True`) devuelve las banderas azules de la última edición disponible;
        pon `solo_ultimo_anio=False` o indica `anio` (ej. '2026', '2025') para consultar el histórico.
        Devuelve los resultados en formato NDJSON.
        """
        res = await pat_client.search_banderas_azules(
            provincia=provincia,
            municipio=municipio,
            categoria=categoria,
            anio=anio,
            solo_ultimo_anio=solo_ultimo_anio,
            texto=texto,
            limite=limite,
        )
        return to_ndjson(
            res["banderas_azules"],
            meta={
                "fuente": "Junta de Andalucía - Banderas Azules (DuckDB)",
                "anio_filtrado": res["anio_filtrado"],
                "total_coincidencias": res["total_coincidencias"],
                "devueltos": res["devueltos"],
            },
        )
