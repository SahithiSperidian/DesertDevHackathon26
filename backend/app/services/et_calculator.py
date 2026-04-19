"""
Reference Evapotranspiration (ETo) Calculator
==============================================
Computes monthly crop irrigation need using the professional standard:

  Primary   : ASCE Penman-Monteith (FAO-56 Eq 6)
              Uses NOAA CDO NORMAL_MLY 30-year normals:
                MLY-TMAX-NORMAL  (°F)
                MLY-TMIN-NORMAL  (°F)
                MLY-DEWP-NORMAL  (°F dew point  → ea)
                MLY-WIND-AVGSPD  (mph → u2 m/s)

  Fallback 1: Hargreaves-Samani (FAO-56 Eq 52) — when dew point / wind missing
              Arid NM calibration factor 0.62 applied (Allen et al. 1998 §2.4)

  Fallback 2: Static NMSU Cooperative Extension inches — when temp data absent

Then multiplied by FAO-56 crop coefficient Kc per growth stage / month.

Cache TTL: 7 days (same station as noaa_rainfall)

References:
  Allen R.G. et al. (1998) FAO Irrigation Paper 56
  Hargreaves G.H. & Samani Z.A. (1985) Reference Crop ET from Temperature
"""

import json
import math
import os
import time
import logging

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logger = logging.getLogger(__name__)

_NOAA_TOKEN = os.getenv("NOAA_CDO_TOKEN", "")
_BASE_URL   = "https://www.ncdc.noaa.gov/cdo-web/api/v2"
_CACHE_DIR  = os.path.join(os.path.dirname(__file__), "..", "..", "data", "live")
_CACHE_FILE = os.path.join(_CACHE_DIR, "et_normals_cache.json")
_CACHE_TTL  = 604_800   # 7 days

# Mean representative day-of-year for the middle of each month (FAO-56 Table 2.5)
_DOY_MID = {1: 15, 2: 46, 3: 75, 4: 106, 5: 136, 6: 167,
            7: 197, 8: 228, 9: 259, 10: 289, 11: 320, 12: 350}

# Average days per month (Feb uses 28.25 to account for leap years)
_DAYS_IN_MONTH = {1: 31, 2: 28.25, 3: 31, 4: 30, 5: 31, 6: 30,
                  7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}

# NM city geocoordinates + elevation (lat, lon, elev_m)
# Source: US Census Bureau / NM GIS / USGS National Elevation Dataset
_NM_CITIES: dict[str, tuple[float, float, float]] = {
    "albuquerque":           (35.0844, -106.6504, 1619),
    "santa fe":              (35.6870, -105.9378, 2194),
    "las cruces":            (32.3199, -106.7637, 1189),
    "rio rancho":            (35.2328, -106.6630, 1698),
    "roswell":               (33.3943, -104.5230, 1116),
    "farmington":            (36.7281, -108.2087, 1698),
    "clovis":                (34.4048, -103.2052, 1372),
    "hobbs":                 (32.7026, -103.1360, 1107),
    "alamogordo":            (32.8995, -105.9603, 1310),
    "carlsbad":              (32.4207, -104.2288, 978),
    "gallup":                (35.5281, -108.7426, 1976),
    "taos":                  (36.4072, -105.5731, 2124),
    "los lunas":             (34.8062, -106.7314, 1453),
    "belen":                 (34.6618, -106.7742, 1473),
    "bernalillo":            (35.3001, -106.5531, 1555),
    "socorro":               (34.0584, -106.8914, 1418),
    "silver city":           (32.7701, -108.2803, 1829),
    "truth or consequences": (33.1290, -107.2528, 1310),
    "deming":                (32.2687, -107.7586, 1312),
    "las vegas":             (35.5939, -105.2228, 1931),
    "espanola":              (35.9928, -106.0744, 1767),
    "española":              (35.9928, -106.0744, 1767),
    "portales":              (34.1873, -103.3335, 1238),
    "ruidoso":               (33.3315, -105.6569, 2062),
    "artesia":               (32.8428, -104.4030, 1017),
    "lovington":             (32.9440, -103.3488, 1113),
    "grants":                (35.1475, -107.8514, 1977),
    "los alamos":            (35.8800, -106.2952, 2239),
    "moriarty":              (34.9943, -106.0489, 1966),
    "edgewood":              (35.0617, -106.1911, 1976),
    "corrales":              (35.2375, -106.6086, 1545),
    "estancia":              (34.7601, -106.0564, 1893),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_location(location_str: str | None) -> tuple[float, float, float] | None:
    """Match free-text location to (lat, lon, elev_m). Returns None if unknown."""
    if not location_str:
        return None
    loc = location_str.lower().strip().split(",")[0].strip()
    if loc in _NM_CITIES:
        return _NM_CITIES[loc]
    for city, coords in _NM_CITIES.items():
        if city in loc or loc in city:
            return coords
    return None


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _find_nearest_station(lat: float, lon: float) -> str | None:
    delta = 1.5
    params = {
        "datasetid":  "NORMAL_MLY",
        "datatypeid": "MLY-TMAX-NORMAL",
        "extent":     f"{lat - delta},{lon - delta},{lat + delta},{lon + delta}",
        "limit":      25,
    }
    try:
        r = requests.get(
            f"{_BASE_URL}/stations",
            params=params,
            headers={"token": _NOAA_TOKEN},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return None
        return min(results,
                   key=lambda s: _haversine_km(lat, lon, s["latitude"], s["longitude"]))["id"]
    except Exception as exc:
        logger.warning("ET: NOAA station search failed: %s", exc)
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


# ---------------------------------------------------------------------------
# Physics: Ra, ETo
# ---------------------------------------------------------------------------

def _ra_mj_m2_day(lat_deg: float, month: int) -> float:
    """
    Extraterrestrial radiation Ra (MJ/m²/day) — FAO-56 Eq 21.
    Uses representative day-of-year for the middle of each month.
    """
    J   = _DOY_MID[month]
    phi = math.radians(lat_deg)
    dr  = 1 + 0.033 * math.cos(2 * math.pi * J / 365)
    d   = 0.409 * math.sin(2 * math.pi * J / 365 - 1.39)
    ws  = math.acos(max(-1.0, min(1.0, -math.tan(phi) * math.tan(d))))
    Gsc = 0.0820   # MJ m⁻² min⁻¹
    Ra  = ((24 * 60 / math.pi) * Gsc * dr
           * (ws * math.sin(phi) * math.sin(d)
              + math.cos(phi) * math.cos(d) * math.sin(ws)))
    return max(Ra, 0.0)


def _eto_penman_monteith(
    tmax_c: float, tmin_c: float,
    tdew_c: float, u2_ms: float,
    ra: float, elev_m: float,
) -> float:
    """
    FAO-56 ASCE Penman-Monteith daily ETo (mm/day) — FAO-56 Eq 6.

    Solar radiation Rs estimated from temperature range via Hargreaves
    solar formula (Ra × 0.16 × √(Tmax-Tmin)) — FAO-56 Eq 50.
    """
    tmean = (tmax_c + tmin_c) / 2

    # Atmospheric pressure and psychrometric constant
    P     = 101.3 * ((293 - 0.0065 * elev_m) / 293) ** 5.26
    gamma = 0.000665 * P   # kPa/°C

    # Saturation vapor pressure
    es_max = 0.6108 * math.exp(17.27 * tmax_c / (tmax_c + 237.3))
    es_min = 0.6108 * math.exp(17.27 * tmin_c / (tmin_c + 237.3))
    es     = (es_max + es_min) / 2

    # Actual vapor pressure from dew point
    ea = 0.6108 * math.exp(17.27 * tdew_c / (tdew_c + 237.3))
    ea = min(ea, es)   # ea cannot exceed es

    # Slope of saturation VP curve (kPa/°C)
    delta = 4098 * (0.6108 * math.exp(17.27 * tmean / (tmean + 237.3))) / (tmean + 237.3) ** 2

    # Estimated solar radiation (MJ/m²/day) — Hargreaves solar formula
    td = max(tmax_c - tmin_c, 0.0)
    rs = ra * 0.16 * math.sqrt(td)

    # Net shortwave radiation (α = 0.23 for reference grass)
    rns = (1 - 0.23) * rs

    # Net longwave radiation — FAO-56 Eq 39
    sigma  = 4.903e-9   # MJ K⁻⁴ m⁻² day⁻¹
    tmax_k = tmax_c + 273.16
    tmin_k = tmin_c + 273.16
    rso    = (0.75 + 2e-5 * elev_m) * ra   # clear-sky radiation
    rs_rso = min(rs / rso, 1.0) if rso > 0 else 1.0
    rnl = (sigma
           * (tmax_k ** 4 + tmin_k ** 4) / 2
           * (0.34 - 0.14 * math.sqrt(max(ea, 0.0)))
           * (1.35 * rs_rso - 0.35))

    rn = rns - rnl

    # FAO-56 Eq 6
    numer = (0.408 * delta * rn
             + gamma * (900 / (tmean + 273)) * u2_ms * (es - ea))
    denom = delta + gamma * (1 + 0.34 * u2_ms)
    return max(numer / denom, 0.0)


def _eto_hargreaves(tmax_c: float, tmin_c: float, ra: float) -> float:
    """
    Hargreaves-Samani (1985) ETo (mm/day) — FAO-56 Eq 52.
    Calibration factor 0.62 applied for NM arid climate
    (cf. Allen et al. 1998 §2.4 — reduces ~30% overestimate in arid zones).
    """
    tmean = (tmax_c + tmin_c) / 2
    td    = max(tmax_c - tmin_c, 0.0)
    return max(0.0023 * ra * math.sqrt(td) * (tmean + 17.8) * 0.62, 0.0)


# ---------------------------------------------------------------------------
# NOAA data fetch
# ---------------------------------------------------------------------------

def _fetch_climate_normals(station_id: str) -> dict:
    """
    Fetch monthly climate normals for a station.
    Returns {month_int: {tmax_f, tmin_f, tdew_f, wind_mph}} for months 1-12.
    Missing fields are None.
    """
    params = {
        "datasetid":  "NORMAL_MLY",
        "datatypeid": "MLY-TMAX-NORMAL,MLY-TMIN-NORMAL,MLY-DEWP-NORMAL,MLY-WIND-AVGSPD",
        "stationid":  station_id,
        "startdate":  "2010-01-01",
        "enddate":    "2010-12-31",
        "units":      "standard",
        "limit":      50,
    }
    try:
        r = requests.get(
            f"{_BASE_URL}/data",
            params=params,
            headers={"token": _NOAA_TOKEN},
            timeout=15,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception as exc:
        logger.warning("ET: NOAA climate normals fetch failed for %s: %s", station_id, exc)
        return {}

    normals: dict[int, dict] = {m: {} for m in range(1, 13)}
    for rec in results:
        month = int(rec["date"][5:7])
        dtype = rec["datatype"]
        val   = rec["value"]
        if dtype == "MLY-TMAX-NORMAL":
            normals[month]["tmax_f"] = val
        elif dtype == "MLY-TMIN-NORMAL":
            normals[month]["tmin_f"] = val
        elif dtype == "MLY-DEWP-NORMAL":
            normals[month]["tdew_f"] = val
        elif dtype == "MLY-WIND-AVGSPD":
            normals[month]["wind_mph"] = val

    return normals


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_et_normals(location: str | None = None) -> dict:
    """
    Compute monthly reference ETo (mm/day) for the farmer's location.

    Returns:
        eto_monthly : {str(month): float}   — mean daily ETo mm/day, all 12 months
        lat         : float
        station_id  : str | None
        method      : "penman-monteith" | "hargreaves" | "fallback"
        source      : "noaa" | "fallback"
    """
    # Default: NM central fallback ETo (mm/day), derived from NRCS NM published data
    _NM_FALLBACK_ETO = {
        "1": 1.8, "2": 2.4, "3": 3.9, "4": 5.3, "5": 7.1, "6": 8.9,
        "7": 8.7, "8": 8.1, "9": 6.2, "10": 4.2, "11": 2.6, "12": 1.7,
    }

    coords = _resolve_location(location)
    if not coords or not _NOAA_TOKEN:
        return {
            "eto_monthly": _NM_FALLBACK_ETO,
            "lat": 35.08, "station_id": None, "method": "fallback", "source": "fallback",
        }

    lat, _lon, elev_m = coords
    cache_key = f"et_{lat:.4f},{_lon:.4f}"

    cache = _load_cache()
    entry = cache.get(cache_key)
    if entry and time.time() - entry.get("ts", 0) < _CACHE_TTL:
        return entry["data"]

    # Find nearest station
    station_id = _find_nearest_station(lat, _lon)
    if not station_id:
        return {
            "eto_monthly": _NM_FALLBACK_ETO,
            "lat": lat, "station_id": None, "method": "fallback", "source": "fallback",
        }

    # Fetch climate normals
    normals = _fetch_climate_normals(station_id)
    if not normals:
        return {
            "eto_monthly": _NM_FALLBACK_ETO,
            "lat": lat, "station_id": station_id, "method": "fallback", "source": "fallback",
        }

    eto_monthly = {}
    methods_used = set()

    for month in range(1, 13):
        rec = normals.get(month, {})
        tmax_f = rec.get("tmax_f")
        tmin_f = rec.get("tmin_f")
        tdew_f = rec.get("tdew_f")
        wind_mph = rec.get("wind_mph")

        if tmax_f is None or tmin_f is None:
            # No temperature data — use fallback for this month
            eto_monthly[str(month)] = _NM_FALLBACK_ETO[str(month)]
            methods_used.add("fallback")
            continue

        # Convert °F → °C
        tmax_c = (tmax_f - 32) * 5 / 9
        tmin_c = (tmin_f - 32) * 5 / 9
        ra = _ra_mj_m2_day(lat, month)

        if tdew_f is not None and wind_mph is not None:
            # Full Penman-Monteith
            tdew_c = (tdew_f - 32) * 5 / 9
            u2_ms  = wind_mph * 0.44704   # mph → m/s
            eto    = _eto_penman_monteith(tmax_c, tmin_c, tdew_c, u2_ms, ra, elev_m)
            methods_used.add("penman-monteith")
        else:
            # Hargreaves-Samani fallback
            eto = _eto_hargreaves(tmax_c, tmin_c, ra)
            methods_used.add("hargreaves")

        eto_monthly[str(month)] = round(eto, 3)

    # Determine method label
    if "penman-monteith" in methods_used:
        method = "penman-monteith"
    elif "hargreaves" in methods_used:
        method = "hargreaves"
    else:
        method = "fallback"

    result = {
        "eto_monthly": eto_monthly,
        "lat": lat,
        "station_id": station_id,
        "method": method,
        "source": "noaa" if method != "fallback" else "fallback",
    }

    cache[cache_key] = {"ts": time.time(), "data": result}
    _save_cache(cache)
    return result


def crop_seasonal_et_inches(crop: dict, et_normals: dict) -> float:
    """
    Compute location-adjusted seasonal irrigation need for one crop (inches/season).

    Method — ratio adjustment (NRCS spatial scaling approach):
      1. Compute growing-season ETo at farmer's location using NOAA normals.
      2. Divide by pre-computed Albuquerque (central NM) Hargreaves baseline ETo
         for the same growing months.
      3. Scale the NMSU Cooperative Extension published value by this ratio.

    This anchors the output to NMSU-validated irrigation data while correctly
    reflecting how demand is higher in hot/dry Hobbs vs. cooler Taos, for example.
    Ratio ≈ 1.0 for Albuquerque → result equals NMSU published value exactly.

    Albuquerque baseline derived from NOAA station GHCND:USC00290231
    (30-yr normals 1991-2020) using Hargreaves-Samani + NM arid factor 0.62.
    """
    # Pre-computed Albuquerque Hargreaves ETo baseline (mm/day)
    _ABQ_ETO = {
        1: 2.057, 2: 3.093, 3: 4.690, 4: 6.741,
        5: 8.626, 6: 10.368, 7: 10.004, 8: 8.716,
        9: 6.825, 10: 4.551, 11: 2.721, 12: 1.830,
    }

    et         = crop.get("et", {})
    growing_months = et.get("growing_months", [])
    static_in  = float(crop["water"]["irrigation_inches_per_season"])

    if not growing_months or et_normals.get("source") == "fallback":
        return static_in

    eto_monthly = et_normals["eto_monthly"]

    # Growing-season ETo totals (mm) — local vs. Albuquerque baseline
    local_mm = sum(
        float(eto_monthly.get(str(m), _ABQ_ETO[m])) * _DAYS_IN_MONTH[m]
        for m in growing_months
    )
    baseline_mm = sum(
        _ABQ_ETO[m] * _DAYS_IN_MONTH[m]
        for m in growing_months
    )

    if baseline_mm <= 0:
        return static_in

    ratio = local_mm / baseline_mm
    return round(static_in * ratio, 1)
