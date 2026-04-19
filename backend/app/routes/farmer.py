import json
from flask import Blueprint, render_template, jsonify, request, redirect, url_for, flash
from flask_login import login_required, current_user
from werkzeug.security import check_password_hash
from backend.app import db
from backend.app.models import User, SoilTestRequest, FacilityReferral
from backend.app.services.crop_engine import suggest_crops
from backend.app.services.epa_resources import get_nm_resources
from backend.app.services.amendment_calc import calculate_amendments, annotate_distances
from backend.app.services.nass_price_alert import get_price_alerts
from backend.app.services.water_guide import get_water_guide

farmer_bp = Blueprint("farmer", __name__)

# Real NM soil test centres (free or low-cost)
SOIL_TEST_CENTRES = [
    {
        "id": "nmsu_extension",
        "name": "NMSU Cooperative Extension Service",
        "address": "Bernalillo County — 1510 Menaul Blvd NW, Albuquerque, NM 87107",
        "phone": "(505) 243-1386",
        "url": "https://aces.nmsu.edu/ces/",
        "cost": "Free",
        "price_usd": 0,
        "turnaround": "2–3 weeks",
        "tests": ["pH", "Nitrogen", "Phosphorus", "Potassium", "Organic Matter"],
    },
    {
        "id": "nmda_lab",
        "name": "NM Department of Agriculture Lab",
        "address": "945 College Ave, Las Cruces, NM 88003",
        "phone": "(575) 646-3317",
        "url": "https://www.nmda.nmsu.edu/",
        "cost": "$10–$25",
        "price_usd": 18,
        "turnaround": "1–2 weeks",
        "tests": ["pH", "Nitrogen", "Phosphorus", "Potassium", "Organic Matter", "Salinity"],
    },
    {
        "id": "bernalillo_extension",
        "name": "Bernalillo County Extension Office",
        "address": "1510 Menaul Blvd NW, Albuquerque, NM 87107",
        "phone": "(505) 243-1386",
        "url": "https://aces.nmsu.edu/ces/bernalillo/",
        "cost": "Free",
        "price_usd": 0,
        "turnaround": "2–4 weeks",
        "tests": ["pH", "Nitrogen", "Phosphorus", "Organic Matter"],
    },
    {
        "id": "sandoval_extension",
        "name": "Sandoval County Extension Office",
        "address": "711 Camino del Pueblo, Bernalillo, NM 87004",
        "phone": "(505) 867-2582",
        "url": "https://aces.nmsu.edu/ces/sandoval/",
        "cost": "Free",
        "price_usd": 0,
        "turnaround": "2–4 weeks",
        "tests": ["pH", "Nitrogen", "Phosphorus", "Organic Matter"],
    },
    {
        "id": "valencia_extension",
        "name": "Valencia County Extension Office",
        "address": "404 River Rd, Belen, NM 87002",
        "phone": "(505) 565-3002",
        "url": "https://aces.nmsu.edu/ces/valencia/",
        "cost": "Free",
        "price_usd": 0,
        "turnaround": "2–4 weeks",
        "tests": ["pH", "Nitrogen", "Phosphorus", "Organic Matter"],
    },
]


@farmer_bp.route("/dashboard")
@login_required
def dashboard():
    tests = SoilTestRequest.query.filter_by(farmer_id=current_user.id).order_by(SoilTestRequest.created_at.desc()).all()
    completed = [t for t in tests if t.status == "completed"]

    # ── Feature 3: Soil health trend ─────────────────────────────────────
    _level = {"low": 1, "medium": 2, "high": 3}
    trend_points = []
    for t in reversed(completed):          # chronological order
        try:
            r = json.loads(t.results_json or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        trend_points.append({
            "date":    t.created_at.strftime("%b %d, %Y"),
            "pH":      float(r.get("pH") or 0),
            "om":      float(r.get("organic_matter") or 0),
            "N":       _level.get(str(r.get("nitrogen", "")).lower(), 0),
            "P":       _level.get(str(r.get("phosphorus", "")).lower(), 0),
        })

    # Build human-readable trend summary (latest vs previous)
    trend_summary = []
    if len(trend_points) >= 2:
        cur, prev = trend_points[-1], trend_points[-2]
        for key, label, fmt in [("pH", "pH", ".1f"), ("om", "Organic Matter", ".1f"), ("N", "Nitrogen", "d"), ("P", "Phosphorus", "d")]:
            delta = cur[key] - prev[key]
            if abs(delta) > 0.05:
                sign  = "+" if delta > 0 else ""
                pct   = round(abs(delta) / max(prev[key], 0.001) * 100)
                arrow = "▲" if delta > 0 else "▼"
                trend_summary.append({"label": label, "arrow": arrow, "pct": pct, "delta": delta, "good": delta > 0})

    # ── Feature 4: Crop price alerts ─────────────────────────────────────
    # Loaded asynchronously via /farmer/api/price-alerts — nothing to do here.

    # ── Referral summary for dashboard ───────────────────────────────────
    referral_count = FacilityReferral.query.filter_by(farmer_id=current_user.id).count()
    commission_earned = db.session.query(
        db.func.sum(FacilityReferral.commission_usd)
    ).filter_by(farmer_id=current_user.id, status="deal_closed").scalar() or 0.0

    return render_template("farmer/dashboard.html",
                           tests=tests,
                           completed=completed,
                           trend_points=json.dumps(trend_points),
                           trend_summary=trend_summary,
                           referral_count=referral_count,
                           commission_earned=commission_earned)


@farmer_bp.route("/api/price-alerts")
@login_required
def api_price_alerts():
    """Return NASS price alerts as JSON (called asynchronously by the dashboard)."""
    try:
        alerts = get_price_alerts()
    except Exception:
        alerts = []
    return jsonify(alerts)


# ── Water Guide ─────────────────────────────────────────────────────────────

@farmer_bp.route("/water-guide")
@login_required
def water_guide():
    if not current_user.is_subscribed:
        flash("The Water Guide is a Pro feature. Upgrade to unlock it.", "warning")
        return redirect(url_for("farmer.upgrade"))
    """
    Water usage guide: per-crop irrigation amounts, storage methods, reuse benefits.
    Uses the farmer's most recent completed soil test (for organic matter / retention),
    live USGS streamflow, and US Drought Monitor data.
    """
    # Get latest completed test for soil-based retention advice
    latest = (
        SoilTestRequest.query
        .filter_by(farmer_id=current_user.id, status="completed")
        .order_by(SoilTestRequest.created_at.desc())
        .first()
    )
    soil = None
    if latest and latest.results_json:
        try:
            soil = json.loads(latest.results_json)
        except (json.JSONDecodeError, TypeError):
            soil = None

    guide = get_water_guide(soil, location=current_user.location)
    return render_template("farmer/water_guide.html", guide=guide, has_soil_test=soil is not None)

@farmer_bp.route("/soil-test")
@login_required
def soil_test():
    tests = SoilTestRequest.query.filter_by(farmer_id=current_user.id).order_by(SoilTestRequest.created_at.desc()).all()
    return render_template("farmer/soil_test.html", centres=SOIL_TEST_CENTRES, tests=tests)


@farmer_bp.route("/soil-test/book", methods=["POST"])
@login_required
def book_soil_test():
    centre_id = request.form.get("centre_id", "").strip()
    location  = request.form.get("location", "").strip()

    centre = next((c for c in SOIL_TEST_CENTRES if c["id"] == centre_id), None)
    if not centre:
        flash("Invalid test centre selected.", "danger")
        return redirect(url_for("farmer.soil_test"))

    existing = SoilTestRequest.query.filter_by(farmer_id=current_user.id).count()
    free_limit = 5 if current_user.is_subscribed else 1

    if existing >= free_limit:
        price = centre["price_usd"]
        cost_str = f"${price}" if price > 0 else centre["cost"]
        plan_str = "Pro plan" if current_user.is_subscribed else "Free plan"
        flash(
            f"{plan_str} includes {free_limit} free soil test{'s' if free_limit > 1 else ''}. "
            f"Additional tests at {centre['name']} cost {cost_str} — "
            f"please book directly with the lab to proceed.",
            "info"
        )
        if not current_user.is_subscribed:
            flash("Upgrade to Pro to get 5 free soil tests before lab fees apply.", "warning")
            return redirect(url_for("farmer.upgrade"))
        return redirect(url_for("farmer.soil_test"))

    test = SoilTestRequest(
        farmer_id=current_user.id,
        location=location or current_user.location,
        test_center=centre["name"],
        status="pending",
    )
    db.session.add(test)
    db.session.commit()
    flash(f"Soil test booked at {centre['name']}. We'll notify you when results are ready.", "success")
    return redirect(url_for("farmer.soil_test"))


# ── Soil test: enter results (simulates lab returning results) ──────────────

@farmer_bp.route("/soil-test/<int:test_id>/results", methods=["GET", "POST"])
@login_required
def soil_test_results(test_id):
    test = SoilTestRequest.query.filter_by(id=test_id, farmer_id=current_user.id).first_or_404()

    if request.method == "POST":
        results = {
            "pH":            request.form.get("pH", "7.0"),
            "nitrogen":      request.form.get("nitrogen", "medium"),
            "phosphorus":    request.form.get("phosphorus", "medium"),
            "organic_matter": request.form.get("organic_matter", "1.5"),
            "potassium":     request.form.get("potassium", "medium"),
            "salinity":      request.form.get("salinity", "low"),
            "notes":         request.form.get("notes", ""),
        }
        test.results_json = json.dumps(results)
        test.status = "completed"
        db.session.commit()
        flash("Soil test results saved! Here are your crop recommendations.", "success")
        return redirect(url_for("farmer.crop_match", test_id=test.id))

    return render_template("farmer/soil_test_results.html", test=test)


@farmer_bp.route("/soil-test/<int:test_id>/crop-match")
@login_required
def crop_match(test_id):
    """Show crop suitability ranked by the saved soil test results."""
    test = SoilTestRequest.query.filter_by(id=test_id, farmer_id=current_user.id).first_or_404()

    if test.status != "completed" or not test.results_json:
        flash("Soil test results are not available yet.", "warning")
        return redirect(url_for("farmer.soil_test"))

    soil = json.loads(test.results_json)
    soil_for_engine = {
        "pH":             soil.get("pH", "7.0"),
        "nitrogen":       soil.get("nitrogen", "medium"),
        "phosphorus":     soil.get("phosphorus", "medium"),
        "organic_matter": soil.get("organic_matter", "1.5"),
    }
    suggestions = suggest_crops(soil_for_engine, location=current_user.location)
    return render_template(
        "farmer/crop_suggestions.html",
        test=test,
        soil=soil,
        suggestions=suggestions,
    )


@farmer_bp.route("/crop-matches")
@login_required
def crop_matches():
    """List all completed soil tests so the farmer can pick one to view crop matches."""
    completed = SoilTestRequest.query.filter_by(
        farmer_id=current_user.id, status="completed"
    ).order_by(SoilTestRequest.created_at.asc()).all()
    return render_template("farmer/crop_matches.html", completed=completed)


@farmer_bp.route("/soil-test/<int:test_id>/delete", methods=["POST"])
@login_required
def delete_soil_test(test_id):
    test = SoilTestRequest.query.filter_by(id=test_id, farmer_id=current_user.id).first_or_404()
    db.session.delete(test)
    db.session.commit()
    flash("Soil test record removed.", "info")
    return redirect(url_for("farmer.soil_test"))


# ── Crop suggestions API ────────────────────────────────────────────────────

@farmer_bp.route("/api/crop-suggestions", methods=["POST"])
@login_required
def crop_suggestions():
    """
    POST JSON body:
      { "pH": "7.2", "nitrogen": "low", "phosphorus": "medium", "organic_matter": "1.2%" }
    Returns ranked crop suggestions combined with live water + drought data.
    """
    data = request.get_json(silent=True) or {}
    soil = {
        "pH":            str(data.get("pH", "7.0")),
        "nitrogen":      str(data.get("nitrogen", "medium")),
        "phosphorus":    str(data.get("phosphorus", "medium")),
        "organic_matter": str(data.get("organic_matter", "1.5")),
    }
    result = suggest_crops(soil)
    return jsonify(result)


# ── Soil Improvement Resources ──────────────────────────────────────────────

@farmer_bp.route("/soil-resources")
@login_required
def soil_resources():
    """Show NM composting & anaerobic digestion facilities farmers can use."""
    if not current_user.is_subscribed:
        flash("Soil Resources is a Pro feature. Upgrade to unlock it.", "warning")
        return redirect(url_for("farmer.upgrade"))
    from backend.app.services.epa_resources import get_nm_resources
    raw = get_nm_resources()
    farmer_location = current_user.location or ""
    if farmer_location:
        resources = annotate_distances(raw, farmer_location)
    else:
        resources = raw
        resources["farmer_city"] = None
        resources["farmer_coords"] = None
    # Use test_id from query param if provided, otherwise fall back to latest
    test_id = request.args.get("test_id", type=int)
    if test_id:
        amendment_test = SoilTestRequest.query.filter_by(
            id=test_id, farmer_id=current_user.id, status="completed"
        ).first()
    else:
        amendment_test = SoilTestRequest.query.filter_by(
            farmer_id=current_user.id, status="completed"
        ).order_by(SoilTestRequest.created_at.desc()).first()
    return render_template("farmer/soil_resources.html", resources=resources, amendment_test=amendment_test)


@farmer_bp.route("/soil-test/<int:test_id>/amendments")
@login_required
def amendments(test_id):
    """Amendment quantity plan: how much of what to buy, from where."""
    if not current_user.is_subscribed:
        flash("The Amendment Calculator is a Pro feature. Upgrade to unlock it.", "warning")
        return redirect(url_for("farmer.upgrade"))
    test = SoilTestRequest.query.filter_by(id=test_id, farmer_id=current_user.id).first_or_404()
    if test.status != "completed" or not test.results_json:
        flash("Soil test results are not available yet.", "warning")
        return redirect(url_for("farmer.soil_test"))

    soil = json.loads(test.results_json)
    # Prefer the farmer's saved profile location; fall back to the location on the test
    location = current_user.location or test.location or ""
    result = calculate_amendments(soil, location=location)
    return render_template("farmer/amendments.html", test=test, soil=soil, result=result)


# ── Farmer Profile ──────────────────────────────────────────────────────────

@farmer_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        action = request.form.get("action")

        # ── Update basic info ──────────────────────────────────────────────
        if action == "update_info":
            name      = request.form.get("name", "").strip()
            farm_name = request.form.get("farm_name", "").strip()
            phone     = request.form.get("phone", "").strip()
            location  = request.form.get("location", "").strip()

            if not name:
                flash("Full name cannot be empty.", "danger")
                return redirect(url_for("farmer.profile"))

            current_user.name      = name
            current_user.farm_name = farm_name
            current_user.phone     = phone
            current_user.location  = location
            db.session.commit()
            flash("Profile updated successfully.", "success")
            return redirect(url_for("farmer.profile"))

        # ── Change email ───────────────────────────────────────────────────
        elif action == "change_email":
            new_email    = request.form.get("new_email", "").strip().lower()
            confirm_pass = request.form.get("confirm_password_email", "")

            if not new_email:
                flash("Email cannot be empty.", "danger")
                return redirect(url_for("farmer.profile"))

            if not current_user.check_password(confirm_pass):
                flash("Incorrect current password.", "danger")
                return redirect(url_for("farmer.profile"))

            if User.query.filter(User.email == new_email, User.id != current_user.id).first():
                flash("That email is already in use by another account.", "danger")
                return redirect(url_for("farmer.profile"))

            current_user.email = new_email
            db.session.commit()
            flash("Email address updated.", "success")
            return redirect(url_for("farmer.profile"))

        # ── Change password ────────────────────────────────────────────────
        elif action == "change_password":
            current_pass = request.form.get("current_password", "")
            new_pass     = request.form.get("new_password", "")
            confirm_pass = request.form.get("confirm_password", "")

            if not current_user.check_password(current_pass):
                flash("Current password is incorrect.", "danger")
                return redirect(url_for("farmer.profile"))

            if len(new_pass) < 8:
                flash("New password must be at least 8 characters.", "danger")
                return redirect(url_for("farmer.profile"))

            if new_pass != confirm_pass:
                flash("New passwords do not match.", "danger")
                return redirect(url_for("farmer.profile"))

            current_user.set_password(new_pass)
            db.session.commit()
            flash("Password changed successfully.", "success")
            return redirect(url_for("farmer.profile"))

    return render_template("farmer/profile.html")


# ── Upgrade / Subscription ──────────────────────────────────────────────────

@farmer_bp.route("/upgrade")
@login_required
def upgrade():
    """Pricing / upgrade page."""
    return render_template("farmer/upgrade.html")


@farmer_bp.route("/mock-subscribe", methods=["POST"])
@login_required
def mock_subscribe():
    """Mock Stripe checkout — sets is_subscribed=True for demo purposes."""
    current_user.is_subscribed = True
    db.session.commit()
    flash("Welcome to CropPulse Pro! All features are now unlocked.", "success")
    return redirect(url_for("farmer.dashboard"))


@farmer_bp.route("/cancel-subscription", methods=["POST"])
@login_required
def cancel_subscription():
    """Cancel Pro subscription."""
    current_user.is_subscribed = False
    db.session.commit()
    flash("Your Pro subscription has been cancelled. You have been moved to the free plan.", "info")
    return redirect(url_for("farmer.profile"))


# ── Facility Referrals & Transactions ──────────────────────────────────────

REFERRAL_FEE = 10.0    # flat fee per referral ($)
COMMISSION_PCT = 5.0   # % commission on reported deal value


@farmer_bp.route("/soil-resources/refer", methods=["POST"])
@login_required
def facility_refer():
    """Log a farmer→facility referral when farmer clicks 'Contact via CropPulse'."""
    facility_name = request.form.get("facility_name", "").strip()
    facility_type = request.form.get("facility_type", "").strip()
    facility_city = request.form.get("facility_city", "").strip()

    if not facility_name:
        flash("Invalid facility.", "danger")
        return redirect(url_for("farmer.soil_resources"))

    referral = FacilityReferral(
        farmer_id=current_user.id,
        facility_name=facility_name,
        facility_type=facility_type,
        facility_city=facility_city,
        referral_fee_usd=REFERRAL_FEE,
        commission_pct=COMMISSION_PCT,
        commission_usd=REFERRAL_FEE,   # starts as flat fee; grows when deal is closed
        status="referred",
    )
    db.session.add(referral)
    db.session.commit()
    flash(
        f"You're now connected with {facility_name}. "
        "Once you close a deal, mark it in My Facility Contacts.",
        "success"
    )
    return redirect(url_for("farmer.transactions"))


@farmer_bp.route("/transactions")
@login_required
def transactions():
    """Farmer transaction & commission history."""
    referrals = FacilityReferral.query.filter_by(
        farmer_id=current_user.id
    ).order_by(FacilityReferral.referred_at.desc()).all()
    total_earned = sum(r.commission_usd for r in referrals if r.status == "deal_closed")
    total_pipeline = sum(r.referral_fee_usd for r in referrals if r.status != "deal_closed")
    return render_template(
        "farmer/transactions.html",
        referrals=referrals,
        total_earned=total_earned,
        total_pipeline=total_pipeline,
        commission_pct=COMMISSION_PCT,
    )


@farmer_bp.route("/transactions/<int:referral_id>/close", methods=["POST"])
@login_required
def close_deal(referral_id):
    """Farmer reports a completed deal so commission is calculated."""
    referral = FacilityReferral.query.filter_by(
        id=referral_id, farmer_id=current_user.id
    ).first_or_404()

    deal_value = request.form.get("deal_value_usd", "").strip()
    try:
        deal_value = float(deal_value)
        if deal_value < 0:
            raise ValueError
    except ValueError:
        flash("Please enter a valid deal value.", "danger")
        return redirect(url_for("farmer.transactions"))

    from datetime import datetime
    referral.deal_value_usd = deal_value
    referral.commission_usd = referral.referral_fee_usd + (deal_value * referral.commission_pct / 100)
    referral.status = "deal_closed"
    referral.closed_at = datetime.utcnow()
    db.session.commit()

    flash(
        f"Deal closed with {referral.facility_name} for ${deal_value:,.0f}. Great work!",
        "success"
    )
    return redirect(url_for("farmer.transactions"))
