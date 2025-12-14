"""Main (non-mobile) routes package.

This package defines the primary Flask blueprint (`bp`) and imports the split
route modules so their @bp.route decorators are registered.

NOTE: The Flask app factory and database initialization should live in `app/__init__.py`,
not inside the routes package.
"""

from flask import Blueprint

# Primary site blueprint
bp = Blueprint("main", __name__)

# Import route modules to register routes on the blueprint.
# These imports must come AFTER `bp` is defined.
from . import claims  # noqa: F401,E402
from . import reports  # noqa: F401,E402
from . import invoices  # noqa: F401,E402
from . import settings  # noqa: F401,E402

# Additional route modules to be migrated out of app/routes.py
from . import api  # noqa: F401,E402
from . import billing  # noqa: F401,E402
from . import documents  # noqa: F401,E402
from . import forms  # noqa: F401,E402
from . import core_data  # noqa: F401,E402
