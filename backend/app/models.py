from app import db, login_manager
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'farmer' or 'partner'
    location = db.Column(db.String(200))
    phone = db.Column(db.String(30))
    farm_name = db.Column(db.String(200))
    is_subscribed = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    soil_tests = db.relationship("SoilTestRequest", backref="farmer", lazy=True)
    resources = db.relationship("Resource", backref="partner", lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class SoilTestRequest(db.Model):
    __tablename__ = "soil_tests"

    id = db.Column(db.Integer, primary_key=True)
    farmer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    location = db.Column(db.String(200))
    test_center = db.Column(db.String(200))
    status = db.Column(db.String(20), default="pending")  # pending / completed
    results_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Resource(db.Model):
    __tablename__ = "resources"

    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    type = db.Column(db.String(50))  # compost / water / equipment / kitchen
    title = db.Column(db.String(200))
    description = db.Column(db.Text)
    quantity = db.Column(db.Float)
    unit = db.Column(db.String(50))
    location = db.Column(db.String(200))
    lat = db.Column(db.Float)
    lng = db.Column(db.Float)
    available = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    connections = db.relationship("ConnectionRequest", backref="resource", lazy=True)


class ConnectionRequest(db.Model):
    __tablename__ = "connections"

    id = db.Column(db.Integer, primary_key=True)
    farmer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    resource_id = db.Column(db.Integer, db.ForeignKey("resources.id"), nullable=False)
    message = db.Column(db.Text)
    status = db.Column(db.String(20), default="pending")  # pending / accepted / declined
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class FacilityReferral(db.Model):
    """Tracks farmer → EPA facility referrals generated through CropPulse."""
    __tablename__ = "facility_referrals"

    id = db.Column(db.Integer, primary_key=True)
    farmer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    facility_name = db.Column(db.String(300), nullable=False)
    facility_type = db.Column(db.String(100))          # e.g. Composting, Anaerobic Digestion
    facility_city = db.Column(db.String(200))
    referral_fee_usd = db.Column(db.Float, default=10.0)   # flat referral fee
    deal_value_usd = db.Column(db.Float, nullable=True)    # farmer-reported deal value (optional)
    commission_pct = db.Column(db.Float, default=5.0)      # % commission on deal value
    commission_usd = db.Column(db.Float, default=0.0)      # computed: flat + (deal_value * pct/100)
    status = db.Column(db.String(30), default="referred")  # referred / contacted / deal_closed
    referred_at = db.Column(db.DateTime, default=datetime.utcnow)
    closed_at = db.Column(db.DateTime, nullable=True)

    farmer = db.relationship("User", backref="referrals", lazy=True)
