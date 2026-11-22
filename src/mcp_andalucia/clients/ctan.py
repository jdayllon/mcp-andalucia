"""Cliente asíncrono para la API REST de los Consorcios de Transporte de Andalucía (CTAN)."""

from __future__ import annotations

import time
from typing import Any

import httpx

from mcp_andalucia.config import settings
from mcp_andalucia.db import DuckDBStore, get_store
from mcp_andalucia.logging_config import get_logger
from mcp_andalucia.utils import normalize_text

logger = get_logger("clients.ctan")


class CTANClient:
    """Cliente para autobuses interurbanos de los 9 Consorcios de Transporte de Andalucía."""

    def __init__(self, store: DuckDBStore | None = None) -> None:
        self.base_url = settings.ctan_base_url.rstrip("/")
        self.store = store or get_store()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=settings.http_timeout_seconds,
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )

    async def get_consorcios(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        """Obtiene la lista de los 9 consorcios andaluces."""
        ns = "ctan:consorcios"
        if not force_refresh and self.store.is_fresh(f"catalog:{ns}", settings.catalog_ttl_seconds):
            cached = self.store.get_catalog(ns)
            if cached:
                return [
                    {
                        "idConsorcio": item["meta"]["idConsorcio"],
                        "nombre": item["value"],
                        "nombreCorto": item["meta"].get("nombreCorto"),
                    }
                    for item in cached
                    if item.get("meta")
                ]

        async with self._client() as client:
            resp = await client.get("/Consorcios/consorcios")
            resp.raise_for_status()
            data = resp.json()

        consorcios = data.get("consorcios", [])
        catalog_items = [
            {
                "value": c["nombre"],
                "norm_value": normalize_text(c["nombre"]),
                "meta": {"idConsorcio": str(c["idConsorcio"]), "nombreCorto": c.get("nombreCorto")},
            }
            for c in consorcios
        ]
        self.store.save_catalog(ns, catalog_items)
        return consorcios

    async def get_nucleos(self, id_consorcio: str, force_refresh: bool = False) -> list[dict[str, Any]]:
        """Obtiene los núcleos de población de un consorcio."""
        ns = f"ctan:nucleos:{id_consorcio}"
        if not force_refresh and self.store.is_fresh(f"catalog:{ns}", settings.catalog_ttl_seconds):
            cached = self.store.get_catalog(ns)
            if cached:
                return [
                    {
                        "idNucleo": item["meta"]["idNucleo"],
                        "idMunicipio": item["meta"].get("idMunicipio"),
                        "idZona": item["meta"].get("idZona"),
                        "nombre": item["value"],
                    }
                    for item in cached
                    if item.get("meta")
                ]

        async with self._client() as client:
            resp = await client.get(f"/Consorcios/{id_consorcio}/nucleos")
            resp.raise_for_status()
            data = resp.json()

        nucleos = data.get("nucleos", [])
        catalog_items = [
            {
                "value": n["nombre"].strip(),
                "norm_value": normalize_text(n["nombre"]),
                "meta": {
                    "idNucleo": str(n["idNucleo"]),
                    "idMunicipio": n.get("idMunicipio"),
                    "idZona": n.get("idZona"),
                },
            }
            for n in nucleos
            if n.get("nombre")
        ]
        self.store.save_catalog(ns, catalog_items)
        return nucleos

    def _match_nucleo(self, query: str, nucleos: list[dict[str, Any]]) -> dict[str, Any] | None:
        q = normalize_text(query)
        if not q:
            return None
        exact = [n for n in nucleos if normalize_text(n["nombre"]) == q]
        if exact:
            return exact[0]
        prefix = [n for n in nucleos if normalize_text(n["nombre"]).startswith(q)]
        if prefix:
            # Preferir el nombre más corto (evita "(zona B)" o urbanizaciones frente a la capital)
            return sorted(prefix, key=lambda x: len(x["nombre"]))[0]
        contains = [n for n in nucleos if q in normalize_text(n["nombre"])]
        if contains:
            return sorted(contains, key=lambda x: len(x["nombre"]))[0]
        return None

    async def search_horarios(
        self,
        origen: str,
        destino: str,
        provincia_o_consorcio: str | None = None,
        limite: int = 20,
    ) -> dict[str, Any]:
        """Busca horarios de autobús interurbano entre dos núcleos dentro de un consorcio CTAN."""
        consorcios = await self.get_consorcios()

        candidatos = consorcios
        if provincia_o_consorcio:
            q_cons = normalize_text(provincia_o_consorcio)
            filtrados = [
                c
                for c in consorcios
                if q_cons in normalize_text(c["nombre"]) or q_cons == normalize_text(c.get("nombreCorto", ""))
            ]
            if filtrados:
                candidatos = filtrados

        match_info: tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None = None
        for cons in candidatos:
            nucleos = await self.get_nucleos(str(cons["idConsorcio"]))
            n_orig = self._match_nucleo(origen, nucleos)
            n_dest = self._match_nucleo(destino, nucleos)
            if n_orig and n_dest and n_orig["idNucleo"] != n_dest["idNucleo"]:
                match_info = (cons, n_orig, n_dest)
                break

        if not match_info:
            return {
                "cobertura": False,
                "mensaje": (
                    f"No se encontró un consorcio CTAN común para '{origen}' y '{destino}'. "
                    "CTAN cubre trayectos interurbanos dentro de cada área metropolitana; "
                    "para trayectos entre provincias distintas usa tren (Renfe) o autobús de larga distancia."
                ),
                "servicios": [],
            }

        cons, n_orig, n_dest = match_info
        id_cons = str(cons["idConsorcio"])
        cache_key = f"ctan:horarios:{id_cons}:{n_orig['idNucleo']}:{n_dest['idNucleo']}:{limite}"
        cached = self.store.get_cached_response(cache_key)
        if cached is not None:
            logger.info("CACHE HIT %s (%s -> %s)", cache_key, n_orig["nombre"], n_dest["nombre"])
            return cached

        t0 = time.perf_counter()
        async with self._client() as client:
            resp = await client.get(
                f"/Consorcios/{id_cons}/horarios_origen_destino",
                params={"idNucleoOrigen": n_orig["idNucleo"], "idNucleoDestino": n_dest["idNucleo"]},
            )
            resp.raise_for_status()
            data = resp.json()
        logger.info(
            "HTTP GET CTAN /Consorcios/%s/horarios_origen_destino status=%d (%.1f ms) [%s -> %s]",
            id_cons,
            resp.status_code,
            (time.perf_counter() - t0) * 1000,
            n_orig["nombre"],
            n_dest["nombre"],
        )

        if "error" in data:
            return {
                "cobertura": True,
                "consorcio": cons["nombre"],
                "origen": n_orig["nombre"],
                "destino": n_dest["nombre"],
                "mensaje": data["error"],
                "servicios": [],
            }

        bloques = data.get("bloques", [])
        # Los bloques son: [Lineas, parada_1, parada_2, ..., Frecuencia, Observaciones]
        # Las horas en cada fila corresponden a bloques[1 : 1 + len(horas)]
        horarios_raw = data.get("horario", [])
        stop_names = [b.get("nombre", "").strip() for b in bloques[1:]]

        servicios: list[dict[str, Any]] = []
        for h in horarios_raw[:limite]:
            horas: list[str] = h.get("horas", [])
            paradas_horas = [
                (stop_names[i] if i < len(stop_names) else f"Parada {i+1}", hora)
                for i, hora in enumerate(horas)
                if hora and hora != "--"
            ]
            if not paradas_horas:
                continue
            salida_parada, salida_hora = paradas_horas[0]
            llegada_parada, llegada_hora = paradas_horas[-1]
            servicios.append(
                {
                    "linea_codigo": h.get("codigo"),
                    "id_linea": h.get("idlinea"),
                    "origen_parada": salida_parada,
                    "hora_salida": salida_hora,
                    "destino_parada": llegada_parada,
                    "hora_llegada": llegada_hora,
                    "dias": h.get("dias"),
                    "observaciones": h.get("observaciones") or None,
                }
            )

        payload = {
            "cobertura": True,
            "consorcio": cons["nombre"],
            "origen": n_orig["nombre"],
            "destino": n_dest["nombre"],
            "total_servicios": len(horarios_raw),
            "servicios": servicios,
        }
        self.store.set_cached_response(cache_key, "ctan", payload)
        return payload
