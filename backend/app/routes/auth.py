from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from backend.app import db
from backend.app.models import User

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/")
def index():
    if current_user.is_authenticated:
        if current_user.role == "farmer":
            return redirect(url_for("farmer.dashboard"))
        return redirect(url_for("partner.dashboard"))
    return render_template("index.html")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("auth.index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()

        if user and user.check_password(password):
            login_user(user, remember=request.form.get("remember") == "on")
            flash(f"Welcome back, {user.name}!", "success")
            next_page = request.args.get("next")
            if user.role == "farmer":
                return redirect(next_page or url_for("farmer.dashboard"))
            return redirect(next_page or url_for("partner.dashboard"))

        flash("Invalid email or password.", "danger")

    return render_template("auth/login.html")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("auth.index"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        role = request.form.get("role", "farmer")
        location = request.form.get("location", "").strip()

        if not name or not email or not password:
            flash("Please fill in all required fields.", "danger")
            return render_template("auth/register.html")

        if password != confirm:
            flash("Passwords do not match.", "danger")
            return render_template("auth/register.html")

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
            return render_template("auth/register.html")

        if User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "danger")
            return render_template("auth/register.html")

        if role not in ("farmer", "partner"):
            role = "farmer"

        user = User(name=name, email=email, role=role, location=location)
        user.set_password(password)
        
        try:
            db.session.add(user)
            db.session.commit()
            login_user(user)
            flash(f"Account created! Welcome to CropPulse, {name}.", "success")
        except Exception as e:
            db.session.rollback()
            flash("An error occurred during account creation. Please try again.", "danger")
            return render_template("auth/register.html")

        if role == "farmer":
            return redirect(url_for("farmer.dashboard"))
        return redirect(url_for("partner.dashboard"))

    return render_template("auth/register.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
