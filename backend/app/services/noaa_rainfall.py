"""
NOAA Climate Data Online — Seasonal Precipitation Service
==========================================================
Uses the NOAA CDO Normals Monthly dataset (1991-2020 30-year averages) to
return the May–October seasonal precipitation in inches for the weather
station nearest to the farmer's city.

API: https://www.ncdc.noaa.gov/cdo-web/api/v2/
Token: NOAA_CDO_TOKEN env var  (free, instant at ncdc.noaa.gov/cdo-web/token)
Cache TTL: 7 days (normals don't change)
Fallback: 7.5 inches (central NM historical average)
"""

import os
import json
import math
import time
import logging

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logger = logging.getLogger(__name__)

_NOAA_TOKEN = os.getenv("NOAA_CDO_TOKEN", "")
_BASE_URL   = "https://www.ncdc.noaa.gov/cdo-web/api/v2"
_CACHE_DIR  = os.path.join(os.path.dirname(__file__), "..", "..", "data", "live")
_CACHE_FILE = os.path.join(_CACHE_DIR, "noaa_rainfall_cache.json")
_CACHE_TTL  = 604_800   # 7 days
_FALLBACK   = 7.5       # central NM May-Oct average (inches)

# NM city geocoordinates — source: US Census Bureau / NM GIS
_NM_CITIES: dict[str, tuple[float, float]] = {
    "albuquerque":           (35.0844, -106.6504),
    "santa fe":              (35.6870, -105.9378),
    "las cruces":            (32.3199, -106.7637),
    "rio rancho":            (35.2328, -106.6630),
    "roswell":               (33.3943, -104.5230),
    "farmington":            (36.7281, -108.2087),
    "clovis":                (34.4048, -103.2052),
    "hobbs":                 (32.7026, -103.1360),
    "alamogordo":            (32.8995, -105.9603),
    "carlsbad":              (32.4207, -104.2288),
    "gallup":                (35.5281, -108.7426),
    "taos":                  (36.4072, -105.5731),
    "los lunas":             (34.8062, -106.7314),
    "belen":                 (34.6618, -106.7742),
    "bernalillo":            (35.3001, -106.5531),
    "socorro":               (34.0584, -106.8914),
    "silver city":           (32.7701, -108.2803),
    "truth or consequences": (33.1290, -107.2528),
    "deming":                (32.2687, -107.7586),
    "las vegas":             (35.5939, -105.2228),
    "espanola":              (35.9928, -106.0744),
    "española":              (35.9928, -106.0744),
    "portales":              (34.1873, -103.3335),
    "ruidoso":               (33.3315, -105.6569),
    "artesia":               (32.8428, -104.4030),
    "lovington":             (32.9440, -103.3488),
    "grants":                (35.1475, -107.8514),
    "los alamos":            (35.8800, -106.2952),
    "moriarty":              (34.9943, -106.0489),
    "edgewood":              (35.0617, -106.1911),
    "corrales":              (35.2375, -106.6086),
    "estancia":              (34.7601, -106.0564),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _resolve_coords(location_str: str | None) -> tuple[float, float] | None:
    """Match a free-text location string to a known NM city lat/lon."""
    if not location_str:
        return None
    loc = location_str.lower().strip().split(",")[0].strip()
    if loc in _NM_CITIES:
        return _NM_CITIES[loc]
    for city, coords in _NM_CITIES.items():
        if city in loc or loc in city:
            return coords
    return None


def _load_cache() -> dict:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(data: dict) -> None:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _find_nearest_station(lat: float, lon: float) -> str | None:
    """Return the NOAA CDO station ID with NORMAL_MLY data nearest to lat/lon."""
    delta = 1.5  # search ±1.5 degrees (~165 km)
    params = {
        "datasetid":  "NORMAL_MLY",
        "datatypeid": "MLY-PRCP-NORMAL",
        "extent":     f"{lat - delta},{lon - delta},{lat + delta},{lon + delta}",
        "limit":      25,
    }
    headers = {"token": _NOAA_TOKEN}
    try:
        r = requests.get(
            f"{_BASE_URL}/stations",
            params=params,
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return None
        best = min(
            results,
            key=lambda s: _haversine_km(lat, lon, s["latitude"], s["longitude"]),
        )
        logger.info(
            "NOAA nearest station: %s (%s) — %.1f km away",
            best["id"], best.get("name", ""), 
            _haversine_km(lat, lon, best["latitude"], best["longitude"]),
        )
        return best["id"]
    except Exception as exc:
        logger.warning("NOAA station search failed: %s", exc)
        return None


def _fetch_seasonal_inches(station_id: str) -> float | None:
    """
    Fetch MLY-PRCP-NORMAL for all 12 months and sum May–Oct (months 5–10).
    NOAA stores 30-yr normals using 2010 as a placeholder year.
    Units: standard → values are in hundredths of inches.
    """
    params = {
        "datasetid":  "NORMAL_MLY",
        "datatypeid": "MLY-PRCP-NORMAL",
        "stationid":  station_id,
        "startdate":  "2010-01-01",
        "enddate":    "2010-12-31",
        "units":      "standard",
        "limit":      12,
    }
    headers = {"token": _NOAA_TOKEN}
    try:
        r = requests.get(
            f"{_BASE_URL}/data",
            params=params,
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        seasonal = 0.0
        for rec in results:
            month = int(rec["date"][5:7])
            if 5 <= month <= 10:
                # units=standard returns inches directly
                seasonal += rec["value"]
        return round(seasonal, 2) if seasonal > 0 else None
    except Exception as exc:
        logger.warning("NOAA data fetch failed for %s: %s", station_id, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_seasonal_rainfall(location_str: str | None = None) -> dict:
    """
    Returns seasonal (May–Oct) precipitation inches for the farmer's location.

    Return dict:
        inches      — float, precipitation inches
        city        — display city name
        station_id  — NOAA station ID used (or None)
        station_name — human-readable station name (or None)
        source      — "noaa" | "fallback"
    """
    city_display = location_str.split(",")[0].strip().title() if location_str else "New Mexico"

    if not _NOAA_TOKEN:
        logger.warning("NOAA_CDO_TOKEN not set — using fallback rainfall")
        return {
            "inches": _FALLBACK,
            "city": city_display,
            "station_id": None,
            "station_name": None,
            "source": "fallback",
        }

    coords = _resolve_coords(location_str)
    if not coords:
        return {
            "inches": _FALLBACK,
            "city": city_display,
            "station_id": None,
            "station_name": None,
            "source": "fallback",
        }

    lat, lon = coords
    cache_key = f"{lat:.4f},{lon:.4f}"

    # Return cached result if fresh
    cache = _load_cache()
    entry = cache.get(cache_key)
    if entry and time.time() - entry.get("ts", 0) < _CACHE_TTL:
        logger.debug("NOAA rainfall: cache hit for %s", cache_key)
        return {**entry["data"], "city": city_display}

    # Live fetch
    station_id = _find_nearest_station(lat, lon)
    if not station_id:
        return {
            "inches": _FALLBACK,
            "city": city_display,
            "station_id": None,
            "station_name": None,
            "source": "fallback",
        }

    inches = _fetch_seasonal_inches(station_id)
    if not inches:
        return {
            "inches": _FALLBACK,
            "city": city_display,
            "station_id": None,
            "station_name": None,
            "source": "fallback",
        }

    result = {
        "inches": inches,
        "city": city_display,
        "station_id": station_id,
        "station_name": station_id,   # plain ID; station name not returned by /data
        "source": "noaa",
    }

    cache[cache_key] = {"ts": time.time(), "data": result}
    _save_cache(cache)

    return result
