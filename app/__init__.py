from flask import Flask

# Import the shared SQLAlchemy instance
from .extensions import db


def _seed_reference_data():
    """Create a few demo carriers/employers/providers if tables are empty."""
    from .models import Carrier, Employer, Provider

    something_added = False

    if Carrier.query.count() == 0:
        demo_carriers = [
            Carrier(
                name="Acme Insurance Co.",
                city="Boise",
                state="ID",
                zip="83702",
                phone="208-555-1000",
                fax="208-555-1001",
                email="claims@acmeins.com",
            ),
            Carrier(
                name="Northwest Casualty",
                city="Spokane",
                state="WA",
                zip="99201",
                phone="509-555-2000",
                fax="509-555-2001",
                email="intake@nwcasualty.com",
            ),
        ]
        from .extensions import db as _db
        _db.session.add_all(demo_carriers)
        something_added = True

    if Employer.query.count() == 0:
        demo_employers = [
            Employer(
                name="Example Manufacturing, Inc.",
                city="Nampa",
                state="ID",
                zip="83651",
                phone="208-555-3000",
            ),
            Employer(
                name="Sunrise Distribution, LLC",
                city="Meridian",
                state="ID",
                zip="83642",
                phone="208-555-4000",
            ),
        ]
        from .extensions import db as _db
        _db.session.add_all(demo_employers)
        something_added = True

    if Provider.query.count() == 0:
        demo_providers = [
            Provider(
                name="Boise Orthopedic Clinic",
                city="Boise",
                state="ID",
                zip="83706",
                phone="208-555-5000",
                fax="208-555-5001",
                email="referrals@boiseortho.com",
            ),
            Provider(
                name="Valley Primary Care",
                city="Caldwell",
                state="ID",
                zip="83605",
                phone="208-555-6000",
                fax="208-555-6001",
                email="frontdesk@valleypc.com",
            ),
        ]
        from .extensions import db as _db
        _db.session.add_all(demo_providers)
        something_added = True

    if something_added:
        from .extensions import db as _db
        _db.session.commit()


def create_app():
    """Application factory for the Impact CMS."""
    app = Flask(__name__)

    # Basic configuration
    app.config.setdefault("SECRET_KEY", "change-this-secret-key")
    # SQLite database in the project root (adjust if you want a different path)
    app.config.setdefault("SQLALCHEMY_DATABASE_URI", "sqlite:///impact_cms.db")
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)

    # Initialize extensions (register this app with the shared db instance)
    db.init_app(app)

    # Register blueprints
    from .routes import bp as main_bp
    app.register_blueprint(main_bp)

    # Create tables and ensure a Settings row exists
    with app.app_context():
        from .models import Settings  # ensure models are registered

        # Create all tables if they don't exist
        db.create_all()

        # Ensure there is at least one Settings row
        existing = Settings.query.first()
        if not existing:
            settings = Settings(
                business_name="Impact Medical Consulting, PLLC",
                documents_root="documents",
            )
            db.session.add(settings)
            db.session.commit()

        _seed_reference_data()

    return app