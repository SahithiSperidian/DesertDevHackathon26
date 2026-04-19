"""
USDA NASS Quick Stats economics service.
Fetches real NM price-received and yield data to replace dummy crop economics.
Cache TTL: 24 hours (data changes annually at most).
"""
import os
import json
import time
import logging
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logger = logging.getLogger(__name__)

_NASS_KEY  = os.getenv("NASS_API_KEY", "")
_BASE_URL  = "https://quickstats.nass.usda.gov/api/api_GET/"
_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "live")
_CACHE_FILE = os.path.join(_CACHE_DIR, "nass_cache.json")
_CACHE_TTL  = 86400  # 24 hours


# ---------------------------------------------------------------------------
# NASS query map: how to get price + yield for each crop
# Each entry: (commodity_desc, price_unit, yield_commodity, yield_unit, state)
#   price_unit  — NASS unit_desc we expect for price
#   yield_unit  — NASS unit_desc we expect for yield
#   state       — "NM" for NM-specific, "US" for national fallback
# ---------------------------------------------------------------------------
_CROP_QUERIES = {
    "chile_pepper": {
        "commodity": "PEPPERS",
        "price_state": "NM",
        "yield_state": "NM",
        "price_unit": "$ / CWT",
        "yield_unit": "CWT / ACRE",
        "investment_per_acre": 3000,
    },
    "onion": {
        "commodity": "ONIONS",
        "price_state": "NM",
        "yield_state": "NM",
        "price_unit": "$ / CWT",
        "yield_unit": "CWT / ACRE",
        "investment_per_acre": 5000,
    },
    "corn": {
        "commodity": "CORN",
        "price_state": "NM",
        "yield_state": "NM",
        "price_unit": "$ / BU",
        "yield_unit": "BU / ACRE",
        "investment_per_acre": 700,
    },
    "alfalfa": {
        "commodity": "HAY",
        "price_state": "NM",
        "yield_state": None,
        "price_unit": "$ / TON",
        "yield_unit": "TONS / ACRE",
        "yield_override": 6.0,      # NMSU Extension irrigated alfalfa budget
        "investment_per_acre": 900,
    },
    "wheat": {
        "commodity": "WHEAT",
        "price_state": "NM",
        "yield_state": "NM",
        "price_unit": "$ / BU",
        "yield_unit": "BU / ACRE",
        "investment_per_acre": 250,
    },
    "sorghum": {
        "commodity": "SORGHUM",
        "price_state": "NM",
        "yield_state": "NM",
        "price_unit": "$ / CWT",
        "yield_unit": "BU / ACRE",
        # Sorghum: yield in BU/acre, price in $/CWT — 1 BU sorghum = 56 lbs = 0.56 CWT
        "unit_conversion": "bu_to_cwt_sorghum",
        "investment_per_acre": 350,
    },
    "pecans": {
        "commodity": "PECANS",
        "price_state": "NM",
        "yield_state": "NM",
        "price_unit": "$ / LB",
        "yield_unit": "LB / ACRE",
        "investment_per_acre": 1200,
    },
    "cotton": {
        "commodity": "COTTON",
        "price_state": "NM",
        "yield_state": "NM",
        "price_unit": "$ / TON",
        "yield_unit": "LB / ACRE",
        # Cotton yield in LB/acre, price in $/TON — convert: revenue = (lbs/2000)*price
        "unit_conversion": "lb_to_ton",
        "investment_per_acre": 600,
    },
}


def _fetch_nass(commodity: str, statisticcat: str, state: str, unit: str) -> tuple[float, int] | tuple[None, None]:
    """Return (value, year) for the most-recent NASS record, or (None, None)."""
    if not _NASS_KEY:
        return None, None
    try:
        r = requests.get(_BASE_URL, params={
            "key": _NASS_KEY,
            "source_desc": "SURVEY",
            "commodity_desc": commodity,
            "statisticcat_desc": statisticcat,
            "state_alpha": state,
            "year__GE": "2018",
            "format": "json",
        }, timeout=15)
        items = r.json().get("data", [])
        if not items:
            return None, None
        # Take the most recent year, prefer the one matching expected unit
        matching = [i for i in items if unit.lower() in i.get("unit_desc", "").lower()]
        pool = matching or items
        latest = max(pool, key=lambda x: int(x.get("year", 0)))
        raw = latest.get("Value", "").strip().replace(",", "")
        if not raw or raw == "(NA)" or raw == "(D)":
            return None, None
        return float(raw), int(latest.get("year", 0))
    except Exception as e:
        logger.warning("NASS fetch failed for %s %s: %s", commodity, statisticcat, e)
        return None


def _build_economics() -> dict:
    """
    Fetch NASS price + yield for each crop and compute per-acre economics.
    Returns a dict keyed by crop_id with real investment/revenue/profit.
    """
    result = {}
    for crop_id, cfg in _CROP_QUERIES.items():
        commodity   = cfg["commodity"]
        price_state = cfg.get("price_state", "NM")
        yield_state = cfg.get("yield_state")
        price_unit  = cfg.get("price_unit", "")
        yield_unit  = cfg.get("yield_unit", "")
        investment  = cfg["investment_per_acre"]

        # --- Fetch price ---
        price, price_year = _fetch_nass(commodity, "PRICE RECEIVED", price_state, price_unit)

        # --- Fetch or override yield ---
        if "yield_override" in cfg:
            yield_val = cfg["yield_override"]
            yield_year = price_year
            yield_source = "nmsu_extension"
        elif yield_state:
            yield_val, yield_year = _fetch_nass(commodity, "YIELD", yield_state, yield_unit)
            yield_source = "nass_" + yield_state.lower()
        else:
            yield_val = None
            yield_year = None
            yield_source = None

        if price is None or yield_val is None:
            result[crop_id] = None   # signal: use fallback
            logger.info("NASS: no data for %s (price=%s yield=%s)", crop_id, price, yield_val)
            continue

        # Unit conversions — revenue always expressed per acre in USD
        unit_conv = cfg.get("unit_conversion")
        if unit_conv == "lb_to_ton":
            # cotton: yield in LB/acre, price in $/TON
            yield_val = yield_val / 2000.0
        elif unit_conv == "bu_to_cwt_sorghum":
            # sorghum: yield in BU/acre (56 lbs/bu), price in $/CWT (100 lbs)
            yield_val = yield_val * 56.0 / 100.0

        revenue = round(price * yield_val)
        profit  = revenue - investment

        result[crop_id] = {
            "investment_per_acre_usd": investment,
            "revenue_per_acre_usd": revenue,
            "profit_per_acre_usd": profit,
            "price_per_unit": price,
            "price_unit": price_unit,
            "yield_per_acre": yield_val,
            "yield_unit": yield_unit,
            "yield_source": yield_source,
            "price_source": "nass_" + price_state.lower(),
            "data_year": price_year or yield_year,
        }
        logger.info("NASS: %s → price=%.2f %s  yield=%.1f %s  revenue=$%d",
                    crop_id, price, price_unit, yield_val, yield_unit, revenue)

    return result


def get_nass_economics(force_refresh: bool = False) -> dict:
    """
    Return cached NASS economics dict (refreshed if > 24h old).
    Falls back to an empty dict on any failure — crop engine then uses JSON fallback values.
    """
    os.makedirs(_CACHE_DIR, exist_ok=True)

    # Load existing cache
    if not force_refresh and os.path.exists(_CACHE_FILE):
        try:
            with open(_CACHE_FILE, encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - cached.get("_fetched_at", 0) < _CACHE_TTL:
                return cached.get("economics", {})
        except Exception:
            pass

    # Fetch fresh data
    economics = _build_economics()

    try:
        payload = {"_fetched_at": time.time(), "economics": economics}
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        logger.warning("NASS: could not write cache: %s", e)

    return economics
