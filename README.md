# CropPulse 🌱

**Farm smarter. Grow better. Built for New Mexico.**

CropPulse is a farmer intelligence platform that turns raw soil data and live government API feeds into actionable planting, watering, and amendment decisions — built for the 24,000 small farms across New Mexico.

---

## The Problem

New Mexico's small farmers make critical decisions — what to plant, how much water they'll need, which soil nutrients are missing — with no real-time data. Wrong decisions mean lost harvests and lost income they can't recover.

---

## The Solution

Enter your soil test results once. CropPulse tells you:
- **What to grow** — ranked crop matches based on your soil, local water stress, and live drought conditions
- **How much water you need** — precise gallon estimates per acre, per crop, adjusted for your soil's organic matter
- **What your soil is missing** — amendment recommendations with the nearest NM suppliers to fix it
- **Where to get help** — real NM soil testing labs, extension offices, and agricultural resources

---

## Live Data Sources

CropPulse pulls from **five federal APIs** in real time:

| API | What we use it for |
|---|---|
| **USGS National Water Information System** | Live streamflow → water stress score (0–100) |
| **US Drought Monitor (USDM)** | Current drought level (D0–D4) for NM counties |
| **NOAA Climate Data Online** | Seasonal rainfall totals for water budget calculations |
| **USDA NASS** | Crop price data and market alerts |
| **Bureau of Reclamation** | NM reservoir storage status |

Every service has a graceful fallback — if an API is unreachable, the app uses conservative NM-specific defaults and keeps running.

---

## App Flow

```
Register / Login
      │
      ▼
  Dashboard
  ├── Soil Test Request → Select lab → Submit results → Status tracking
  ├── Crop Match Results ← soil pH, N, P, OM + USGS water stress + drought level
  │        └── Crop Suggestions → per-crop match score, water risk, economics/acre
  │                  └── Amendment Calculator → nutrient gaps + nearest NM facilities
  ├── Water Guide → seasonal gallons/acre per crop + storage method recommendations
  ├── Soil Resources → NM extension labs, EPA resources, guides
  └── My Facility Contacts → track supplier connections and closed deals
```

---

## Key Features

### Crop Match Engine
Combines three live signals:
1. Your soil test (pH, nitrogen, phosphorus, organic matter)
2. USGS live streamflow water stress score
3. US Drought Monitor drought level

Returns a ranked list of crops with a match score, soil gap analysis, water risk flag, and NASS price-per-acre economics.

### Water Guide
Calculates crop-specific irrigation needs using:
- Crop base irrigation requirements
- ET (evapotranspiration) normals from NOAA
- Seasonal rainfall offset
- Soil organic matter retention adjustment
- Current drought severity multiplier

Output: total gallons needed, gallons saved via rainwater harvesting, recommended storage methods.

### Amendment Calculator
- Diagnoses which nutrients (N, P, K, pH) your soil is deficient in
- Recommends specific amendments (compost, lime, sulfur, phosphate)
- Finds the nearest New Mexico agricultural facilities by driving distance
- Links directly to the CropPulse facility referral network

### Facility Network
- Farmers browse and contact NM agricultural suppliers via CropPulse
- Track deal status (In Progress → Deal Closed)
- Log deal value when a deal closes

---

## Business Model

| Stream | Description |
|---|---|
| **CropPulse Pro** ($9/month) | Unlocks Water Guide, Amendment Calculator, and Facility Network |
| **Facility Listing Fees** | Suppliers pay a monthly fee to appear in the network |
| **Referral Fees** | Commission on deals closed through the CropPulse network |
| **Data Licensing** | Anonymized soil/crop/water data sold to insurers, lenders, and state agencies |

**Year 1 target:** 500 Pro farmers × $9 × 12 = **$54K ARR** + facility fees = **$150K+**

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, Flask 3.1.3 |
| Database | SQLite (via SQLAlchemy + Flask-Migrate) |
| Auth | Flask-Login, Werkzeug password hashing |
| Frontend | Bootstrap 5.3.3, Bootstrap Icons 1.11.3 |
| Containerization | Docker + Docker Compose |
| Data | USGS, NOAA, USDM, NASS, BOR APIs |

---

## Getting Started

### Local (Python)
```bash
cd backend
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
python run.py
```
App runs at `http://127.0.0.1:5000`

### Docker
```bash
cp backend/.env.example backend/.env
# Add your API keys to backend/.env
docker-compose up --build
```
App runs at `http://localhost:5000`

---

## Environment Variables

Copy `backend/.env.example` to `backend/.env` and fill in:

```
NASS_API_KEY=your_nass_api_key_here
NOAA_CDO_TOKEN=your_noaa_token_here
```

Both keys are free:
- NASS: https://quickstats.nass.usda.gov/api
- NOAA CDO: https://www.ncdc.noaa.gov/cdo-web/token

---

## Project Structure

```
DesertDev_Hackathon26/
├── backend/
│   ├── app/
│   │   ├── models.py              # User, SoilTestRequest, FacilityReferral
│   │   ├── routes/
│   │   │   ├── farmer.py          # All farmer-facing routes
│   │   │   ├── auth.py            # Register / login / logout
│   │   │   └── partner.py         # Facility/partner portal
│   │   └── services/
│   │       ├── crop_engine.py     # Crop match scoring engine
│   │       ├── water_guide.py     # Water budget calculator
│   │       ├── amendment_calc.py  # Soil amendment recommender
│   │       ├── usgs_water.py      # USGS streamflow API
│   │       ├── drought.py         # US Drought Monitor API
│   │       ├── noaa_rainfall.py   # NOAA rainfall API
│   │       ├── et_calculator.py   # Evapotranspiration normals
│   │       ├── bor_reservoir.py   # Bureau of Reclamation API
│   │       ├── nass_economics.py  # USDA NASS crop prices
│   │       └── epa_resources.py   # EPA NM resources
│   ├── Dockerfile
│   ├── requirements.txt
│   └── run.py
├── frontend/
│   └── app/
│       ├── static/
│       │   ├── css/style.css
│       │   └── img/logo.svg
│       └── templates/
│           ├── base.html
│           └── farmer/
│               ├── dashboard.html
│               ├── crop_matches.html
│               ├── crop_suggestions.html
│               ├── amendments.html
│               ├── water_guide.html
│               ├── soil_test.html
│               ├── soil_resources.html
│               └── transactions.html
├── docker-compose.yml
└── README.md
```

---

## Built At

**DesertDev Hackathon 2026** — Albuquerque, NM
