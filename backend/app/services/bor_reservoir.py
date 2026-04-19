"""
Bureau of Reclamation RISE API — Elephant Butte Reservoir storage service.

API docs: https://data.usbr.gov/rise-api
No authentication required; header 'accept: application/vnd.api+json' required.

Elephant Butte Reservoir (Rio Grande Project, NM) — item IDs:
  329  — Daily Storage (acre-feet)          ← primary
  332  — Daily Elevation (ft)
  4377 — Daily Release Total (cfs)
  330  — Daily Inflow (cfs)

Full conservation pool capacity: 2,065,625 acre-feet (BOR, 2024)
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta

import requests

logger = logging.getLogger(__name__)

_RISE_BASE   = "https://data.usbr.gov/rise/api/result"
_RISE_HEADERS = {"accept": "application/vnd.api+json"}
_TIMEOUT      = 12   # seconds

_FULL_CAPACITY_AF = 2_065_625   # Elephant Butte conservation pool

_CACHE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "live", "bor_reservoir_cache.json"
)
_CACHE_TTL_HOURS = 6   # BOR updates daily; 6 h is sufficient

# ── Status thresholds (% of full capacity) ────────────────────────────────
_STATUS_THRESHOLDS = [
    (15, "Critical",     "danger"),
    (35, "Low",          "danger"),
    (60, "Below Normal", "warning"),
    (85, "Normal",       "success"),
    (101, "High",        "info"),
]


def _status_from_pct(pct: float) -> tuple[str, str]:
    for threshold, label, color in _STATUS_THRESHOLDS:
        if pct < threshold:
            return label, color
    return "High", "info"


# ── Cache helpers ──────────────────────────────────────────────────────────

def _read_cache() -> dict | None:
    try:
        path = os.path.abspath(_CACHE_PATH)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        cached_at = datetime.fromisoformat(data["cached_at"])
        age_h = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
        if age_h > _CACHE_TTL_HOURS:
            return None
        return data
    except Exception:
        return None


def _write_cache(payload: dict) -> None:
    try:
        path = os.path.abspath(_CACHE_PATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload["cached_at"] = datetime.now(timezone.utc).isoformat()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as exc:
        logger.warning("BOR cache write failed: %s", exc)


# ── RISE API fetch ─────────────────────────────────────────────────────────

def _fetch_item(item_id: int, days_back: int = 35) -> list[dict]:
    """
    Return a list of {date, value} dicts for the given RISE item ID,
    covering the last `days_back` days, newest first.
    """
    after = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    params = {
        "itemId":       item_id,
        "itemsPerPage": days_back,
        "dateTime[after]": after,
        "order[dateTime]": "desc",
    }
    resp = requests.get(_RISE_BASE, headers=_RISE_HEADERS, params=params,
                        timeout=_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    rows = []
    for record in body.get("data", []):
        attrs = record.get("attributes", {})
        dt_raw = attrs.get("dateTime", "")
        val    = attrs.get("result")
        if dt_raw and val is not None:
            try:
                dt = datetime.fromisoformat(dt_raw).strftime("%Y-%m-%d")
                rows.append({"date": dt, "value": float(val)})
            except (ValueError, TypeError):
                continue
    return rows


# ── Trend analysis ─────────────────────────────────────────────────────────

def _compute_trend(storage_series: list[dict]) -> dict:
    """
    Given a list of {date, value} dicts (newest first), compute:
      - delta_7d  : change over last 7 days (positive = filling)
      - delta_30d : change over last 30 days
      - direction : "rising" | "falling" | "stable"
      - sparkline : list of values for charting (oldest first, last 30 pts)
    """
    if len(storage_series) < 2:
        return {"delta_7d": None, "delta_30d": None, "direction": "stable",
                "sparkline": []}

    latest = storage_series[0]["value"]

    delta_7d = None
    if len(storage_series) >= 7:
        delta_7d = round(latest - storage_series[6]["value"])

    delta_30d = None
    if len(storage_series) >= 30:
        delta_30d = round(latest - storage_series[29]["value"])

    # Direction from 7-day trend (or 30d if insufficient)
    ref = delta_7d if delta_7d is not None else delta_30d
    if ref is None:
        direction = "stable"
    elif ref > 5_000:
        direction = "rising"
    elif ref < -5_000:
        direction = "falling"
    else:
        direction = "stable"

    sparkline = [r["value"] for r in reversed(storage_series[:30])]
    return {
        "delta_7d":  delta_7d,
        "delta_30d": delta_30d,
        "direction": direction,
        "sparkline": sparkline,
    }


def _format_af(af: float) -> str:
    """Format acre-feet with comma separation and rounding to nearest thousand."""
    return f"{round(af / 1000) * 1000:,.0f}"


# ── Public API ─────────────────────────────────────────────────────────────

def get_reservoir_status() -> dict:
    """
    Return live Elephant Butte storage status from BOR RISE API.

    Returns:
        storage_af      float  — latest daily storage in acre-feet
        storage_af_fmt  str    — human-readable (e.g. "530,000")
        pct_full        float  — percent of full capacity (1 dp)
        status          str    — "Critical" | "Low" | "Below Normal" | "Normal" | "High"
        status_color    str    — Bootstrap colour name
        direction       str    — "rising" | "falling" | "stable"
        direction_icon  str    — Bootstrap icon class
        delta_7d        int|None  — 7-day storage change (af)
        delta_30d       int|None  — 30-day storage change (af)
        sparkline       list   — up to 30 daily values for micro-chart
        date            str    — ISO date of latest reading (YYYY-MM-DD)
        full_capacity   int    — reservoir full capacity in af
        source          str    — "bor_rise" | "cache" | "unavailable"
    """
    cached = _read_cache()
    if cached and "storage_af" in cached:
        cached["source"] = "cache"
        return cached

    try:
        series = _fetch_item(329, days_back=35)
    except Exception as exc:
        logger.warning("BOR RISE fetch failed: %s", exc)
        return _unavailable_payload()

    if not series:
        logger.warning("BOR RISE returned no data for item 329")
        return _unavailable_payload()

    latest    = series[0]
    storage_af = latest["value"]
    pct_full   = round(storage_af / _FULL_CAPACITY_AF * 100, 1)
    status, status_color = _status_from_pct(pct_full)
    trend = _compute_trend(series)

    direction_icons = {
        "rising":  "bi-arrow-up-circle-fill",
        "falling": "bi-arrow-down-circle-fill",
        "stable":  "bi-dash-circle-fill",
    }

    payload = {
        "storage_af":     storage_af,
        "storage_af_fmt": _format_af(storage_af),
        "pct_full":       pct_full,
        "status":         status,
        "status_color":   status_color,
        "direction":      trend["direction"],
        "direction_icon": direction_icons[trend["direction"]],
        "delta_7d":       trend["delta_7d"],
        "delta_30d":      trend["delta_30d"],
        "sparkline":      trend["sparkline"],
        "date":           latest["date"],
        "full_capacity":  _FULL_CAPACITY_AF,
        "source":         "bor_rise",
    }

    _write_cache(payload)
    return payload


def _unavailable_payload() -> dict:
    return {
        "storage_af":     None,
        "storage_af_fmt": "N/A",
        "pct_full":       None,
        "status":         "Data Unavailable",
        "status_color":   "secondary",
        "direction":      "stable",
        "direction_icon": "bi-dash-circle-fill",
        "delta_7d":       None,
        "delta_30d":      None,
        "sparkline":      [],
        "date":           None,
        "full_capacity":  _FULL_CAPACITY_AF,
        "source":         "unavailable",
    }
