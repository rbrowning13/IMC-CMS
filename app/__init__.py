import sys
import os
import json
import re
from pathlib import Path
from datetime import datetime, date
from zoneinfo import ZoneInfo

from markupsafe import Markup, escape
from sqlalchemy.exc import ProgrammingError, OperationalError
from flask import Flask, request, redirect, current_app
from flask_migrate import Migrate

from dotenv import load_dotenv
load_dotenv()

# Application version
APP_VERSION = "0.6.0"

# Minimal imports for the shared SQLAlchemy instance
try:
    from .extensions import db
except ImportError:
    # Fallback stub to avoid hard crashes if extensions isn&apos;t imported yet.
    db = None

# Flask-Migrate (Alembic integration)
migrate = Migrate()

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
    """Format a datetime for display, using local time when possible.

    - Datetime values are converted to local time if timezone-aware.
    - Naive datetimes are assumed to already be local.
    - Dates are promoted to midnight local time.
    """
    if not value:
        return ""

    local_tz = ZoneInfo(os.environ.get("TZ", "America/Denver"))

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, datetime.min.time())
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except Exception:
            return str(value)

    # If timezone-aware, convert to local time
    if dt.tzinfo is not None:
        dt = dt.astimezone(local_tz)

    return dt.strftime(fmt)


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
    # Initialize migrations
    migrate.init_app(app, db)

    # Register blueprints
    from .routes import bp as main_bp
    from .mobile_routes import mobile_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(mobile_bp, url_prefix="/mobile")

    # ------------------------------------------------------------
    # Mobile auto-redirect
    # ------------------------------------------------------------
    def _is_mobile_user_agent(ua: str) -> bool:
        if not ua:
            return False
        s = ua.lower()
        # Broad, pragmatic UA sniffing (good enough for our use-case)
        mobile_tokens = [
            "iphone",
            "ipod",
            "android",
            "mobile",
            "ipad",
            "windows phone",
        ]
        return any(tok in s for tok in mobile_tokens)

    @app.before_request
    def _mobile_redirect():
        # Only redirect safe, idempotent requests
        if request.method not in ("GET", "HEAD"):
            return None

        # Allow forcing desktop via query string
        if request.args.get("desktop") == "1":
            return None

        path = request.path or "/"

        # Never redirect these (avoid breaking assets/API/dev tools)
        skip_prefixes = (
            "/mobile",
            "/static",
            "/api",
            "/favicon.ico",
            "/robots.txt",
        )
        if path.startswith(skip_prefixes):
            return None

        # Only redirect on mobile-ish user agents
        ua = request.headers.get("User-Agent", "")
        if not _is_mobile_user_agent(ua):
            return None

        # Candidate mobile path
        mobile_path = "/mobile" + path

        # Only redirect if the mobile route actually exists
        try:
            adapter = current_app.url_map.bind(request.host)
            adapter.match(mobile_path, method=request.method)
        except Exception:
            return None

        # Preserve query string, but keep the ability to force desktop
        query_string = request.query_string.decode("utf-8") if request.query_string else ""
        target = mobile_path + ("?" + query_string if query_string else "")
        return redirect(target, code=302)

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
        try:
            existing = Settings.query.first()
        except Exception:
            # During migrations, columns may not exist yet
            existing = None

        if not existing:
            try:
                settings = Settings(
                    business_name="Impact Medical Consulting",
                    # … defaults …
                )
                db.session.add(settings)
                db.session.commit()
            except Exception:
                # If schema is mid-migration, skip seeding
                db.session.rollback()

        _seed_reference_data()

    return app
