import os
import sys
import webbrowser
from threading import Timer

from app import create_app


def run_flask():
    """Create the Flask app and run the development server.

    This is a simple launcher so that Gina (or you) can just run one command
    or double-click a bundled app. It:

    - creates the Flask app via create_app()
    - starts it on http://127.0.0.1:5000 by default
    - tries to open the browser automatically once the server is starting up
    """
    app = create_app()

    host = os.environ.get("IMPACTCMS_HOST", "127.0.0.1")
    port_str = os.environ.get("IMPACTCMS_PORT", "5000")
    try:
        port = int(port_str)
    except ValueError:
        port = 5000

    url = f"http://{host}:{port}/"

    def _open_browser():
        try:
            webbrowser.open(url)
        except Exception:
            # If the browser can't be opened, just keep the server running
            pass

    # Skip auto-open if you run: python impact_launcher.py --no-browser
    if not any(arg in sys.argv for arg in ("--no-browser", "-nb")):
        Timer(1.5, _open_browser).start()

    # use_reloader=False -> no extra child process, better for PyInstaller too
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    print("Starting Impact Medical CMS on http://127.0.0.1:5000 …")
    print("Press CTRL+C in this window to stop it.")
    run_flask()