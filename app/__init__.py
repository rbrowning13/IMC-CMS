import json
import datetime
from flask import Flask

CONTACT_ROLE_DEFAULTS = [
    "Adjuster",
    "Nurse Case Manager",
    "Claims Representative",
    "HR / Employer Contact",
    "Billing",
    "Attorney",
    "Provider Office Contact",
    "Other",
]

def format_date(value, fmt="%m/%d/%Y"):
    """
    Jinja filter to render dates consistently as MM/DD/YYYY for display.
    Leaves non-date values unchanged.
    """
    if not value:
        return ""
    # If it's a datetime or date-like object, try strftime
    if hasattr(value, "strftime"):
        try:
            return value.strftime(fmt)
        except Exception:
            return str(value)
    # Fallback: just convert to string
    return str(value)

# Import the shared SQLAlchemy instance
from .extensions import db


def _seed_reference_data():
    """Create a few demo carriers/employers/providers and contacts if tables are empty."""
    from .models import Carrier, Employer, Provider, Contact, BarrierOption
    from .extensions import db as _db

    something_added = False

    demo_carriers = []
    demo_employers = []
    demo_providers = []
    demo_contacts = []

    # Seed carriers
    if Carrier.query.count() == 0:
        c1 = Carrier(
            name="Acme Insurance Co.",
            city="Boise",
            state="ID",
            postal_code="83702",
            phone="208-555-1000",
            fax="208-555-1001",
            email="claims@acmeins.com",
        )
        c2 = Carrier(
            name="Northwest Casualty",
            city="Spokane",
            state="WA",
            postal_code="99201",
            phone="509-555-2000",
            fax="509-555-2001",
            email="intake@nwcasualty.com",
        )
        demo_carriers.extend([c1, c2])

        # Contacts for carriers
        demo_contacts.extend(
            [
                Contact(
                    name="Alice Adjuster",
                    role="Adjuster",
                    phone="208-555-1100",
                    email="alice.adjuster@acmeins.com",
                    carrier=c1,
                ),
                Contact(
                    name="Nina Nurse",
                    role="Nurse Case Manager",
                    phone="208-555-1101",
                    email="nina.nurse@acmeins.com",
                    carrier=c1,
                ),
                Contact(
                    name="Bob Adjuster",
                    role="Adjuster",
                    phone="509-555-2100",
                    email="bob.adjuster@nwcasualty.com",
                    carrier=c2,
                ),
            ]
        )
        something_added = True

    # Seed employers
    if Employer.query.count() == 0:
        e1 = Employer(
            name="Example Manufacturing, Inc.",
            city="Nampa",
            state="ID",
            postal_code="83651",
            phone="208-555-3000",
        )
        e2 = Employer(
            name="Sunrise Distribution, LLC",
            city="Meridian",
            state="ID",
            postal_code="83642",
            phone="208-555-4000",
        )
        demo_employers.extend([e1, e2])

        # Contacts for employers
        demo_contacts.extend(
            [
                Contact(
                    name="Harriet HR",
                    role="HR / Employer Contact",
                    phone="208-555-3100",
                    email="harriet.hr@examplemfg.com",
                    employer=e1,
                ),
                Contact(
                    name="Dan Supervisor",
                    role="HR / Employer Contact",
                    phone="208-555-4100",
                    email="dan.supervisor@sunrisedist.com",
                    employer=e2,
                ),
            ]
        )
        something_added = True

    # Seed providers
    if Provider.query.count() == 0:
        p1 = Provider(
            name="Boise Orthopedic Clinic",
            city="Boise",
            state="ID",
            postal_code="83706",
            phone="208-555-5000",
            fax="208-555-5001",
            email="referrals@boiseortho.com",
        )
        p2 = Provider(
            name="Valley Primary Care",
            city="Caldwell",
            state="ID",
            postal_code="83605",
            phone="208-555-6000",
            fax="208-555-6001",
            email="frontdesk@valleypc.com",
        )
        demo_providers.extend([p1, p2])

        # Contacts for providers
        demo_contacts.extend(
            [
                Contact(
                    name="Olivia Ortho MA",
                    role="Provider Office Contact",
                    phone="208-555-5100",
                    email="olivia.ma@boiseortho.com",
                    provider=p1,
                ),
                Contact(
                    name="Patty PCP MA",
                    role="Provider Office Contact",
                    phone="208-555-6100",
                    email="patty.ma@valleypc.com",
                    provider=p2,
                ),
            ]
        )
        something_added = True

    # Seed default barrier options if none exist
    if BarrierOption.query.count() == 0:
        _db.session.add_all(
            [
                BarrierOption(
                    category="General",
                    label="Depression / PTSD / Psychosocial",
                    sort_order=10,
                    is_active=True,
                ),
                BarrierOption(
                    category="General",
                    label="Smoker",
                    sort_order=20,
                    is_active=True,
                ),
                BarrierOption(
                    category="General",
                    label="Treatment Noncompliance",
                    sort_order=30,
                    is_active=True,
                ),
                BarrierOption(
                    category="General",
                    label="Diabetes",
                    sort_order=40,
                    is_active=True,
                ),
                BarrierOption(
                    category="General",
                    label="Frequently Missing Work",
                    sort_order=50,
                    is_active=True,
                ),
                BarrierOption(
                    category="General",
                    label="Hypertension",
                    sort_order=60,
                    is_active=True,
                ),
                BarrierOption(
                    category="General",
                    label="Substance Abuse History",
                    sort_order=70,
                    is_active=True,
                ),
                BarrierOption(
                    category="General",
                    label="Pain Management",
                    sort_order=80,
                    is_active=True,
                ),
                BarrierOption(
                    category="General",
                    label="Legal Representation",
                    sort_order=90,
                    is_active=True,
                ),
                BarrierOption(
                    category="General",
                    label="Surgery or Recent Hospital Stay",
                    sort_order=100,
                    is_active=True,
                ),
                BarrierOption(
                    category="General",
                    label="Late Injury Reporting",
                    sort_order=110,
                    is_active=True,
                ),
            ]
        )
        _db.session.commit()

    if something_added:
        # Add all newly created rows and commit
        if demo_carriers:
            _db.session.add_all(demo_carriers)
        if demo_employers:
            _db.session.add_all(demo_employers)
        if demo_providers:
            _db.session.add_all(demo_providers)
        if demo_contacts:
            _db.session.add_all(demo_contacts)
        _db.session.commit()


def create_app():
    """Application factory for the Impact CMS."""
    app = Flask(__name__)

    # Register Jinja filters
    app.jinja_env.filters["format_date"] = format_date

    # Basic configuration
    app.config["SECRET_KEY"] = app.config.get("SECRET_KEY") or "dev-secret-key-change-me"
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
                contact_roles_json=json.dumps(CONTACT_ROLE_DEFAULTS),
            )
            db.session.add(settings)
            db.session.commit()

        _seed_reference_data()

    return app