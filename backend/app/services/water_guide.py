"""
Water Guide service.

Combines four live/static data sources to produce crop-specific water
recommendations, storage method suggestions, and reuse benefits:

  1. crop_requirements.json  – irrigation_inches_per_season per crop
  2. USGS live streamflow    – water_stress score 0-100
  3. US Drought Monitor      – drought level 0-5
  4. Farmer soil test        – organic_matter % for retention quality

Conversions used:
  1 acre-inch of water = 27,154 US gallons
  1 inch of rainfall on 1,000 sq ft roof = ~623 gallons (0.623 gal/sqft/inch)
"""

import json
import os
import logging

from backend.app.services.usgs_water import get_water_status
from backend.app.services.drought import get_drought_status
from backend.app.services.noaa_rainfall import get_seasonal_rainfall
from backend.app.services.et_calculator import get_et_normals, crop_seasonal_et_inches
from backend.app.services.bor_reservoir import get_reservoir_status

logger = logging.getLogger(__name__)

_CROP_DATA_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "static", "crop_requirements.json"
)

_GALLONS_PER_ACRE_INCH = 27_154
_RAINFALL_FALLBACK_IN  = 7.5   # used when NOAA is unavailable


def _load_crops() -> list:
    path = os.path.abspath(_CROP_DATA_PATH)
    with open(path, encoding="utf-8") as f:
        return json.load(f)["crops"]


def _soil_retention_tier(om_pct: float) -> dict:
    """
    Return soil water-retention quality based on organic matter %.
    Low OM = poor retention = more evaporation, more frequent watering needed.
    """
    if om_pct >= 3.0:
        return {
            "tier": "Good",
            "color": "success",
            "icon": "bi-droplet-fill",
            "desc": "High organic matter holds water well. You can irrigate less frequently with deeper passes.",
            "frequency_tip": "Water every 5–7 days per growth stage.",
            "adjustment_pct": -10,   # 10% less water needed vs baseline
        }
    elif om_pct >= 1.5:
        return {
            "tier": "Moderate",
            "color": "warning",
            "icon": "bi-droplet-half",
            "desc": "Moderate organic matter. Soil retains water adequately but will benefit from mulching.",
            "frequency_tip": "Water every 3–5 days; mulch surface to cut evaporation.",
            "adjustment_pct": 0,
        }
    else:
        return {
            "tier": "Poor",
            "color": "danger",
            "icon": "bi-droplet",
            "desc": "Low organic matter means water drains or evaporates quickly. Add compost to improve retention.",
            "frequency_tip": "Water every 2–3 days in small amounts; drip irrigation strongly advised.",
            "adjustment_pct": +15,   # 15% more water needed
        }


def _stress_adjustment(stress_score: int) -> dict:
    """Return a multiplier and advisory based on current USGS water stress."""
    if stress_score <= 25:
        return {"label": "Low Stress", "color": "success",
                "icon": "bi-water", "pct": 0,
                "tip": "River flow is normal. Standard irrigation schedule applies."}
    elif stress_score <= 50:
        return {"label": "Moderate Stress", "color": "info",
                "icon": "bi-cloud-drizzle", "pct": +5,
                "tip": "Flow is slightly below normal. Consider switching to drip irrigation to reduce usage by 30–50%."}
    elif stress_score <= 75:
        return {"label": "High Stress", "color": "warning",
                "icon": "bi-exclamation-triangle", "pct": +10,
                "tip": "Water availability is below normal. Prioritise drought-tolerant crops; schedule irrigation for early morning."}
    else:
        return {"label": "Critical Stress", "color": "danger",
                "icon": "bi-fire", "pct": +20,
                "tip": "Critically low flow. Use stored/harvested water, apply mulch, and avoid high-water crops this season."}


def _drought_advisory(level: int) -> dict | None:
    if level == 0:
        return None
    advisories = {
        1: ("D0 — Abnormally Dry", "info",
            "Conditions are drier than normal. Begin conserving irrigation water now."),
        2: ("D1 — Moderate Drought", "warning",
            "Moderate drought in effect. Reduce irrigated area or switch to low-water crops."),
        3: ("D2 — Severe Drought", "danger",
            "Severe drought. Prioritise stored water for high-value crops only. Fallowing low-margin fields is advisable."),
        4: ("D3 — Extreme Drought", "danger",
            "Extreme drought. Emergency water conservation measures apply. Contact NRCS for emergency assistance."),
        5: ("D4 — Exceptional Drought", "danger",
            "Exceptional drought — worst category. All stored water reserves should be prioritised for perennial crops."),
    }
    label, color, text = advisories[level]
    return {"label": label, "color": color, "text": text}


def _build_crop_water_table(
    crops: list,
    om_pct: float,
    stress_score: int,
    seasonal_rain_in: float,
    et_normals: dict,
) -> list:
    """
    Return per-crop water guidance rows.
    """
    retention = _soil_retention_tier(om_pct)
    stress_adj = _stress_adjustment(stress_score)
    rows = []
    for crop in crops:
        # Base irrigation: ET×Kc calculated, or static NMSU fallback
        base_in = crop_seasonal_et_inches(crop, et_normals)
        need    = crop["water"]["need"]
        tol     = crop["water"]["drought_tolerance"]

        # Apply soil-retention and stress adjustments
        total_adj = retention["adjustment_pct"] + stress_adj["pct"]
        adjusted_in = round(base_in * (1 + total_adj / 100), 1)

        # Net irrigation = adjusted requirement minus what rainfall provides
        net_in = max(0, round(adjusted_in - seasonal_rain_in, 1))

        # Gallons per acre
        net_gal = round(net_in * _GALLONS_PER_ACRE_INCH)

        # Best irrigation method given stress + need
        if stress_score >= 60 or need == "high":
            method = "Drip / subsurface"
            method_icon = "bi-vinyl-fill"
            method_color = "success"
        elif need == "medium":
            method = "Drip or furrow"
            method_icon = "bi-reception-4"
            method_color = "primary"
        else:
            method = "Furrow / sprinkler"
            method_icon = "bi-cloud-rain"
            method_color = "info"

        # Urgency badge
        if tol == "low" and stress_score >= 60:
            urgency = "High Risk"
            urgency_color = "danger"
        elif tol == "high":
            urgency = "Drought-Tolerant"
            urgency_color = "success"
        else:
            urgency = "Moderate"
            urgency_color = "warning"

        rows.append({
            "id":            crop["id"],
            "name":          crop["name"],
            "emoji":         crop["emoji"],
            "need":          need.capitalize(),
            "base_in":       base_in,
            "adjusted_in":   adjusted_in,
            "net_in":        net_in,
            "net_gal":       f"{net_gal:,}",
            "method":        method,
            "method_icon":   method_icon,
            "method_color":  method_color,
            "drought_tol":   tol.capitalize(),
            "urgency":       urgency,
            "urgency_color": urgency_color,
        })

    # Sort: drought-tolerant first when under stress, else by water need asc
    if stress_score >= 50:
        order = {"high": 0, "medium": 1, "low": 2}
        rows.sort(key=lambda r: order.get(r["drought_tol"].lower(), 1))
    else:
        order = {"Low": 0, "Medium": 1, "High": 2}
        rows.sort(key=lambda r: order.get(r["need"], 1))

    return rows


# ── Storage methods ────────────────────────────────────────────────────────

_STORAGE_METHODS = [
    {
        "id": "rainwater_cistern",
        "title": "Rooftop Rainwater Cistern",
        "icon": "bi-house-fill",
        "color": "primary",
        "desc": (
            "Collect rainfall from barn or greenhouse roofs into above-ground or "
            "buried tanks. At NM's ~12 in/yr average, a 2,000 sq-ft roof yields "
            "≈14,900 gallons per year — enough for a half-acre of Chile Pepper."
        ),
        "capacity_range": "500 – 10,000 gallons",
        "install_cost": "$200 – $2,500",
        "nm_legal": True,
        "nm_note": "NM law (§72-12-1.2) allows unlimited rainwater collection for use on the same property.",
        "best_for": ["low", "medium"],   # water-need tiers this suits best
        "steps": [
            "Install gutters with first-flush diverters to exclude roof debris.",
            "Connect downspout to a sealed polyethylene or fiberglass tank.",
            "Add a fine-mesh screen inlet to keep out insects and sediment.",
            "Install a float valve and overflow pipe directed to a swale.",
            "Use a small 12V pump or gravity feed to drip lines.",
        ],
    },
    {
        "id": "farm_pond",
        "title": "Earthen Farm Pond / Stock Tank",
        "icon": "bi-water",
        "color": "info",
        "desc": (
            "Excavate a small lined or unlined pond in a low-lying area to capture "
            "runoff from fields and access roads. A 0.25-acre pond (1 ft deep) holds "
            "≈81,000 gallons. Clay-lined ponds lose 15–30% to seepage; HDPE liners "
            "reduce loss to <5%."
        ),
        "capacity_range": "50,000 – 500,000 gallons",
        "install_cost": "$3,000 – $20,000",
        "nm_legal": True,
        "nm_note": "Ponds capturing only on-farm runoff (not diverting a stream) are generally exempt from NM water rights permits.",
        "best_for": ["medium", "high"],
        "steps": [
            "Select a low point that naturally collects runoff; avoid flood plains.",
            "Design spillway 1 ft below top of berm to handle 100-year storm.",
            "Line with bentonite clay or 20-mil HDPE liner if soils are sandy.",
            "Add aeration if storing water > 2 weeks to prevent algae.",
            "Install a screened intake pipe for gravity-fed drip/sprinkler systems.",
        ],
    },
    {
        "id": "acequia",
        "title": "Acequia / Irrigation Canal",
        "icon": "bi-arrows-expand-vertical",
        "color": "success",
        "desc": (
            "Traditional NM gravity-fed earthen channels dating to Spanish colonial "
            "times. Acequias distribute surface water from rivers or snowmelt across "
            "multiple farms at near-zero energy cost. Joining an established acequia "
            "association gives access to adjudicated water rights."
        ),
        "capacity_range": "Continuous flow (shared)",
        "install_cost": "Membership fee + labor (varies by association)",
        "nm_legal": True,
        "nm_note": "Acequia associations are recognised legal entities in NM. Contact NM Acequia Association (nmacequia.org) for membership.",
        "best_for": ["medium", "high"],
        "steps": [
            "Contact the NM Acequia Association to find your local parciante.",
            "File a water right transfer or new-use application with NMOSE if needed.",
            "Clean and line your portion of the lateral each spring (community work).",
            "Use check-gates to control flow to individual furrows or borders.",
            "Install a head gate meter to track usage against your water right.",
        ],
    },
    {
        "id": "drip_reuse",
        "title": "Drip Irrigation + Return-Flow Capture",
        "icon": "bi-vinyl-fill",
        "color": "warning",
        "desc": (
            "Drip/subsurface drip systems cut water use by 30–50% vs flood "
            "irrigation. Tail-water pits at the end of fields capture runoff "
            "that would otherwise be lost, and pump it back to the head of the "
            "field for re-use."
        ),
        "capacity_range": "Saves 8–24 acre-inches per acre per season",
        "install_cost": "$500 – $1,500 per acre (EQIP cost-share available)",
        "nm_legal": True,
        "nm_note": "USDA EQIP (Environmental Quality Incentives Program) offers 50–75% cost-share for drip conversion in NM.",
        "best_for": ["low", "medium", "high"],
        "steps": [
            "Design emitter spacing for your crop row width (typ. 12–18 in).",
            "Install a disc or sand filter + pressure regulator at the head.",
            "Lay drip tape at 4–8 in depth for subsurface (SDI) or surface.",
            "Dig a tail-water pit (100–500 gal) at the low end of each field.",
            "Connect pit pump on a float switch to return water to head ditch.",
        ],
    },
    {
        "id": "cover_crop_mulch",
        "title": "Cover Crops & Mulching",
        "icon": "bi-tree",
        "color": "success",
        "desc": (
            "Organic mulch (straw, wood chips, cardboard) applied 2–4 inches deep "
            "reduces soil evaporation by up to 70%. Winter cover crops (rye, vetch) "
            "add organic matter and lock residual soil moisture through spring."
        ),
        "capacity_range": "Saves 2–6 acre-inches per season",
        "install_cost": "$50 – $300 per acre",
        "nm_legal": True,
        "nm_note": "NRCS RCPP and CSP programs in NM offer payments for cover-crop adoption.",
        "best_for": ["low", "medium", "high"],
        "steps": [
            "Apply 3-in straw or wood-chip mulch around crop rows immediately after transplant.",
            "Plant rye or hairy vetch as a winter cover between October and November.",
            "Terminate cover crop 2–3 weeks before spring planting (mow or roll).",
            "Incorporate terminated biomass shallowly to build organic matter.",
            "Repeat each season — each 1% OM increase stores ~20,000 gal/acre.",
        ],
    },
]


# ── Reuse benefits ─────────────────────────────────────────────────────────

_REUSE_BENEFITS = [
    {
        "icon": "bi-cash-coin",
        "color": "success",
        "title": "Lower Pumping Costs",
        "stat": "30–50%",
        "stat_label": "reduction in groundwater pumping",
        "detail": (
            "Harvested or recycled water costs only the energy to move it vs. "
            "deep-well pumping at $0.50–$2.00 per 1,000 gallons in NM. A 10-acre "
            "farm using stored water can save $800–$3,000 per season."
        ),
    },
    {
        "icon": "bi-cloud-rain-heavy",
        "color": "primary",
        "title": "Drought Insurance",
        "stat": "60–90 days",
        "stat_label": "of supplemental supply from a 0.25-acre pond",
        "detail": (
            "A modest 80,000-gallon pond can sustain 5 acres of Chile Pepper for "
            "two full dry months — bridging the gap between monsoon seasons "
            "without relying on the over-allocated Rio Grande."
        ),
    },
    {
        "icon": "bi-arrow-repeat",
        "color": "info",
        "title": "Groundwater Recharge",
        "stat": "20–40%",
        "stat_label": "of pond seepage reaches the aquifer",
        "detail": (
            "Unlined ponds and earthworks deliberately allow seepage to percolate "
            "through the vadose zone, recharging shallow alluvial aquifers that "
            "feed local wells — a community benefit recognised by NMOSE."
        ),
    },
    {
        "icon": "bi-shield-check",
        "color": "warning",
        "title": "Reduced Soil Erosion",
        "stat": "Up to 80%",
        "stat_label": "less runoff erosion with cover crops + ponds",
        "detail": (
            "Capturing storm runoff in farm ponds or swales prevents the flash-flood "
            "erosion that strips NM topsoil. USDA estimates 1 inch of lost topsoil "
            "equals 5–10 years of reduced productivity."
        ),
    },
    {
        "icon": "bi-thermometer-sun",
        "color": "danger",
        "title": "Microclimate Cooling",
        "stat": "2–5 °F",
        "stat_label": "cooler air temperature near stored water",
        "detail": (
            "Open water bodies and well-mulched fields evapotranspire moisture that "
            "cools the surrounding air — reducing heat stress on crops and "
            "extending the growing window in NM's intense summer sun."
        ),
    },
    {
        "icon": "bi-flower2",
        "color": "success",
        "title": "Higher Organic Matter Over Time",
        "stat": "Each +1% OM",
        "stat_label": "stores ~20,000 extra gallons per acre",
        "detail": (
            "The best long-term water storage is your own soil. Compost, cover "
            "crops, and reduced tillage build organic matter — each percentage "
            "point increase adds roughly 20,000 gallons of plant-available water "
            "per acre, per year."
        ),
    },
]


# ── Public API ─────────────────────────────────────────────────────────────

def get_water_guide(soil: dict | None = None, location: str | None = None) -> dict:
    """
    soil:     dict with keys pH, organic_matter, nitrogen, phosphorus (from latest test).
              Pass None if no test exists yet.
    location: farmer's location string (e.g. "Albuquerque, NM") for NOAA rainfall lookup.

    Returns a complete guide dict for the template.
    """
    # Live data
    try:
        water = get_water_status()
        stress_score = water.get("stress_score", 50)
        stress_status = water.get("status", "Unknown")
        flow_cfs = water.get("flow_cfs")
        trend = water.get("trend", [])
    except Exception as exc:
        logger.warning("USGS water fetch failed: %s", exc)
        stress_score, stress_status, flow_cfs, trend = 50, "Unavailable", None, []

    try:
        drought = get_drought_status()
        drought_level = drought.get("level", 0)
        drought_label = drought.get("label", "Unknown")
        drought_color = drought.get("color", "#4caf50")
    except Exception as exc:
        logger.warning("Drought fetch failed: %s", exc)
        drought_level, drought_label, drought_color = 0, "Unavailable", "#9e9e9e"

    # Soil data
    om_pct = 1.0   # default
    if soil:
        try:
            raw = str(soil.get("organic_matter", "1.0")).replace("%", "")
            om_pct = float(raw)
        except (ValueError, TypeError):
            pass

    retention = _soil_retention_tier(om_pct)
    stress_adj = _stress_adjustment(stress_score)
    drought_adv = _drought_advisory(drought_level)

    # Live seasonal rainfall for this farmer's location
    try:
        rainfall_data = get_seasonal_rainfall(location)
    except Exception as exc:
        logger.warning("NOAA rainfall fetch failed: %s", exc)
        rainfall_data = {
            "inches": _RAINFALL_FALLBACK_IN,
            "city": "New Mexico",
            "station_id": None,
            "station_name": None,
            "source": "fallback",
        }
    seasonal_rain_in = rainfall_data["inches"]

    # ET normals for Penman-Monteith / Hargreaves-Samani crop calculations
    try:
        et_normals = get_et_normals(location)
    except Exception as exc:
        logger.warning("ET normals fetch failed: %s", exc)
        et_normals = {"eto_monthly": {}, "source": "fallback", "method": "fallback", "station_id": None}

    crops = _load_crops()
    crop_table = _build_crop_water_table(crops, om_pct, stress_score, seasonal_rain_in, et_normals)

    # Seasonal rainfall offset display
    rainfall_offset = f"{seasonal_rain_in} in ({round(seasonal_rain_in * _GALLONS_PER_ACRE_INCH / 1000)}k gal/acre)"

    # Elephant Butte reservoir status (BOR RISE API)
    try:
        reservoir = get_reservoir_status()
    except Exception as exc:
        logger.warning("BOR reservoir fetch failed: %s", exc)
        from backend.app.services.bor_reservoir import _unavailable_payload
        reservoir = _unavailable_payload()

    return {
        # Live signals
        "stress_score":   stress_score,
        "stress_status":  stress_status,
        "stress_adj":     stress_adj,
        "flow_cfs":       flow_cfs,
        "trend":          trend,
        "drought_level":  drought_level,
        "drought_label":  drought_label,
        "drought_color":  drought_color,
        "drought_adv":    drought_adv,
        # Soil
        "om_pct":         om_pct,
        "retention":      retention,
        "has_soil":       soil is not None,
        # Guidance tables
        "crop_table":      crop_table,
        "rainfall_offset": rainfall_offset,
        "rainfall_data":   rainfall_data,
        "et_method":       et_normals.get("method", "fallback"),
        # Reservoir
        "reservoir":       reservoir,
        # Storage and reuse
        "storage_methods": _STORAGE_METHODS,
        "reuse_benefits":  _REUSE_BENEFITS,
    }
