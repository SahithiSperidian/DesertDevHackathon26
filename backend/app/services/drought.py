"""
US Drought Monitor service.
Fetches the latest drought severity for Bernalillo County, NM (FIPS 35001).
Returns a drought level 0–4 (D0–D4) and a human label.
No API key required.
"""

import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# Bernalillo County, NM FIPS code
_FIPS = "35001"
_CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "live", "drought_cache.json")
_CACHE_MAX_AGE_SECONDS = 43200  # 12 hours (drought data updates weekly)

# US Drought Monitor county statistics API
# statisticsType=1 = percent area in each drought category
def _build_url() -> str:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=14)
    return (
        "https://usdmdataservices.unl.edu/api/CountyStatistics/"
        "GetDroughtSeverityStatisticsByAreaPercent"
        f"?aoi={_FIPS}&startdate={start}&enddate={end}&statisticsType=1"
    )


_DROUGHT_LABELS = {
    0: "No Drought",
    1: "Abnormally Dry (D0)",
    2: "Moderate Drought (D1)",
    3: "Severe Drought (D2)",
    4: "Extreme Drought (D3)",
    5: "Exceptional Drought (D4)",
}

_DROUGHT_COLORS = {
    0: "#4caf50",
    1: "#ffeb3b",
    2: "#ff9800",
    3: "#f44336",
    4: "#9c27b0",
    5: "#4a0000",
}


def _parse_dominant_level(record: dict) -> int:
    """
    Given a USDM record, return the dominant drought level (0-5)
    based on the highest category with meaningful area coverage (>10%).
    """
    for level in range(5, -1, -1):
        key = f"D{level}" if level > 0 else "None"
        try:
            if float(record.get(key, 0)) > 10:
                return level
        except (ValueError, TypeError):
            pass
    return 0


def _load_cache():
    path = os.path.abspath(_CACHE_PATH)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            cache = json.load(f)
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(cache["fetched_at"])).total_seconds()
        if age < _CACHE_MAX_AGE_SECONDS:
            return cache
    except Exception:
        pass
    return None


def _save_cache(data: dict):
    path = os.path.abspath(_CACHE_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def get_drought_status() -> dict:
    """
    Returns:
      {
        "level": int,          # 0 (none) – 5 (exceptional)
        "label": str,          # human-readable label
        "color": str,          # hex color for UI
        "county": str,         # "Bernalillo County, NM"
        "source": str
      }
    """
    cached = _load_cache()
    if cached:
        cached["source"] = "cache"
        return cached

    try:
        url = _build_url()
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            records = json.loads(resp.read())

        if not records:
            raise ValueError("Empty drought response")

        # Use the most recent record
        latest = records[-1]
        level = _parse_dominant_level(latest)
        result = {
            "level": level,
            "label": _DROUGHT_LABELS[level],
            "color": _DROUGHT_COLORS[level],
            "county": "Bernalillo County, NM",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "live",
        }
        _save_cache(result)
        return result

    except Exception:
        # Conservative fallback — assume moderate drought for NM
        return {
            "level": 2,
            "label": "Moderate Drought (D1) — estimated",
            "color": _DROUGHT_COLORS[2],
            "county": "Bernalillo County, NM",
            "source": "fallback",
        }
