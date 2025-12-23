import sys
import os
import json
from pathlib import Path
from datetime import datetime, date

from flask import Flask

from dotenv import load_dotenv
load_dotenv()

# Application version
APP_VERSION = "0.3.0"

# Minimal imports for the shared SQLAlchemy instance
try:
    from .extensions import db
except ImportError:
    # Fallback stub to avoid hard crashes if extensions isn&apos;t imported yet.
    db = None

# --- Minimal filter stubs to keep templates working even if the real helpers live elsewhere ---
def format_date(value, fmt="%m/%d/%Y"):
    """Format a date or datetime for display.

    Default format is MM/DD/YYYY. If value is falsy, return an empty string.
    """
    if not value:
        return ""
    # Accept both date and datetime objects; fall back to string if needed
    if isinstance(value, (date, datetime)):
        return value.strftime(fmt)
    try:
        # Try to parse common ISO-like strings
        parsed = datetime.fromisoformat(str(value))
        return parsed.strftime(fmt)
    except Exception:
        # As a last resort, just return the original value
        return str(value)

def format_datetime(value, fmt="%m/%d/%Y %H:%M"):
    """Format a datetime for display.

    Default format is MM/DD/YYYY HH:MM (24-hour). If value is falsy, return an empty string.
    """
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime(fmt)
    if isinstance(value, date):
        # Promote date to datetime at midnight
        dt = datetime.combine(value, datetime.min.time())
        return dt.strftime(fmt)
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.strftime(fmt)
    except Exception:
        return str(value)

def nl2br(value):
    """Fallback newline-to-&lt;br&gt; formatter."""
    if value is None:
        return ""
    return str(value).replace("\n", "<br>")

# Demo contact-role defaults stub; real values can be overridden elsewhere.
CONTACT_ROLE_DEFAULTS = []

def _seed_reference_data():
    """No-op seed stub; real implementation is defined elsewhere."""
    return

def _get_database_path() -> str:
    """Return the full path to the SQLite database file.

    - In a frozen PyInstaller app, keep the DB next to the executable
      (inside the .app bundle, e.g. Contents/MacOS/impact_cms.db).
    - In normal dev/source checkouts, use project_root/impact_cms.db.

    This keeps the DB separate from the documents tree but still easy
    to back up or migrate between versions.
    """
    # PyInstaller sets sys.frozen and sys.executable inside the bundle
    if getattr(sys, "frozen", False) and hasattr(sys, "executable"):
        exe_dir = Path(sys.executable).resolve().parent
        return str(exe_dir / "impact_cms.db")

    # Default: running from source / a normal checkout
    project_root = Path(__file__).resolve().parent.parent
    return str(project_root / "impact_cms.db")

def create_app():
    """Application factory for the Impact CMS.

    Handles both normal source checkout and PyInstaller-frozen bundle by
    choosing the correct template/static/documents paths.
    """
    # Determine paths depending on whether we're frozen under PyInstaller
    if hasattr(sys, "_MEIPASS"):
        # Running from a bundled .app
        base_dir = sys._MEIPASS
        templates_folder = os.path.join(base_dir, "templates")
        static_folder = os.path.join(base_dir, "static")
        documents_root = os.path.join(base_dir, "documents")
    else:
        # Normal dev / source checkout
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        templates_folder = os.path.join(project_root, "app", "templates")
        static_folder = os.path.join(project_root, "app", "static")
        documents_root = os.path.join(project_root, "documents")

    # Ensure documents folder exists
    os.makedirs(documents_root, exist_ok=True)

    # Create Flask app pointing at the resolved template/static folders
    app = Flask(
        __name__,
        template_folder=templates_folder,
        static_folder=static_folder,
    )
    app.config.setdefault("APP_VERSION", APP_VERSION)

    # Register Jinja filters
    app.jinja_env.filters["format_date"] = format_date
    app.jinja_env.filters["format_datetime"] = format_datetime
    app.jinja_env.filters["nl2br"] = nl2br

    def _format_phone(value):
        """Format US phone numbers as (###) ###-#### when possible; otherwise return original."""
        if value is None:
            return ""
        s = str(value).strip()
        if not s:
            return ""
        digits = "".join(ch for ch in s if ch.isdigit())
        # Handle leading country code '1'
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10:
            return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"
        return s

    app.jinja_env.filters["format_phone"] = _format_phone
    app.jinja_env.filters["format_fax"] = _format_phone

    # Register template globals
    try:
        from .routes.helpers import state_options
    except Exception:
        def state_options(selected=None):
            return ""
    app.jinja_env.globals["state_options"] = state_options

    # Basic configuration
    app.config["SECRET_KEY"] = app.config.get("SECRET_KEY") or "dev-secret-key-change-me"

    # Database configuration (Postgres via DATABASE_URL; fail loudly if missing)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set. Refusing to start with an implicit SQLite database.")

    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Toggle demo data seeding (False for production, True for development)
    app.config.setdefault("SEED_DEMO_DATA", False)

    # Expose documents root in config so other modules can use it
    app.config.setdefault("DOCUMENTS_ROOT", documents_root)

    # Initialize extensions (register this app with the shared db instance)
    db.init_app(app)

    # Register blueprints
    from .routes import bp as main_bp
    from .mobile_routes import mobile_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(mobile_bp, url_prefix="/mobile")

    @app.context_processor
    def inject_settings():
        from .models import Settings
        try:
            settings = Settings.query.first()
        except Exception:
            settings = None
        return {"settings": settings}

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
                documents_root=documents_root,
                contact_roles_json=json.dumps(CONTACT_ROLE_DEFAULTS),
            )
            db.session.add(settings)
            db.session.commit()

        _seed_reference_data()

    return app