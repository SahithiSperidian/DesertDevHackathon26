"""
NASS Monthly Price Alert Service
=================================
Fetches monthly price-received series for NM crops from USDA NASS Quick Stats,
computes a rolling 5-year average per calendar month, then compares the most
recent available month to that average to generate actionable price alerts.

Cached for 24 hours (same TTL as nass_economics.py).
"""

import os
import json
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logger = logging.getLogger(__name__)

_NASS_KEY   = os.getenv("NASS_API_KEY", "")
_BASE_URL   = "https://quickstats.nass.usda.gov/api/api_GET/"
_CACHE_DIR  = os.path.join(os.path.dirname(__file__), "..", "..", "data", "live")
_CACHE_FILE = os.path.join(_CACHE_DIR, "nass_alerts_cache.json")
_CACHE_TTL  = 86400   # 24 hours

# Month abbreviations NASS uses in reference_period_desc
_MONTH_ABBR = ["JAN","FEB","MAR","APR","MAY","JUN",
               "JUL","AUG","SEP","OCT","NOV","DEC"]

# Crops to monitor — (display_name, commodity_desc, price_unit, state)
_ALERT_CROPS = [
    {"id": "chile_pepper",  "name": "Chile Pepper",  "emoji": "🌶️",  "commodity": "PEPPERS",  "unit": "$ / CWT",  "state": "NM"},
    {"id": "onion",         "name": "Onion",         "emoji": "🧅",  "commodity": "ONIONS",   "unit": "$ / CWT",  "state": "NM"},
    {"id": "corn",          "name": "Corn",          "emoji": "🌽",  "commodity": "CORN",     "unit": "$ / BU",   "state": "NM"},
    {"id": "alfalfa",       "name": "Alfalfa / Hay", "emoji": "🌾",  "commodity": "HAY",      "unit": "$ / TON",  "state": "NM"},
    {"id": "wheat",         "name": "Wheat",         "emoji": "🌾",  "commodity": "WHEAT",    "unit": "$ / BU",   "state": "NM"},
    {"id": "pecans",        "name": "Pecans",        "emoji": "🥜",  "commodity": "PECANS",   "unit": "$ / LB",   "state": "NM"},
    {"id": "cotton",        "name": "Cotton",        "emoji": "🪻",  "commodity": "COTTON",   "unit": "$ / TON",  "state": "NM"},
]

# Alert threshold: flag if current price deviates by this % from 5-yr avg
_ALERT_THRESHOLD = 10.0   # ±10%

# Best months to sell per crop (agronomic / market knowledge)
_BEST_SELL_MONTHS = {
    "chile_pepper":  [9, 10],     # peak harvest Aug-Oct, prices highest Sep-Oct
    "onion":         [6, 7],      # NM onion harvest Jun-Jul
    "corn":          [8, 9],      # late summer harvest
    "alfalfa":       [6, 7, 8],   # multiple cuts, peak summer
    "wheat":         [6, 7],      # winter wheat harvest Jun-Jul
    "pecans":        [11, 12],    # NM pecan harvest Nov-Dec
    "cotton":        [11, 12],    # gin season
    "sorghum":       [9, 10],
}


def _fetch_monthly_prices(commodity: str, state: str, unit: str) -> list[dict]:
    """
    Fetch all available monthly PRICE RECEIVED records for the last 6 years.
    Returns list of {year, month_num, month_abbr, price} dicts.
    """
    if not _NASS_KEY:
        return []
    try:
        cutoff_year = str(datetime.now().year - 6)
        r = requests.get(_BASE_URL, params={
            "key":              _NASS_KEY,
            "source_desc":      "SURVEY",
            "commodity_desc":   commodity,
            "statisticcat_desc":"PRICE RECEIVED",
            "state_alpha":      state,
            "freq_desc":        "MONTHLY",
            "year__GE":         cutoff_year,
            "format":           "json",
        }, timeout=15)
        items = r.json().get("data", [])
    except Exception as e:
        logger.warning("NASS monthly fetch failed for %s: %s", commodity, e)
        return []

    result = []
    for item in items:
        ref = str(item.get("reference_period_desc", "")).strip().upper()
        if ref not in _MONTH_ABBR:
            continue
        raw_val = str(item.get("Value", "")).replace(",", "").strip()
        if not raw_val or raw_val in ("(NA)", "(D)", "(Z)"):
            continue
        try:
            price = float(raw_val)
        except ValueError:
            continue
        # Only keep rows matching the expected unit (loose match)
        item_unit = str(item.get("unit_desc", "")).upper()
        if unit.replace(" ", "") not in item_unit.replace(" ", ""):
            continue
        result.append({
            "year":        int(item.get("year", 0)),
            "month_num":   _MONTH_ABBR.index(ref) + 1,
            "month_abbr":  ref,
            "price":       price,
        })
    return result


def _build_alerts() -> list[dict]:
    """
    For each monitored crop, compute 5-year monthly average and compare to
    the most recent month available.  Fetches are parallelised across all
    crops so total time ≈ slowest single request instead of sum.
    """
    now = datetime.now()
    alerts = []

    # Fetch all crops in parallel (max 7 workers — one per crop)
    with ThreadPoolExecutor(max_workers=len(_ALERT_CROPS)) as pool:
        futures = {
            pool.submit(_fetch_monthly_prices, crop["commodity"], crop["state"], crop["unit"]): crop
            for crop in _ALERT_CROPS
        }
        crop_records = {}
        for fut in as_completed(futures):
            crop = futures[fut]
            try:
                crop_records[crop["id"]] = fut.result()
            except Exception as exc:
                logger.warning("NASS alerts parallel fetch failed for %s: %s", crop["id"], exc)
                crop_records[crop["id"]] = []

    for crop in _ALERT_CROPS:
        records = crop_records.get(crop["id"], [])
        if len(records) < 3:
            logger.info("NASS alerts: insufficient monthly data for %s", crop["id"])
            continue

        # Build per-month average over 5-year window (exclude the most recent year
        # so we compare current vs historical)
        latest_year = max(r["year"] for r in records)
        historical  = [r for r in records if r["year"] < latest_year]
        current_yr  = [r for r in records if r["year"] == latest_year]

        if not historical or not current_yr:
            continue

        # Monthly averages from historical data
        monthly_avg: dict[int, list[float]] = {}
        for r in historical:
            monthly_avg.setdefault(r["month_num"], []).append(r["price"])
        avg_by_month = {m: sum(v)/len(v) for m, v in monthly_avg.items()}

        # Most recent data point in current year
        latest = max(current_yr, key=lambda r: r["month_num"])
        month_num = latest["month_num"]
        current_price = latest["price"]

        if month_num not in avg_by_month:
            continue

        hist_avg = avg_by_month[month_num]
        pct_diff = round((current_price - hist_avg) / hist_avg * 100, 1)

        # Best month advice
        best_months = _BEST_SELL_MONTHS.get(crop["id"], [])
        is_best_month = now.month in best_months
        best_month_names = [datetime(2000, m, 1).strftime("%b") for m in best_months]

        # Build alert object only if notable (above threshold OR in best sell window)
        if abs(pct_diff) >= _ALERT_THRESHOLD or is_best_month:
            if pct_diff >= _ALERT_THRESHOLD:
                alert_type = "good"
                message = (
                    f"Price is {pct_diff:+.1f}% above the {latest_year - min(r['year'] for r in historical)}-year "
                    f"average for {_MONTH_ABBR[month_num-1]} — good time to sell."
                )
            elif pct_diff <= -_ALERT_THRESHOLD:
                alert_type = "low"
                message = (
                    f"Price is {pct_diff:+.1f}% below average for {_MONTH_ABBR[month_num-1]}."
                )
                if best_month_names:
                    message += f" Typically strongest in {', '.join(best_month_names)}."
            else:
                alert_type = "neutral"
                message = (
                    f"Price near average ({pct_diff:+.1f}%). "
                    f"Peak sell window: {', '.join(best_month_names)}." if best_month_names
                    else f"Price near historical average ({pct_diff:+.1f}%)."
                )

            alerts.append({
                "crop_id":        crop["id"],
                "crop_name":      crop["name"],
                "emoji":          crop["emoji"],
                "alert_type":     alert_type,    # good / low / neutral
                "current_price":  current_price,
                "hist_avg":       round(hist_avg, 2),
                "pct_diff":       pct_diff,
                "unit":           crop["unit"],
                "month":          _MONTH_ABBR[month_num-1],
                "year":           latest_year,
                "message":        message,
                "is_best_month":  is_best_month,
                "best_months":    best_month_names,
            })

    # Sort: good alerts first, then neutral, then low
    order = {"good": 0, "neutral": 1, "low": 2}
    alerts.sort(key=lambda a: order.get(a["alert_type"], 3))
    return alerts


def get_price_alerts(force_refresh: bool = False) -> list[dict]:
    """Return cached price alerts (refreshed if > 24h old)."""
    os.makedirs(_CACHE_DIR, exist_ok=True)

    if not force_refresh and os.path.exists(_CACHE_FILE):
        try:
            with open(_CACHE_FILE, encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - cached.get("_fetched_at", 0) < _CACHE_TTL:
                return cached.get("alerts", [])
        except Exception:
            pass

    alerts = _build_alerts()

    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"_fetched_at": time.time(), "alerts": alerts}, f, indent=2)
    except Exception as e:
        logger.warning("NASS alerts: could not write cache: %s", e)

    return alerts
