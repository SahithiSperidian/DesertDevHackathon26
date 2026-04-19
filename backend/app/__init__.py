import os
import json
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from dotenv import load_dotenv

# Load .env from backend root before anything else
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

db = SQLAlchemy()
login_manager = LoginManager()

# Resolve paths relative to this file so they work from any cwd
_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_frontend_dir = os.path.join(os.path.dirname(_backend_dir), "frontend")
_template_dir = os.path.join(_frontend_dir, "app", "templates")
_static_dir = os.path.join(_frontend_dir, "app", "static")


def create_app():
    app = Flask(
        __name__,
        template_folder=_template_dir,
        static_folder=_static_dir,
    )

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "croppulse-hackathon-secret-2026")
    # Use DATABASE_URL from env (Render PostgreSQL) or fall back to local SQLite
    db_url = os.environ.get("DATABASE_URL", "sqlite:///croppulse.db")
    # Render gives postgres:// but SQLAlchemy needs postgresql://
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "warning"

    # Jinja2 custom filter: parse JSON strings in templates
    @app.template_filter("from_json")
    def from_json_filter(value):
        if not value:
            return {}
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return {}

    from backend.app.routes.auth import auth_bp
    from backend.app.routes.farmer import farmer_bp
    from backend.app.routes.partner import partner_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(farmer_bp, url_prefix="/farmer")
    app.register_blueprint(partner_bp, url_prefix="/partner")

    with app.app_context():
        from backend.app import models
        db.create_all()
        # Safe migration: add new columns to existing SQLite DB if missing
        _run_migrations(app)

    return app


def _run_migrations(app):
    """Add columns introduced after initial schema without dropping data."""
    from sqlalchemy import text
    new_cols = [
        ("users", "phone",     "VARCHAR(30)"),
        ("users", "farm_name", "VARCHAR(200)"),
    ]
    with app.app_context():
        with db.engine.connect() as conn:
            for table, col, col_type in new_cols:
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                    conn.commit()
                except Exception:
                    pass  # column already exists
