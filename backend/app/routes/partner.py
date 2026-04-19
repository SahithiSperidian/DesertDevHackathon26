from flask import Blueprint, render_template
from flask_login import login_required, current_user

partner_bp = Blueprint("partner", __name__)


@partner_bp.route("/dashboard")
@login_required
def dashboard():
    return render_template("partner/dashboard.html")
