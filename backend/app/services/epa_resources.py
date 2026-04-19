"""
EPA Excess Food Opportunities Map — NM soil resource loader.

Returns two top-level action buckets so the UI can split clearly:

BUY / SOURCE amendments (improve your soil)
--------------------------------------------
  buy.digestate  — anaerobic digestion facilities → source digestate (high N)
  buy.compost    — composting sites → purchase / collect finished compost (OM, NPK)

SELL / DONATE excess (manage your waste)
-----------------------------------------
  sell.crop_waste — composting sites that accept crop residue / food waste
  sell.produce    — food banks / pantries accepting surplus produce donations
"""

import os
import openpyxl

_BASE = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "data", "static",
    "ExcessFoodPublic_USTer_2024_R9", "ExcelTables",
)
_BASE = os.path.normpath(_BASE)

# ── cache so we only parse once per process ──────────────────────────────────
_cache: dict | None = None


def _clean(v) -> str:
    """Strip whitespace / NBSP from a cell value, return '' if None."""
    if v is None:
        return ""
    return str(v).replace("\xa0", " ").strip()


def _load_sheet(fname: str, state: str = "NM") -> list[dict]:
    path = os.path.join(_BASE, fname)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["Data"]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    headers = [_clean(h) for h in rows[0]]
    state_idx = next((i for i, h in enumerate(headers) if h.lower() == "state"), None)
    result = []
    for row in rows[1:]:
        if state_idx is None or _clean(row[state_idx]) != state:
            continue
        result.append({headers[i]: _clean(v) for i, v in enumerate(row)})
    return result


def _categorise_composting(rows: list[dict]) -> tuple[list, list]:
    """Split composting rows into food-waste-accepting vs general organic."""
    food, organic = [], []
    for r in rows:
        entry = {
            "name": r.get("Name", ""),
            "address": r.get("Address", ""),
            "city": r.get("City", ""),
            "county": r.get("County", ""),
            "zip": r.get("Zip Code", ""),
            "phone": r.get("Phone", ""),
            "website": r.get("Website", ""),
            "feedstock": r.get("Feedstock", ""),
            "food_waste_accepted": r.get("Food Waste Accepted?", "").lower() == "yes",
            "lat": r.get("Latitude", ""),
            "lon": r.get("Longitude", ""),
        }
        if entry["food_waste_accepted"]:
            food.append(entry)
        else:
            organic.append(entry)
    return food, organic


def _format_ad(rows: list[dict]) -> list[dict]:
    return [
        {
            "name": r.get("Name", ""),
            "facility_type": r.get("Facility Type", ""),
            "address": r.get("Address", ""),
            "city": r.get("City", ""),
            "county": r.get("County", ""),
            "zip": r.get("Zip Code", ""),
            "feedstock": r.get("Feedstock", ""),
            "food_waste_accepted": r.get("Food Waste Accepted?", "").lower() == "yes",
            "lat": r.get("Latitude", ""),
            "lon": r.get("Longitude", ""),
        }
        for r in rows
    ]


def _format_foodbank(rows: list[dict]) -> list[dict]:
    return [
        {
            "name": r.get("Name", ""),
            "type": r.get("Type", ""),
            "address": r.get("Address", ""),
            "city": r.get("City", ""),
            "zip": r.get("Zip Code", ""),
            "website": r.get("Website", ""),
            "accepts_donations": r.get("Accepts Food Donations?", "").lower() == "yes",
            "lat": r.get("Latitude", ""),
            "lon": r.get("Longitude", ""),
        }
        for r in rows
    ]


def get_nm_resources() -> dict:
    """Return NM soil resources split into buy (source) and sell (donate) buckets."""
    global _cache
    if _cache is not None:
        return _cache

    compost_rows = _load_sheet("CompostingFacilities.xlsx")
    compost_food, compost_organic = _categorise_composting(compost_rows)

    ad_rows = _load_sheet("AnaerobicDigestionFacilities.xlsx")
    ad_entries = _format_ad(ad_rows)

    fb_rows = _load_sheet("FoodBanksPantriesSoupKitchens.xlsx")
    fb_entries = _format_foodbank(fb_rows)

    # All composting sites are potential sources of finished compost (buy)
    all_compost = compost_food + compost_organic

    _cache = {
        # ── What a farmer can GO GET to improve their soil ──────────────────
        "buy": {
            "digestate": ad_entries,           # AD facilities → digestate (high N)
            "compost": all_compost,            # composting sites → finished compost (OM)
        },
        # ── Where a farmer can TAKE excess material ──────────────────────────
        "sell": {
            "crop_waste": compost_food,        # accept food/crop waste drop-offs
            "produce": fb_entries,             # donate surplus produce
        },
        "counts": {
            "digestate": len(ad_entries),
            "compost": len(all_compost),
            "crop_waste": len(compost_food),
            "produce": len(fb_entries),
        },
    }
    return _cache
