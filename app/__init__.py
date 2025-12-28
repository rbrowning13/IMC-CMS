import sys
import os
import json
import re
from pathlib import Path
from datetime import datetime, date

from markupsafe import Markup, escape
from sqlalchemy.exc import ProgrammingError, OperationalError
from flask import Flask

from dotenv import load_dotenv
load_dotenv()

# Application version
APP_VERSION = "0.4.0"

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


def _normalize_multiline_text(value) -> str:
    """Normalize text that may contain HTML-ish <br> fragments into real newlines.

    We have legacy content where users' line breaks were stored as literal "<br>" or
    escaped "&lt;br&gt;" text. This normalizes everything back to real newlines.
    """
    if value is None:
        return ""

    s = str(value)

    # Normalize Windows/Mac newlines first
    s = s.replace("\r\n", "\n").replace("\r", "\n")

    # Handle double-escaped entities that sometimes land in the DB (e.g. "&amp;lt;br&amp;gt;")
    # Convert them back to the single-escaped form so the regex below can catch them.
    s = s.replace("&amp;lt;", "&lt;").replace("&amp;gt;", "&gt;")

    # Convert escaped <br> strings (literally "&lt;br&gt;") into newlines
    s = re.sub(r"(?i)&lt;br\s*/?&gt;", "\n", s)

    # Convert literal <br> tags into newlines
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)

    return s



def nl2br(value):
    """Render multiline text safely, honoring both real newlines and stored <br> tags.

    Important: we must return a Markup object so Jinja does NOT escape the <br> tags.
    """
    s = _normalize_multiline_text(value)
    if not s:
        return Markup("")

    # Escape user content first (prevents HTML injection), then convert real newlines
    # into actual <br> tags and return Markup so they render as HTML.
    escaped = escape(s)
    return Markup(str(escaped).replace("\n", "<br>\n"))


def br2nl(value):
    """Convert stored <br> fragments to newlines (useful for textarea values)."""
    return _normalize_multiline_text(value)

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
    app.jinja_env.filters["br2nl"] = br2nl

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
                business_name="Impact Medical Consulting",
                # … defaults …
            )
            db.session.add(settings)
            db.session.commit()
        _seed_reference_data()

    return app
