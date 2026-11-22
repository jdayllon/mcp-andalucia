"""Cliente asíncrono para OpenSky Network (conectividad aérea reciente en aeropuertos andaluces)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx

from mcp_andalucia.config import settings
from mcp_andalucia.db import DuckDBStore, get_store
from mcp_andalucia.logging_config import get_logger
from mcp_andalucia.utils import normalize_text

logger = get_logger("clients.opensky")

# Mapa de aeropuertos andaluces y principales hubs españoles/europeos
AIRPORTS: dict[str, dict[str, str]] = {
    "LEZL": {"iata": "SVQ", "ciudad": "Sevilla", "nombre": "Aeropuerto de Sevilla"},
    "LEMG": {"iata": "AGP", "ciudad": "Málaga", "nombre": "Aeropuerto de Málaga-Costa del Sol"},
    "LEGR": {"iata": "GRX", "ciudad": "Granada", "nombre": "Aeropuerto Federico García Lorca Granada-Jaén"},
    "LEJR": {"iata": "XRY", "ciudad": "Jerez de la Frontera", "nombre": "Aeropuerto de Jerez"},
    "LEAM": {"iata": "LEI", "ciudad": "Almería", "nombre": "Aeropuerto de Almería"},
    "LEBA": {"iata": "ODB", "ciudad": "Córdoba", "nombre": "Aeropuerto de Córdoba"},
    "LEMD": {"iata": "MAD", "ciudad": "Madrid", "nombre": "Aeropuerto Adolfo Suárez Madrid-Barajas"},
    "LEBL": {"iata": "BCN", "ciudad": "Barcelona", "nombre": "Aeropuerto Josep Tarradellas Barcelona-El Prat"},
    "LEVC": {"iata": "VLC", "ciudad": "Valencia", "nombre": "Aeropuerto de Valencia"},
    "LEBB": {"iata": "BIO", "ciudad": "Bilbao", "nombre": "Aeropuerto de Bilbao"},
    "LEPA": {"iata": "PMI", "ciudad": "Palma de Mallorca", "nombre": "Aeropuerto de Palma de Mallorca"},
    "LEAL": {"iata": "ALC", "ciudad": "Alicante", "nombre": "Aeropuerto de Alicante-Elche"},
    "LEST": {"iata": "SCQ", "ciudad": "Santiago de Compostela", "nombre": "Aeropuerto de Santiago"},
    "EGLL": {"iata": "LHR", "ciudad": "Londres", "nombre": "London Heathrow"},
    "LFPG": {"iata": "CDG", "ciudad": "París", "nombre": "Paris Charles de Gaulle"},
}

ANDALUSIAN_AIRPORTS = {"LEZL", "LEMG", "LEGR", "LEJR", "LEAM", "LEBA"}


def resolve_airport_icao(query: str | None) -> str | None:
    """Resuelve una ciudad, código IATA o código ICAO a un código ICAO de 4 letras."""
    if not query:
        return None
    q_up = query.strip().upper()
    if q_up in AIRPORTS:
        return q_up
    for icao, info in AIRPORTS.items():
        if info["iata"] == q_up:
            return icao
    q_norm = normalize_text(query)
    for icao, info in AIRPORTS.items():
        if q_norm == normalize_text(info["ciudad"]) or q_norm in normalize_text(info["ciudad"]):
            return icao
    if len(q_up) == 4 and q_up.isalpha():
        return q_up
    return None


class OpenSkyClient:
    """Cliente para consultar conectividad aérea reciente mediante OpenSky Network."""

    def __init__(self, store: DuckDBStore | None = None) -> None:
        self.base_url = settings.opensky_base_url.rstrip("/")
        self.store = store or get_store()
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def _get_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if settings.opensky_client_id and settings.opensky_client_secret:
            if not self._token or time.time() >= self._token_expires_at - 30:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        settings.opensky_token_url,
                        data={
                            "grant_type": "client_credentials",
                            "client_id": settings.opensky_client_id,
                            "client_secret": settings.opensky_client_secret,
                        },
                    )
                    if resp.status_code == 200:
                        tok = resp.json()
                        self._token = tok.get("access_token")
                        self._token_expires_at = time.time() + int(tok.get("expires_in", 300))
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def get_recent_connectivity(
        self,
        origen: str | None = None,
        destino: str | None = None,
        horas_atras: int = 36,
    ) -> dict[str, Any]:
        """Consulta vuelos recientes (llegadas/salidas) en OpenSky para verificar rutas activas."""
        orig_icao = resolve_airport_icao(origen)
        dest_icao = resolve_airport_icao(destino)

        if not orig_icao and not dest_icao:
            return {
                "cobertura": False,
                "mensaje": (
                    "No se reconoció ningún aeropuerto para origen o destino. "
                    "Aeropuertos andaluces disponibles: Sevilla (SVQ/LEZL), Málaga (AGP/LEMG), "
                    "Granada (GRX/LEGR), Jerez (XRY/LEJR), Almería (LEI/LEAM), Córdoba (ODB/LEBA)."
                ),
                "vuelos": [],
            }

        # OpenSky genera /flights/arrival y /flights/departure en lote nocturno;
        # consultar ventana de las últimas 36h-48h terminando hace unas horas maximiza resultados.
        end_ts = int(time.time()) - 6 * 3600
        hours = max(6, min(horas_atras, 48))
        begin_ts = end_ts - hours * 3600

        target_icao = dest_icao or orig_icao
        endpoint = "/flights/arrival" if dest_icao else "/flights/departure"

        cache_key = f"opensky:{endpoint}:{orig_icao or '-'}:{dest_icao or '-'}:{hours}"
        cached = self.store.get_cached_response(cache_key)
        if cached is not None:
            logger.info("CACHE HIT %s", cache_key)
            return cached

        t0 = time.perf_counter()
        headers = await self._get_headers()
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=settings.http_timeout_seconds,
                headers=headers,
            ) as client:
                resp = await client.get(
                    endpoint,
                    params={"airport": target_icao, "begin": begin_ts, "end": end_ts},
                )
                if resp.status_code == 404:
                    flights_raw = []
                else:
                    resp.raise_for_status()
                    flights_raw = resp.json() if isinstance(resp.json(), list) else []
            logger.info(
                "HTTP GET OpenSky %s airport=%s status=%d flights=%d (%.1f ms)",
                endpoint,
                target_icao,
                resp.status_code,
                len(flights_raw),
                (time.perf_counter() - t0) * 1000,
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("OpenSky HTTP error %d en %s: %s", exc.response.status_code, endpoint, exc)
            return {
                "cobertura": False,
                "error": f"OpenSky HTTP {exc.response.status_code} (puede deberse a límite de cuota anónima).",
                "vuelos": [],
            }
        except Exception as exc:
            logger.warning("OpenSky exception en %s: %s", endpoint, exc)
            return {
                "cobertura": False,
                "error": f"Error consultando OpenSky: {exc}",
                "vuelos": [],
            }

        vuelos: list[dict[str, Any]] = []
        for f in flights_raw:
            dep = f.get("estDepartureAirport")
            arr = f.get("estArrivalAirport")
            if orig_icao and dest_icao:
                if dep != orig_icao or arr != dest_icao:
                    continue
            first_seen = f.get("firstSeen")
            last_seen = f.get("lastSeen")
            vuelos.append(
                {
                    "callsign": (f.get("callsign") or "").strip(),
                    "origen_icao": dep,
                    "origen_nombre": AIRPORTS.get(dep or "", {}).get("ciudad", dep),
                    "destino_icao": arr,
                    "destino_nombre": AIRPORTS.get(arr or "", {}).get("ciudad", arr),
                    "salida_utc": datetime.fromtimestamp(first_seen, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                    if first_seen
                    else None,
                    "llegada_utc": datetime.fromtimestamp(last_seen, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                    if last_seen
                    else None,
                }
            )

        payload = {
            "cobertura": True,
            "nota": "Datos de tráfico aéreo reciente observado en OpenSky (no incluye tarifas ni horarios futuros).",
            "aeropuerto_consultado": target_icao,
            "total_vuelos_filtrados": len(vuelos),
            "vuelos": vuelos[:25],
        }
        self.store.set_cached_response(cache_key, "opensky", payload, ttl_seconds=1800)
        return payload
