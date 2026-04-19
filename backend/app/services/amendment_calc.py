"""
Amendment Quantity Calculator
=============================
Uses real NMSU Cooperative Extension Service agronomic rates to calculate
how much soil amendment a farmer needs per acre, then finds the nearest
EPA-listed NM facility to source it.

Published references used:
  - NMSU Circular CR-504 : Compost Use in New Mexico Agriculture
  - NMSU Extension Guide A-129: Commercial Vegetable Production in NM
  - NMSU Circular A-122  : Soil and Plant Tissue Testing in NM

Distance is computed with the Haversine formula using lat/lon from the
EPA Excess Food Opportunities Map dataset (already loaded by epa_resources.py).
"""

import math
import re

from backend.app.services.epa_resources import get_nm_resources

# ---------------------------------------------------------------------------
# Real NM city geocoordinates (latitude, longitude)
# Source: US Census Bureau / NM state GIS
# ---------------------------------------------------------------------------
_NM_CITIES: dict[str, tuple[float, float]] = {
    "albuquerque":              (35.0844, -106.6504),
    "santa fe":                 (35.6870, -105.9378),
    "las cruces":               (32.3199, -106.7637),
    "rio rancho":               (35.2328, -106.6630),
    "roswell":                  (33.3943, -104.5230),
    "farmington":               (36.7281, -108.2087),
    "clovis":                   (34.4048, -103.2052),
    "hobbs":                    (32.7026, -103.1360),
    "alamogordo":               (32.8995, -105.9603),
    "carlsbad":                 (32.4207, -104.2288),
    "gallup":                   (35.5281, -108.7426),
    "taos":                     (36.4072, -105.5731),
    "los lunas":                (34.8062, -106.7314),
    "belen":                    (34.6618, -106.7742),
    "bernalillo":               (35.3001, -106.5531),
    "socorro":                  (34.0584, -106.8914),
    "silver city":              (32.7701, -108.2803),
    "truth or consequences":    (33.1290, -107.2528),
    "deming":                   (32.2687, -107.7586),
    "las vegas":                (35.5939, -105.2228),
    "espanola":                 (35.9928, -106.0744),
    "española":                 (35.9928, -106.0744),
    "portales":                 (34.1873, -103.3335),
    "ruidoso":                  (33.3315, -105.6569),
    "artesia":                  (32.8428, -104.4030),
    "lovington":                (32.9440, -103.3488),
    "grants":                   (35.1475, -107.8514),
    "los alamos":               (35.8800, -106.2952),
    "moriarty":                 (34.9943, -106.0489),
    "edgewood":                 (35.0617, -106.1911),
    "corrales":                 (35.2375, -106.6086),
    "rio communities":          (34.6500, -106.7800),
    "tijeras":                  (35.0929, -106.3844),
    "estancia":                 (34.7601, -106.0564),
    "mountainair":              (34.5212, -106.2428),
    "milan":                    (35.1906, -107.8903),
    "aztec":                    (36.8228, -107.9923),
    "bloomfield":               (36.7128, -107.9846),
    "raton":                    (36.9042, -104.4394),
    "tucumcari":                (35.1717, -103.7252),
    "santa rosa":               (34.9387, -104.6819),
    "lordsburg":                (32.3512, -108.7087),
    "silver city":              (32.7701, -108.2803),
    "anthony":                  (32.0059, -106.6020),
    "chaparral":                (32.0612, -106.4126),
    "sunland park":             (31.7957, -106.5809),
    "north valley":             (35.1386, -106.6755),
    "south valley":             (35.0109, -106.6738),
    "west albuquerque":         (35.0844, -106.6504),
}

# State-centre fallback (Torrance County, geographic centre of NM)
_NM_DEFAULT = (34.5001, -106.0470)


def _resolve_coords(location: str) -> tuple[float, float]:
    """
    Try to match a city name from the free-text location string.
    Returns (lat, lon). Falls back to NM state centre if no city found.
    """
    if not location:
        return _NM_DEFAULT
    loc_lower = location.lower()
    # Try longest match first to avoid 'las' matching before 'las cruces'
    for city in sorted(_NM_CITIES.keys(), key=len, reverse=True):
        if city in loc_lower:
            return _NM_CITIES[city]
    return _NM_DEFAULT


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in miles between two points."""
    R = 3958.8  # Earth radius in miles
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _nearest(facilities: list[dict], farmer_lat: float, farmer_lon: float,
             n: int = 3) -> list[dict]:
    """Return the n nearest facilities that have valid lat/lon."""
    scored = []
    for f in facilities:
        try:
            flat = float(f.get("lat") or "")
            flon = float(f.get("lon") or "")
        except (ValueError, TypeError):
            continue
        dist = _haversine(farmer_lat, farmer_lon, flat, flon)
        scored.append({**f, "distance_miles": round(dist, 1)})
    scored.sort(key=lambda x: x["distance_miles"])
    return scored[:n]


def annotate_distances(resources: dict, location: str) -> dict:
    """
    Return a copy of the resources dict with every facility annotated with
    distance_miles from the farmer's location, and each list sorted nearest first.
    Facilities without lat/lon are placed at the end.
    """
    farmer_lat, farmer_lon = _resolve_coords(location)

    def _sort_list(facilities: list[dict]) -> list[dict]:
        with_dist, without = [], []
        for f in facilities:
            try:
                flat = float(f.get("lat") or "")
                flon = float(f.get("lon") or "")
                dist = round(_haversine(farmer_lat, farmer_lon, flat, flon), 1)
                with_dist.append({**f, "distance_miles": dist})
            except (ValueError, TypeError):
                without.append({**f, "distance_miles": None})
        with_dist.sort(key=lambda x: x["distance_miles"])
        return with_dist + without

    # Resolve city name for display
    resolved_city = "New Mexico"
    if location:
        loc_lower = location.lower()
        for city in sorted(_NM_CITIES.keys(), key=len, reverse=True):
            if city in loc_lower:
                resolved_city = city.title()
                break

    return {
        "buy": {
            "digestate": _sort_list(resources["buy"]["digestate"]),
            "compost":   _sort_list(resources["buy"]["compost"]),
        },
        "sell": {
            "crop_waste": _sort_list(resources["sell"]["crop_waste"]),
            "produce":    _sort_list(resources["sell"]["produce"]),
        },
        "counts": resources["counts"],
        "farmer_city": resolved_city,
        "farmer_coords": (farmer_lat, farmer_lon),
    }


# ---------------------------------------------------------------------------
# NMSU agronomic amendment rates  (all values sourced from published guides)
# ---------------------------------------------------------------------------

# Optimal OM target for NM agricultural soils (NMSU Circular CR-504 Table 1)
_OM_TARGET_PCT = 2.5

# Compost application: 2–3 tons/acre raises OM by ~1% (NMSU CR-504)
_COMPOST_TONS_PER_PCT_OM = 2.5   # midpoint of 2–3 range

# Nitrogen via compost: typical NM compost = 1.5% N (dry-weight)
# To supply 40–60 lbs N/acre (NMSU A-129 veg production baseline) → ~2 t/acre
_N_COMPOST_TONS_PER_ACRE = 2.0

# Nitrogen via AD digestate: typical digestate = 3% N (liquid, wet-weight)
# 1 ton (≈ 240 gal) supplies ~60 lbs N/acre (NMSU A-129)
_N_DIGESTATE_TONS_PER_ACRE = 1.0

# Phosphorus via bone meal: 3-15-0 NPK, ~200–400 lbs/acre for P deficit (NMSU A-129)
_P_BONEMEAL_LBS_PER_ACRE = 300   # midpoint

# pH correction with agricultural lime: 1.5 tons/acre per 1 pH unit (NMSU A-122)
_PH_LIME_TONS_PER_UNIT = 1.5

# pH correction with elemental sulfur: 300 lbs/acre per 1 pH unit (NMSU A-122)
_PH_SULFUR_LBS_PER_UNIT = 300


# ---------------------------------------------------------------------------
# Main calculator
# ---------------------------------------------------------------------------

def calculate_amendments(soil: dict, location: str = "") -> dict:
    """
    Given a soil dict (from results_json) and the farmer's location string,
    return a structured amendment plan with:
      - per-nutrient deficiency analysis
      - real NMSU agronomic quantities
      - nearest EPA NM facilities sourced from the dataset

    Args:
        soil:     dict with keys pH, nitrogen, phosphorus, organic_matter, [salinity]
        location: free-text location string (e.g. "Albuquerque, NM")

    Returns:
        {
          "deficiencies": [list of amendment objects],
          "already_ok":   [list of nutrients that need no amendment],
          "farmer_city":  str,
          "farmer_coords": (lat, lon),
        }
    """
    resources = get_nm_resources()
    farmer_lat, farmer_lon = _resolve_coords(location)

    # Identify which NM city we resolved to (for display)
    resolved_city = "New Mexico (state centre)"
    if location:
        loc_lower = location.lower()
        for city in sorted(_NM_CITIES.keys(), key=len, reverse=True):
            if city in loc_lower:
                resolved_city = city.title()
                break

    deficiencies = []
    already_ok = []

    # ------------------------------------------------------------------
    # 1. Organic Matter
    # ------------------------------------------------------------------
    try:
        om = float(str(soil.get("organic_matter", "1.0")).replace("%", "").strip())
    except ValueError:
        om = 1.0

    if om < _OM_TARGET_PCT:
        deficit_pct = round(_OM_TARGET_PCT - om, 2)
        tons_needed = round(deficit_pct * _COMPOST_TONS_PER_PCT_OM, 1)
        nearest_compost = _nearest(resources["buy"]["compost"], farmer_lat, farmer_lon)
        deficiencies.append({
            "nutrient": "Organic Matter",
            "icon": "bi-layers-fill",
            "color": "brown",
            "badge_color": "warning",
            "current": f"{om}%",
            "target": f"{_OM_TARGET_PCT}%",
            "deficit": f"{deficit_pct}%",
            "primary_amendment": "Finished compost",
            "quantity": f"{tons_needed} tons / acre",
            "quantity_note": (
                f"Raising OM from {om}% to {_OM_TARGET_PCT}% requires "
                f"~{tons_needed} tons/acre of finished compost "
                f"({_COMPOST_TONS_PER_PCT_OM} t/acre per 1% OM — NMSU Circular CR-504)"
            ),
            "reference": "NMSU Circular CR-504: Compost Use in New Mexico Agriculture",
            "facility_type": "compost",
            "nearest_facilities": nearest_compost,
        })
    else:
        already_ok.append({"nutrient": "Organic Matter", "current": f"{om}%", "icon": "bi-layers-fill"})

    # ------------------------------------------------------------------
    # 2. Nitrogen
    # ------------------------------------------------------------------
    nitrogen = str(soil.get("nitrogen", "medium")).lower()

    if nitrogen == "low":
        nearest_compost = _nearest(resources["buy"]["compost"], farmer_lat, farmer_lon)
        nearest_digestate = _nearest(resources["buy"]["digestate"], farmer_lat, farmer_lon)
        deficiencies.append({
            "nutrient": "Nitrogen",
            "icon": "bi-droplet-fill",
            "color": "info",
            "badge_color": "info",
            "current": "Low",
            "target": "Medium–High",
            "deficit": "—",
            "primary_amendment": "Compost or AD digestate",
            "quantity": (
                f"{_N_COMPOST_TONS_PER_ACRE} tons/acre compost  "
                f"OR  {_N_DIGESTATE_TONS_PER_ACRE} ton/acre digestate"
            ),
            "quantity_note": (
                f"To supply 40–60 lbs N/acre (baseline for NM vegetable production): "
                f"{_N_COMPOST_TONS_PER_ACRE} tons/acre compost (1.5% N dry-weight) "
                f"or {_N_DIGESTATE_TONS_PER_ACRE} ton/acre liquid digestate (3% N wet-weight). "
                f"Source: NMSU Extension Guide A-129."
            ),
            "reference": "NMSU Extension Guide A-129: Commercial Vegetable Production in New Mexico",
            "facility_type": "compost_and_digestate",
            "nearest_compost": nearest_compost,
            "nearest_digestate": nearest_digestate,
            "nearest_facilities": nearest_compost,  # primary list for shared display
        })
    else:
        already_ok.append({"nutrient": "Nitrogen", "current": nitrogen.title(), "icon": "bi-droplet-fill"})

    # ------------------------------------------------------------------
    # 3. Phosphorus
    # ------------------------------------------------------------------
    phosphorus = str(soil.get("phosphorus", "medium")).lower()

    if phosphorus == "low":
        # Phosphorus fix: bone meal / rock phosphate — no EPA facility needed,
        # but compost also helps so we show nearest compost as a secondary option
        nearest_compost = _nearest(resources["buy"]["compost"], farmer_lat, farmer_lon)
        deficiencies.append({
            "nutrient": "Phosphorus",
            "icon": "bi-brightness-high-fill",
            "color": "orange",
            "badge_color": "danger",
            "current": "Low",
            "target": "Medium–High",
            "deficit": "—",
            "primary_amendment": "Bone meal or rock phosphate",
            "quantity": f"{_P_BONEMEAL_LBS_PER_ACRE} lbs / acre",
            "quantity_note": (
                f"Apply {_P_BONEMEAL_LBS_PER_ACRE} lbs/acre bone meal (3-15-0 NPK) "
                f"or rock phosphate to supply 40–60 lbs P₂O₅/acre. "
                f"Compost also supplies supplemental P — nearest site shown below. "
                f"Source: NMSU Extension Guide A-129."
            ),
            "reference": "NMSU Extension Guide A-129: Commercial Vegetable Production in New Mexico",
            "facility_type": "compost",
            "nearest_facilities": nearest_compost,
        })
    else:
        already_ok.append({"nutrient": "Phosphorus", "current": phosphorus.title(), "icon": "bi-brightness-high-fill"})

    # ------------------------------------------------------------------
    # 4. pH
    # ------------------------------------------------------------------
    try:
        ph = float(str(soil.get("pH", "7.0")).strip())
    except ValueError:
        ph = 7.0

    # NM optimal range: 6.0–7.5 (NMSU A-122)
    _PH_MIN_OPTIMAL = 6.0
    _PH_MAX_OPTIMAL = 7.5

    if ph < _PH_MIN_OPTIMAL:
        deficit = round(_PH_MIN_OPTIMAL - ph, 1)
        tons = round(deficit * _PH_LIME_TONS_PER_UNIT, 1)
        deficiencies.append({
            "nutrient": "Soil pH (Too Acidic)",
            "icon": "bi-thermometer-low",
            "color": "danger",
            "badge_color": "danger",
            "current": str(ph),
            "target": f"≥ {_PH_MIN_OPTIMAL}",
            "deficit": f"{deficit} pH units",
            "primary_amendment": "Agricultural lime (calcium carbonate)",
            "quantity": f"{tons} tons / acre",
            "quantity_note": (
                f"Raise pH from {ph} to ≥{_PH_MIN_OPTIMAL} by applying "
                f"{tons} tons/acre of agricultural lime "
                f"({_PH_LIME_TONS_PER_UNIT} t/acre per 1 pH unit). "
                f"Incorporate before planting; re-test after 3 months. "
                f"Source: NMSU Circular A-122."
            ),
            "reference": "NMSU Circular A-122: Soil and Plant Tissue Testing in New Mexico",
            "facility_type": "none",
            "nearest_facilities": [],
        })
    elif ph > _PH_MAX_OPTIMAL:
        excess = round(ph - _PH_MAX_OPTIMAL, 1)
        lbs = round(excess * _PH_SULFUR_LBS_PER_UNIT)
        deficiencies.append({
            "nutrient": "Soil pH (Too Alkaline)",
            "icon": "bi-thermometer-high",
            "color": "orange",
            "badge_color": "warning",
            "current": str(ph),
            "target": f"≤ {_PH_MAX_OPTIMAL}",
            "deficit": f"{excess} pH units",
            "primary_amendment": "Elemental sulfur",
            "quantity": f"{lbs} lbs / acre",
            "quantity_note": (
                f"Lower pH from {ph} to ≤{_PH_MAX_OPTIMAL} by applying "
                f"{lbs} lbs/acre of elemental sulfur "
                f"({_PH_SULFUR_LBS_PER_UNIT} lbs/acre per 1 pH unit). "
                f"High alkalinity (> 8.0) is common in NM — organic matter addition also helps. "
                f"Source: NMSU Circular A-122."
            ),
            "reference": "NMSU Circular A-122: Soil and Plant Tissue Testing in New Mexico",
            "facility_type": "compost",
            "nearest_facilities": _nearest(resources["buy"]["compost"], farmer_lat, farmer_lon),
        })
    else:
        already_ok.append({"nutrient": "pH", "current": str(ph), "icon": "bi-thermometer-half"})

    return {
        "deficiencies": deficiencies,
        "already_ok": already_ok,
        "farmer_city": resolved_city,
        "farmer_coords": (farmer_lat, farmer_lon),
    }
