"""Cliente asíncrono para la API OpenRTA de la Junta de Andalucía."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx

from mcp_andalucia.config import settings
from mcp_andalucia.db import DuckDBStore, get_store
from mcp_andalucia.logging_config import get_logger

logger = get_logger("clients.openrta")
from mcp_andalucia.utils import (
    clean_record,
    is_empty_rta_value,
    normalize_text,
    resolve_from_catalog,
    utm_to_latlon,
)

CATALOG_ENDPOINTS: dict[str, str] = {
    "provinces": "/option-values/provinces",
    "municipalities": "/option-values/municipalities",
    "objects_type": "/option-values/objects_type",
    "categories": "/option-values/categories",
    "groups": "/option-values/groups",
    "modalities": "/option-values/modalities",
}


class OpenRTAClient:
    """Cliente para el Registro de Turismo de Andalucía (OpenRTA)."""

    def __init__(self, store: DuckDBStore | None = None) -> None:
        self.base_url = settings.openrta_base_url.rstrip("/")
        self.store = store or get_store()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=settings.http_timeout_seconds,
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )

    async def get_catalog(self, field: str, force_refresh: bool = False) -> list[dict[str, Any]]:
        """Obtiene los valores válidos de un filtro, usando caché en DuckDB + NDJSON."""
        if field not in CATALOG_ENDPOINTS:
            raise ValueError(f"Catálogo no soportado: {field}. Válidos: {list(CATALOG_ENDPOINTS)}")

        ns = f"openrta:{field}"
        if not force_refresh and self.store.is_fresh(f"catalog:{ns}", settings.catalog_ttl_seconds):
            cached = self.store.get_catalog(ns)
            if cached:
                return cached

        async with self._client() as client:
            resp = await client.get(CATALOG_ENDPOINTS[field])
            resp.raise_for_status()
            data = resp.json()

        values_raw: list[str] = data.get("result", [])
        stats_map: dict[str, int] = {
            s["key"]: int(s["doc_count"]) for s in data.get("stats", []) if "key" in s and "doc_count" in s
        }

        items: list[dict[str, Any]] = []
        for val in values_raw:
            if not val or val == "-":
                continue
            items.append(
                {
                    "value": val,
                    "norm_value": normalize_text(val),
                    "meta": {"doc_count": stats_map.get(val)} if val in stats_map else None,
                }
            )

        self.store.save_catalog(ns, items)
        return items

    async def resolve_filter(self, field: str, user_value: str | None) -> str:
        """Resuelve un valor de filtro proporcionado por el usuario al valor exacto de OpenRTA."""
        if not user_value or user_value.strip() in ("", "-"):
            return "-"
        catalog = await self.get_catalog(field)
        values = [item["value"] for item in catalog]
        matched = resolve_from_catalog(user_value, values)
        return matched if matched else user_value

    async def get_dataset_status(self) -> dict[str, Any]:
        """Devuelve el número total de registros y la fecha de última actualización."""
        cache_key = "openrta:status"
        cached = self.store.get_cached_response(cache_key)
        if cached is not None:
            logger.debug("CACHE HIT %s", cache_key)
            return cached

        t0 = time.perf_counter()
        async with self._client() as client:
            count_resp = await client.get("/count")
            count_resp.raise_for_status()
            upd_resp = await client.get("/search/lastUpdateData")
            upd_resp.raise_for_status()
        logger.info("HTTP GET OpenRTA /count + /lastUpdateData (%.1f ms)", (time.perf_counter() - t0) * 1000)

        count_data = count_resp.json()
        upd_data = upd_resp.json()
        total = count_data.get("count", {}).get("result")
        result = {
            "total_registros": total,
            "ultima_actualizacion": upd_data.get("date"),
        }
        self.store.set_cached_response(cache_key, "openrta", result, ttl_seconds=300)
        return result

    async def search(
        self,
        *,
        object_type: str | None = None,
        province: str | None = None,
        municipality: str | None = None,
        category: str | None = None,
        group: str | None = None,
        modality: str | None = None,
        order_by: str = "id",
        mode: str = "ASC",
        size: int = 10,
    ) -> dict[str, Any]:
        """Ejecuta una búsqueda en GET /api/v0/openrta/search resolviendo filtros."""
        resolved_type = await self.resolve_filter("objects_type", object_type)
        resolved_prov = await self.resolve_filter("provinces", province)
        resolved_muni = await self.resolve_filter("municipalities", municipality)
        resolved_cat = await self.resolve_filter("categories", category)
        resolved_grp = await self.resolve_filter("groups", group)
        resolved_mod = await self.resolve_filter("modalities", modality)

        params = {
            "id": "-",
            "object_type": resolved_type,
            "category": resolved_cat,
            "group": resolved_grp,
            "modality": resolved_mod,
            "province": resolved_prov,
            "municipality": resolved_muni,
            "order_by": order_by,
            "mode": mode if mode in ("ASC", "DESC") else "ASC",
            "format": "json",
            "size": max(1, min(size, 100)),
        }

        raw_key = json.dumps(params, sort_keys=True, ensure_ascii=False)
        cache_key = f"openrta:search:{hashlib.sha256(raw_key.encode()).hexdigest()[:16]}"
        cached = self.store.get_cached_response(cache_key)
        if cached is not None:
            logger.info("CACHE HIT %s (type=%s, prov=%s, muni=%s)", cache_key, resolved_type, resolved_prov, resolved_muni)
            return cached

        t0 = time.perf_counter()
        async with self._client() as client:
            resp = await client.get("/search", params=params)
            resp.raise_for_status()
            data = resp.json()
        logger.info(
            "HTTP GET OpenRTA /search status=%d hits=%d (%.1f ms) [type=%s, prov=%s, muni=%s]",
            resp.status_code,
            len(data.get("results", [])),
            (time.perf_counter() - t0) * 1000,
            resolved_type,
            resolved_prov,
            resolved_muni,
        )

        results = [self._format_record(r) for r in data.get("results", [])]
        payload = {
            "total_hits": data.get("total_hits", len(results)),
            "hits": len(results),
            "filtros_aplicados": {
                k: v for k, v in params.items() if k not in ("id", "format", "order_by", "mode", "size") and v != "-"
            },
            "results": results,
        }
        self.store.set_cached_response(cache_key, "openrta", payload)
        return payload

    async def get_by_id(self, bid: int) -> dict[str, Any]:
        """Obtiene el detalle completo de un registro por su ID."""
        cache_key = f"openrta:bid:{bid}"
        cached = self.store.get_cached_response(cache_key)
        if cached is not None:
            logger.info("CACHE HIT %s", cache_key)
            return cached

        t0 = time.perf_counter()
        async with self._client() as client:
            resp = await client.get(f"/{bid}")
            resp.raise_for_status()
            data = resp.json()
        logger.info("HTTP GET OpenRTA /%d status=%d (%.1f ms)", bid, resp.status_code, (time.perf_counter() - t0) * 1000)

        if isinstance(data, dict) and "results" in data and isinstance(data["results"], list):
            record = data["results"][0] if data["results"] else {}
        elif isinstance(data, dict) and "result" in data and isinstance(data["result"], dict):
            record = data["result"]
        elif isinstance(data, dict):
            record = data
        else:
            record = {}

        formatted = self._format_record(record, detailed=True)
        self.store.set_cached_response(cache_key, "openrta", formatted)
        return formatted

    def _format_record(self, raw: dict[str, Any], detailed: bool = False) -> dict[str, Any]:
        """Limpia un registro OpenRTA y convierte coordenadas UTM a WGS84."""
        cleaned = clean_record(raw)
        latlon = utm_to_latlon(cleaned.get("coord_x"), cleaned.get("coord_y"), cleaned.get("srid"))

        # Contacto unificado (guías usan guide_email / guide_mobile)
        email = cleaned.get("email") or cleaned.get("guide_email")
        phone = cleaned.get("phone") or cleaned.get("mobile") or cleaned.get("guide_mobile") or cleaned.get("guide_phone")

        if detailed:
            if latlon:
                cleaned["lat"] = latlon[0]
                cleaned["lon"] = latlon[1]
            if email and "email" not in cleaned:
                cleaned["email"] = email
            if phone and "phone" not in cleaned:
                cleaned["phone"] = phone
            return cleaned

        summary: dict[str, Any] = {
            "id": cleaned.get("id"),
            "nombre": cleaned.get("name"),
            "tipo": cleaned.get("objects_type_id") or cleaned.get("object_type"),
            "provincia": cleaned.get("provinces") or cleaned.get("guide_province"),
            "municipio": cleaned.get("municipalities") or cleaned.get("guide_municipality"),
        }
        for src, dst in (
            ("categories", "categoria"),
            ("group", "grupo"),
            ("modalities", "modalidad"),
            ("establishment_address", "direccion"),
            ("postal_code", "cp"),
            ("tot_gen_places", "plazas"),
            ("languages", "idiomas"),
            ("active_tourism_activities", "actividades"),
            ("registration_code", "registro_rta"),
            ("url", "web"),
        ):
            val = cleaned.get(src)
            if not is_empty_rta_value(val):
                summary[dst] = val

        if not is_empty_rta_value(email):
            summary["email"] = email
        if not is_empty_rta_value(phone):
            summary["telefono"] = phone
        if latlon:
            summary["lat"] = latlon[0]
            summary["lon"] = latlon[1]

        return {k: v for k, v in summary.items() if v is not None}
