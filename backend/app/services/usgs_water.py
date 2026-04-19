"""
USGS Water Data service.

Uses TWO USGS APIs (both free, no key required):

  1. Daily Values API  - fetches the most recent 7-day streamflow at the
     Rio Grande at Albuquerque gauge (site 08330000).

  2. Statistics API    - fetches the historical daily percentiles (P10, P25,
     P50, P75, P90) for the same gauge across its full period of record
     (1974-2025, 52 years of approved data).

The stress score 0-100 is computed by comparing today real flow against the
real historical percentiles for this exact calendar date - no hardcoded
thresholds.

  flow >= p90  ->  score  0-10   (well above normal)
  p75-p90      ->  score 10-25   (above normal)
  p50-p75      ->  score 25-50   (near normal)
  p25-p50      ->  score 50-75   (below normal)
  p10-p25      ->  score 75-90   (significantly low)
  flow < p10   ->  score 90-100  (critically low)
"""

import io
import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone

# Site config
USGS_SITE = "08330000"   # Rio Grande at Albuquerque, NM

# Daily Values API - last 7 days of discharge
_DV_URL = (
    "https://waterservices.usgs.gov/nwis/dv/"
    "?format=json"
    "&sites=" + USGS_SITE +
    "&parameterCd=00060"
    "&period=P7D"
)

# Statistics API - daily percentiles across full period of record
_STATS_URL = (
    "https://waterservices.usgs.gov/nwis/stat/"
    "?sites=" + USGS_SITE +
    "&parameterCd=00060"
    "&statReportType=daily"
    "&statTypeCd=P10,P25,P50,P75,P90"
    "&format=rdb"
)

# Cache files
_CACHE_DIR  = os.path.join(os.path.dirname(__file__), "..", "..", "data", "live")
_DV_CACHE   = os.path.join(_CACHE_DIR, "usgs_cache.json")
_STAT_CACHE = os.path.join(_CACHE_DIR, "usgs_stats_cache.json")

_DV_CACHE_TTL   = 3600        # 1 hour
_STAT_CACHE_TTL = 86400 * 30  # 30 days - percentiles update once a year


def _load_json_cache(path, max_age):
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(data["fetched_at"])).total_seconds()
        if age < max_age:
            return data
    except Exception:
        pass
    return None


def _save_json_cache(path, data):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _fetch_stats():
    """
    Returns dict keyed "MM-DD" -> {p10, p25, p50, p75, p90} in cfs.
    Cached for 30 days.
    """
    cached = _load_json_cache(_STAT_CACHE, _STAT_CACHE_TTL)
    if cached:
        return cached["percentiles"]

    req = urllib.request.Request(_STATS_URL, headers={"User-Agent": "CropPulse/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw_text = resp.read().decode("utf-8")

    percentiles = {}
    header = None
    for line in io.StringIO(raw_text):
        line = line.rstrip("\n")
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if header is None:
            header = parts
            continue
        # Skip the format-descriptor row (contains only type codes like "5s", "3n")
        if all(p.strip() in ("5s","15s","10n","3n","6n","8n","12s","12n","") for p in parts):
            continue
        row = dict(zip(header, parts))
        try:
            month = int(row["month_nu"])
            day   = int(row["day_nu"])
            def _f(k):
                v = row.get(k, "").strip()
                return float(v) if v else None
            percentiles[f"{month:02d}-{day:02d}"] = {
                "p10": _f("p10_va"),
                "p25": _f("p25_va"),
                "p50": _f("p50_va"),
                "p75": _f("p75_va"),
                "p90": _f("p90_va"),
            }
        except (KeyError, ValueError):
            continue

    _save_json_cache(_STAT_CACHE, {
        "percentiles": percentiles,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })
    return percentiles


def _percentile_stress(flow_cfs, p):
    """
    Compare flow against today historical percentiles.
    Returns (stress_score 0-100, status_label).
    Low flow = high stress (inverted percentile).
    None percentiles are skipped — only the available thresholds are used.
    """
    if not p or flow_cfs is None:
        return 50, "Unknown"

    # Use only the percentiles the API actually returned (not None)
    p10 = p.get("p10")
    p25 = p.get("p25")
    p50 = p.get("p50")
    p75 = p.get("p75")
    p90 = p.get("p90")

    # Walk from highest to lowest, skipping None values
    if p90 is not None and flow_cfs >= p90:
        score = max(0, round(10 * (1 - (flow_cfs - p90) / max(p90, 1))))
        label = "Well Above Normal"
    elif p75 is not None and flow_cfs >= p75:
        score = round(10 + 15 * (1 - (flow_cfs - p75) / max((p90 or p75*1.5) - p75, 1)))
        label = "Above Normal"
    elif p50 is not None and flow_cfs >= p50:
        score = round(25 + 25 * (1 - (flow_cfs - p50) / max((p75 or p50*1.5) - p50, 1)))
        label = "Near Normal"
    elif p25 is not None and flow_cfs >= p25:
        score = round(50 + 25 * (1 - (flow_cfs - p25) / max((p50 or p25*1.5) - p25, 1)))
        label = "Below Normal"
    elif p10 is not None and flow_cfs >= p10:
        score = round(75 + 15 * (1 - (flow_cfs - p10) / max((p25 or p10*1.5) - p10, 1)))
        label = "Much Below Normal"
    elif p10 is not None:
        # flow is below p10 — critically low
        score = round(90 + 10 * (1 - flow_cfs / max(p10, 1)))
        label = "Critical"
    else:
        # No percentile data available
        return 50, "Unknown"

    return max(0, min(100, score)), label


def get_water_status():
    """
    Returns:
      {
        "flow_cfs":     float,       latest daily discharge (cfs)
        "stress_score": int,         0 (good) - 100 (extreme stress)
        "status":       str,         human-readable label
        "percentiles":  dict,        {p10, p25, p50, p75, p90} for today date
        "trend":        list[float], last 7 days cfs oldest first
        "source":       str          live | cache | fallback
      }
    """
    cached = _load_json_cache(_DV_CACHE, _DV_CACHE_TTL)
    if cached:
        cached["source"] = "cache"
        return cached

    try:
        # 1. Current flow - Daily Values API
        req = urllib.request.Request(_DV_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read())

        values = (
            raw.get("value", {})
               .get("timeSeries", [{}])[0]
               .get("values", [{}])[0]
               .get("value", [])
        )
        trend = []
        for v in values:
            try:
                trend.append(float(v["value"]))
            except (KeyError, ValueError):
                pass

        latest_cfs = trend[-1] if trend else None

        # 2. Historical percentiles for today - Statistics API
        today_key = datetime.now().strftime("%m-%d")
        try:
            stats = _fetch_stats()
            today_percentiles = stats.get(today_key, {})
        except Exception:
            today_percentiles = {}

        # 3. Stress score from real percentiles
        stress, status = _percentile_stress(latest_cfs or 0, today_percentiles)

        result = {
            "flow_cfs":     round(latest_cfs, 1) if latest_cfs is not None else None,
            "stress_score": stress,
            "status":       status,
            "percentiles":  today_percentiles,
            "trend":        [round(x, 1) for x in trend],
            "fetched_at":   datetime.now(timezone.utc).isoformat(),
            "source":       "live",
        }
        _save_json_cache(_DV_CACHE, result)
        return result

    except Exception:
        return {
            "flow_cfs":     None,
            "stress_score": 50,
            "status":       "Unknown",
            "percentiles":  {},
            "trend":        [],
            "source":       "fallback",
        }