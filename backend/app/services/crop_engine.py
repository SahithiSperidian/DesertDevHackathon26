"""
Crop suggestion engine.

Combines three signals:
  1. Soil test results  (pH, nitrogen, phosphorus, organic matter)
  2. Water stress score (from USGS live streamflow)
  3. Drought level      (from US Drought Monitor)

Returns a ranked list of suitable crops with a match score,
soil feedback, water risk flag, and economics per acre.
"""

import json
import os
import logging

from app.services.usgs_water import get_water_status
from app.services.drought import get_drought_status
from app.services.nass_economics import get_nass_economics
from app.services.noaa_rainfall import get_seasonal_rainfall
from app.services.et_calculator import get_et_normals, crop_seasonal_et_inches

logger = logging.getLogger(__name__)

_GALLONS_PER_ACRE_INCH = 27_154
_RAINFALL_FALLBACK_IN  = 7.5


def _om_retention_adj(om_pct: float) -> int:
    """% water adjustment from soil organic matter (mirrors water_guide logic)."""
    if om_pct >= 3.0:
        return -10
    elif om_pct >= 1.5:
        return 0
    return +15


def _stress_adj_pct(stress_score: int) -> int:
    """% water adjustment from USGS water stress (mirrors water_guide logic)."""
    if stress_score <= 25:
        return 0
    elif stress_score <= 50:
        return +5
    elif stress_score <= 75:
        return +10
    return +20

_CROP_DATA_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "static", "crop_requirements.json"
)


def _load_crops() -> list:
    path = os.path.abspath(_CROP_DATA_PATH)
    with open(path, encoding="utf-8") as f:
        return json.load(f)["crops"]


def _enrich_economics(crop: dict, nass: dict) -> dict:
    """
    Return economics dict for this crop.
    Uses real NASS data when available; falls back to crop_requirements.json values.
    """
    crop_id = crop["id"]
    nass_data = nass.get(crop_id)
    if nass_data:
        return {
            "investment_per_acre_usd": nass_data["investment_per_acre_usd"],
            "revenue_per_acre_usd":    nass_data["revenue_per_acre_usd"],
            "profit_per_acre_usd":     nass_data["profit_per_acre_usd"],
            "source": "USDA NASS " + nass_data.get("price_source", ""),
            "data_year": nass_data.get("data_year"),
            "price_detail": f"{nass_data['price_per_unit']:.2f} {nass_data['price_unit']} "
                            f"× {nass_data['yield_per_acre']:.1f} {nass_data['yield_unit']}",
        }
    # Fallback to static JSON estimates (labelled so UI can show caveat)
    econ = dict(crop["economics"])
    econ["source"] = "estimated"
    return econ


# ── Normalise soil test values ─────────────────────────────────────────────

_NITROGEN_ORDER = ["low", "medium", "high"]
_PHOSPHORUS_ORDER = ["low", "medium", "high"]


def _nitrogen_index(label: str) -> int:
    label = label.lower()
    return _NITROGEN_ORDER.index(label) if label in _NITROGEN_ORDER else 1


def _phosphorus_index(label: str) -> int:
    label = label.lower()
    return _PHOSPHORUS_ORDER.index(label) if label in _PHOSPHORUS_ORDER else 1


# ── Per-crop scoring ───────────────────────────────────────────────────────

def _score_crop(crop: dict, soil: dict, water_stress: int, drought_level: int) -> dict:
    """
    Returns a dict with:
      score          int 0-100
      soil_ok        bool
      water_ok       bool
      issues         list[str]   human-readable warnings
      amendments     list[str]   actionable soil improvements
    """
    score = 100
    issues = []
    amendments = []
    soil_ok = True
    water_ok = True

    req = crop["soil"]

    # ── pH check ──────────────────────────────────────────────────────────
    ph = float(soil.get("pH", 7.0))
    if ph < req["ph_min"]:
        deficit = round(req["ph_min"] - ph, 1)
        score -= 25
        soil_ok = False
        issues.append(f"Soil pH {ph} is too acidic (need ≥ {req['ph_min']})")
        amendments.append(f"Add agricultural lime to raise pH by ~{deficit} units")
    elif ph > req["ph_max"]:
        excess = round(ph - req["ph_max"], 1)
        score -= 25
        soil_ok = False
        issues.append(f"Soil pH {ph} is too alkaline (need ≤ {req['ph_max']})")
        amendments.append(f"Add elemental sulfur or acidic compost to lower pH by ~{excess} units")

    # ── Nitrogen check ────────────────────────────────────────────────────
    soil_n = soil.get("nitrogen", "medium").lower()
    required_ns = crop["soil"]["nitrogen"]
    if soil_n not in required_ns:
        soil_n_idx = _nitrogen_index(soil_n)
        req_min_idx = min(_nitrogen_index(n) for n in required_ns)
        if soil_n_idx < req_min_idx:
            score -= 15
            soil_ok = False
            issues.append(f"Nitrogen is {soil_n} — {crop['name']} needs {' or '.join(required_ns)}")
            amendments.append("Apply balanced fertilizer (10-10-10) or composted manure to boost nitrogen")

    # ── Phosphorus check ──────────────────────────────────────────────────
    soil_p = soil.get("phosphorus", "medium").lower()
    required_ps = crop["soil"]["phosphorus"]
    if soil_p not in required_ps:
        soil_p_idx = _phosphorus_index(soil_p)
        req_min_idx = min(_phosphorus_index(p) for p in required_ps)
        if soil_p_idx < req_min_idx:
            score -= 10
            soil_ok = False
            issues.append(f"Phosphorus is {soil_p} — {crop['name']} needs {' or '.join(required_ps)}")
            amendments.append("Add bone meal or rock phosphate to increase available phosphorus")

    # ── Organic matter check ──────────────────────────────────────────────
    om = float(soil.get("organic_matter", "1.0").replace("%", ""))
    if om < req["organic_matter_min"]:
        score -= 10
        soil_ok = False
        issues.append(f"Organic matter {om}% is low (need ≥ {req['organic_matter_min']}%)")
        amendments.append("Incorporate compost at 2–3 tons/acre to improve organic matter and water retention")

    # ── Water stress check ────────────────────────────────────────────────
    water_need = crop["water"]["need"]          # low / medium / high
    drought_tol = crop["water"]["drought_tolerance"]  # low / medium / high

    # High-water crops under high stress → penalise
    if water_need == "high" and water_stress >= 60:
        score -= 20
        water_ok = False
        issues.append(f"{crop['name']} needs high water but river stress is elevated ({water_stress}/100)")

    if water_need == "medium" and water_stress >= 75:
        score -= 10
        water_ok = False
        issues.append(f"Water stress is high — consider drip irrigation for {crop['name']}")

    # Bonus for drought-tolerant crops under drought
    if drought_tol == "high" and drought_level >= 2:
        score += 10
        issues.append(f"✓ Drought-tolerant — good choice under current drought conditions")

    if drought_tol == "low" and drought_level >= 3:
        score -= 15
        water_ok = False
        issues.append(f"Severe drought detected — {crop['name']} is drought-sensitive, higher risk")

    score = max(0, min(100, score))

    return {
        "score": score,
        "soil_ok": soil_ok,
        "water_ok": water_ok,
        "issues": issues,
        "amendments": amendments,
    }


# ── Public API ─────────────────────────────────────────────────────────────

def suggest_crops(soil_results: dict, location: str | None = None) -> dict:
    """
    Main entry point.

    Args:
        soil_results: dict with keys:
            pH (float as str), nitrogen (str), phosphorus (str), organic_matter (str)
        location: farmer's location string (e.g. "Albuquerque, NM") for live water calc.

    Returns:
        {
          "crops": [ranked list of crop suggestion dicts],
          "water": water status dict,
          "drought": drought status dict,
          "soil_summary": list of overall soil tips,
          "rainfall_data": live NOAA rainfall dict,
          "et_method": str,
        }
    """
    crops = _load_crops()
    water = get_water_status()
    drought = get_drought_status()
    nass = get_nass_economics()          # real NASS economics (cached 24h)

    water_stress = water["stress_score"]
    drought_level = drought["level"]

    # ── Live water data for per-crop budget calculation ─────────────────────
    try:
        rainfall_data = get_seasonal_rainfall(location)
        seasonal_rain_in = rainfall_data["inches"]
    except Exception as exc:
        logger.warning("NOAA rainfall fetch failed in crop engine: %s", exc)
        rainfall_data = {"inches": _RAINFALL_FALLBACK_IN, "city": "New Mexico",
                         "station_id": None, "source": "fallback"}
        seasonal_rain_in = _RAINFALL_FALLBACK_IN

    try:
        et_normals = get_et_normals(location)
    except Exception as exc:
        logger.warning("ET normals fetch failed in crop engine: %s", exc)
        et_normals = {"eto_monthly": {}, "source": "fallback", "method": "fallback", "station_id": None}

    # Soil OM for retention adjustment
    om_pct = 1.0
    try:
        raw = str(soil_results.get("organic_matter", "1.0")).replace("%", "")
        om_pct = float(raw)
    except (ValueError, TypeError):
        pass

    retention_adj = _om_retention_adj(om_pct)
    stress_adj    = _stress_adj_pct(water_stress)
    total_adj     = retention_adj + stress_adj
    et_method     = et_normals.get("method", "fallback")

    results = []
    for crop in crops:
        evaluation = _score_crop(crop, soil_results, water_stress, drought_level)

        # Live water budget for this crop
        base_in     = crop_seasonal_et_inches(crop, et_normals)
        adjusted_in = round(base_in * (1 + total_adj / 100), 1)
        net_in      = max(0.0, round(adjusted_in - seasonal_rain_in, 1))
        net_gal     = round(net_in * _GALLONS_PER_ACRE_INCH)

        results.append({
            "id": crop["id"],
            "name": crop["name"],
            "emoji": crop["emoji"],
            "description": crop["description"],
            "score": evaluation["score"],
            "soil_ok": evaluation["soil_ok"],
            "water_ok": evaluation["water_ok"],
            "issues": evaluation["issues"],
            "amendments": evaluation["amendments"],
            "economics": _enrich_economics(crop, nass),
            "water_need": crop["water"]["need"],
            "drought_tolerance": crop["water"]["drought_tolerance"],
            "irrigation_inches": crop["water"]["irrigation_inches_per_season"],
            "days_to_harvest": crop["days_to_harvest"],
            "season": crop["season"],
            # Live water budget fields
            "water_base_in":    base_in,
            "water_adjusted_in": adjusted_in,
            "water_net_in":     net_in,
            "water_net_gal":    net_gal,
            "water_rainfall_in": round(seasonal_rain_in, 1),
            "water_et_method":  et_method,
        })

    # Sort by score descending
    results.sort(key=lambda x: x["score"], reverse=True)

    # Overall soil amendment tips (deduplicated across all crops)
    all_amendments = []
    seen = set()
    for r in results[:5]:
        for tip in r["amendments"]:
            if tip not in seen:
                all_amendments.append(tip)
                seen.add(tip)

    return {
        "crops": results,
        "water": water,
        "drought": drought,
        "soil_summary": all_amendments,
        "rainfall_data": rainfall_data,
        "et_method": et_method,
    }
