

"""Forms / templates routes.

This module is intentionally self-contained and keeps its UI in simple
`render_template_string` blocks so we don't have to maintain separate template
files yet. Later we can move these into proper Jinja templates.
"""

from __future__ import annotations

from datetime import datetime

from flask import flash, redirect, request, render_template_string, url_for

from . import bp
from app import db
from app.models import Settings


def _ensure_settings() -> Settings:
    """Return the singleton Settings row, creating it if missing."""
    settings = Settings.query.first()
    if not settings:
        settings = Settings()
        db.session.add(settings)
        db.session.commit()
    return settings


@bp.route("/forms")
def forms_index():
    """Landing page for quick forms/templates."""
    settings = _ensure_settings()

    html = """
{% extends "base.html" %}
{% block content %}
  <div class="d-flex align-items-center justify-content-between mb-3">
    <h1 class="m-0">Forms</h1>
  </div>

  <div class="list-group">
    <a class="list-group-item list-group-item-action" href="{{ url_for('main.forms_fax_cover') }}">
      <div class="d-flex w-100 justify-content-between">
        <h5 class="mb-1">Fax Cover Sheet</h5>
        <small class="text-muted">Template</small>
      </div>
      <p class="mb-1">Quick printable fax cover sheet with editable To/From details.</p>
    </a>

    <div class="list-group-item">
      <div class="d-flex w-100 justify-content-between">
        <h5 class="mb-1 text-muted">Face Sheet</h5>
        <small class="text-muted">Coming soon</small>
      </div>
      <p class="mb-0 text-muted">We’ll move Face Sheet generation here once the Forms/Templates workflow is finalized.</p>
    </div>
  </div>
{% endblock %}
"""

    return render_template_string(html, settings=settings, active_page="forms")


@bp.route("/forms/fax-cover", methods=["GET", "POST"])
def forms_fax_cover():
    """Fax cover sheet generator.

    This intentionally mirrors the original behavior from the legacy monolithic
    routes.py: an edit mode (form) and a preview/print mode.

    NOTE: the "Search" box is a placeholder for the future contact lookup
    endpoint (api.py) — it does not perform live lookup yet.
    """

    settings = _ensure_settings()

    # Defaults from Settings (editable in the Settings UI)
    from_name_default = (settings.business_name or "").strip()
    from_phone_default = (getattr(settings, "phone", "") or "").strip()
    from_email_default = (getattr(settings, "email", "") or "").strip()
    from_fax_default = (getattr(settings, "fax", "") or "").strip()

    # Optional: if present in Settings
    case_manager_name_default = (getattr(settings, "responsible_case_manager", "") or "").strip()

    # Form values (POST overrides)
    to_name = (request.form.get("to_name") or "").strip()
    to_fax = (request.form.get("to_fax") or "").strip()
    to_phone = (request.form.get("to_phone") or "").strip()
    to_email = (request.form.get("to_email") or "").strip()
    to_address = (request.form.get("to_address") or "").strip()

    from_name = (request.form.get("from_name") or from_name_default).strip()
    from_phone = (request.form.get("from_phone") or from_phone_default).strip()
    from_fax = (request.form.get("from_fax") or from_fax_default).strip()
    from_email = (request.form.get("from_email") or from_email_default).strip()
    from_case_manager = (request.form.get("from_case_manager") or case_manager_name_default).strip()

    subject = (request.form.get("subject") or "").strip()
    pages = (request.form.get("pages") or "").strip()
    contents = (request.form.get("contents") or "").strip()

    mode = (request.form.get("mode") or "edit").strip().lower()

    # Preview/print mode
    if request.method == "POST" and mode == "preview":
        preview_html = """
{% extends "base.html" %}
{% block content %}
  <div class="d-print-none mb-3">
    <a class="btn btn-outline-secondary" href="{{ url_for('main.forms_fax_cover') }}">← Back to Edit</a>
    <button class="btn btn-primary" onclick="window.print()">Print</button>
  </div>

  <div class="border rounded p-4 bg-white">
    <div class="d-flex align-items-center justify-content-between">
      <h1 class="h3 m-0">Fax Cover Sheet</h1>
      <div class="text-muted">{{ now }}</div>
    </div>

    <hr>

    <div class="row">
      <div class="col-md-6">
        <h2 class="h5">To</h2>
        <dl class="row mb-0">
          <dt class="col-4">Name</dt><dd class="col-8">{{ to_name }}</dd>
          <dt class="col-4">Fax</dt><dd class="col-8">{{ to_fax }}</dd>
          <dt class="col-4">Phone</dt><dd class="col-8">{{ to_phone }}</dd>
          <dt class="col-4">Email</dt><dd class="col-8">{{ to_email }}</dd>
          <dt class="col-4">Address</dt><dd class="col-8">{{ to_address }}</dd>
        </dl>
      </div>

      <div class="col-md-6">
        <h2 class="h5">From</h2>
        <dl class="row mb-0">
          <dt class="col-4">Business</dt><dd class="col-8">{{ from_name }}</dd>
          <dt class="col-4">Case Manager</dt><dd class="col-8">{{ from_case_manager }}</dd>
          <dt class="col-4">Phone</dt><dd class="col-8">{{ from_phone }}</dd>
          <dt class="col-4">Fax</dt><dd class="col-8">{{ from_fax }}</dd>
          <dt class="col-4">Email</dt><dd class="col-8">{{ from_email }}</dd>
        </dl>
      </div>
    </div>

    <hr>

    <div class="row">
      <div class="col-md-8">
        <h2 class="h5">Re / Subject</h2>
        <div class="mb-3">{{ subject }}</div>

        <h2 class="h5">Description of contents</h2>
        <div style="white-space: pre-wrap;">{{ contents }}</div>
      </div>

      <div class="col-md-4">
        <h2 class="h5">Pages</h2>
        <div class="display-6">{{ pages }}</div>
      </div>
    </div>

  </div>
{% endblock %}
"""

        return render_template_string(
            preview_html,
            active_page="forms",
            settings=settings,
            now=datetime.now().strftime("%m/%d/%Y %I:%M %p"),
            to_name=to_name,
            to_fax=to_fax,
            to_phone=to_phone,
            to_email=to_email,
            to_address=to_address,
            from_name=from_name,
            from_case_manager=from_case_manager,
            from_phone=from_phone,
            from_fax=from_fax,
            from_email=from_email,
            subject=subject,
            pages=pages,
            contents=contents,
        )

    # Edit mode
    edit_html = """
{% extends "base.html" %}
{% block content %}
  <div class="d-flex align-items-center justify-content-between mb-3">
    <h1 class="m-0">Fax Cover Sheet</h1>
    <a class="btn btn-outline-secondary" href="{{ url_for('main.forms_index') }}">← Back to Forms</a>
  </div>

  <p class="text-muted">
    Tip: the “Search” box is a placeholder for future contact lookup. For now, just type what you need.
  </p>

  <form method="post" class="mb-4">
    <input type="hidden" name="mode" value="preview">

    <div class="row g-3">
      <div class="col-12">
        <label class="form-label">Search (placeholder)</label>
        <input class="form-control" name="search" value="" placeholder="Start typing a name...">
      </div>

      <div class="col-md-6">
        <h2 class="h5 mt-2">To</h2>

        <label class="form-label">To (Name)</label>
        <input class="form-control" name="to_name" value="{{ to_name }}">

        <div class="row g-2 mt-1">
          <div class="col-md-6">
            <label class="form-label">Fax</label>
            <input class="form-control" name="to_fax" value="{{ to_fax }}">
          </div>
          <div class="col-md-6">
            <label class="form-label">Phone</label>
            <input class="form-control" name="to_phone" value="{{ to_phone }}">
          </div>
        </div>

        <div class="row g-2 mt-1">
          <div class="col-md-6">
            <label class="form-label">Email</label>
            <input class="form-control" name="to_email" value="{{ to_email }}">
          </div>
          <div class="col-md-6">
            <label class="form-label">Address</label>
            <input class="form-control" name="to_address" value="{{ to_address }}">
          </div>
        </div>
      </div>

      <div class="col-md-6">
        <h2 class="h5 mt-2">From</h2>

        <label class="form-label">From (Business)</label>
        <input class="form-control" name="from_name" value="{{ from_name }}">

        <label class="form-label mt-2">Case Manager</label>
        <input class="form-control" name="from_case_manager" value="{{ from_case_manager }}">

        <div class="row g-2 mt-1">
          <div class="col-md-6">
            <label class="form-label">Phone</label>
            <input class="form-control" name="from_phone" value="{{ from_phone }}">
          </div>
          <div class="col-md-6">
            <label class="form-label">Fax</label>
            <input class="form-control" name="from_fax" value="{{ from_fax }}">
          </div>
        </div>

        <label class="form-label mt-2">Email</label>
        <input class="form-control" name="from_email" value="{{ from_email }}">
      </div>

      <div class="col-12">
        <h2 class="h5 mt-2">Details</h2>

        <div class="row g-2">
          <div class="col-md-8">
            <label class="form-label">Re / Subject</label>
            <input class="form-control" name="subject" value="{{ subject }}">
          </div>
          <div class="col-md-4">
            <label class="form-label">Pages</label>
            <input class="form-control" name="pages" value="{{ pages }}" placeholder="e.g. 12">
          </div>
        </div>

        <label class="form-label mt-2">Description of contents</label>
        <textarea class="form-control" name="contents" rows="5">{{ contents }}</textarea>
      </div>

      <div class="col-12 d-flex gap-2">
        <button class="btn btn-primary" type="submit">Preview / Print</button>
        <a class="btn btn-outline-secondary" href="{{ url_for('main.forms_fax_cover') }}">Reset</a>
      </div>
    </div>
  </form>
{% endblock %}
"""

    return render_template_string(
        edit_html,
        active_page="forms",
        settings=settings,
        to_name=to_name,
        to_fax=to_fax,
        to_phone=to_phone,
        to_email=to_email,
        to_address=to_address,
        from_name=from_name,
        from_case_manager=from_case_manager,
        from_phone=from_phone,
        from_fax=from_fax,
        from_email=from_email,
        subject=subject,
        pages=pages,
        contents=contents,
    )