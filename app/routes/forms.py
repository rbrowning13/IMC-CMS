"""Forms / templates routes.

Forms live in dedicated Jinja templates under `templates/forms/`.
Routes in this module coordinate data + rendering and (when needed) Playwright PDF output.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote as url_quote

from flask import (
    flash,
    redirect,
    request,
    render_template,
    render_template_string,
    url_for,
    jsonify,
    make_response,
    current_app,
    session,
)
from playwright.sync_api import sync_playwright

from sqlalchemy import or_

from . import bp
from app import db
from app.models import Settings
from app.models import Carrier, Employer, Provider, Claim, Contact, ContactRole



def _ensure_settings() -> Settings:
    """Return the singleton Settings row, creating it if missing."""
    settings = Settings.query.first()
    if not settings:
        settings = Settings()
        db.session.add(settings)
        db.session.commit()
    return settings

def _settings_tz(settings: Settings) -> ZoneInfo:
    """Resolve the app's local timezone for forms/prints.

    Defaults to Mountain Time to match the business.
    """
    tz_name = (
        (getattr(settings, "time_zone", None) or "")
        or (getattr(settings, "timezone", None) or "")
        or (getattr(settings, "tz", None) or "")
    )
    tz_name = str(tz_name).strip() if tz_name else ""
    if not tz_name:
        tz_name = "America/Denver"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("America/Denver")


def _now_local(settings: Settings) -> datetime:
    return datetime.now(_settings_tz(settings))


def _fax_session_key(claim_id: int | None) -> str:
    return "fax_cover_standalone" if claim_id is None else f"fax_cover_claim_{claim_id}"


def _fax_load_from_session(claim_id: int | None) -> dict:
    data = dict(session.get(_fax_session_key(claim_id)) or {})

    # Standalone fax cover should not persist forever.
    # Expire standalone drafts after 12 hours.
    if claim_id is None and data:
        try:
            saved_at = data.get("_saved_at")
            if saved_at:
                saved_dt = datetime.fromisoformat(str(saved_at))
                age_seconds = (datetime.utcnow() - saved_dt).total_seconds()
                if age_seconds > 12 * 3600:
                    session.pop(_fax_session_key(claim_id), None)
                    return {}
        except Exception:
            # If anything about the timestamp is weird, just treat as not saved.
            session.pop(_fax_session_key(claim_id), None)
            return {}

    # Do not leak internal keys into the form dict
    data.pop("_saved_at", None)
    return data


def _fax_save_to_session(claim_id: int | None, payload: dict) -> None:
    data = dict(payload or {})

    # Add a timestamp only for the standalone form so it expires.
    if claim_id is None:
        data["_saved_at"] = datetime.utcnow().isoformat()

    session[_fax_session_key(claim_id)] = data
    
def _settings_logo_url(settings: Settings) -> str | None:
    """Best-effort logo URL for templates.

    Supports several possible Settings field names (historical drift) and normalizes
    relative paths into a /static/... URL.
    """

    candidates = [
        getattr(settings, "logo_url", None),
        getattr(settings, "logo_path", None),
        getattr(settings, "logo_filename", None),
        getattr(settings, "logo_file", None),
        getattr(settings, "logo", None),
    ]

    raw = ""
    for c in candidates:
        if c:
            raw = str(c).strip()
            if raw:
                break

    if not raw:
        return None

    if raw.startswith("http://") or raw.startswith("https://"):
        return raw

    # Absolute local paths won't work in the browser unless they're under /static/
    if raw.startswith("/") and "/static/" not in raw:
        return None

    if "/static/" in raw:
        rel = raw.split("/static/", 1)[1].lstrip("/")
        return url_for("static", filename=rel)

    return url_for("static", filename=raw.lstrip("/"))


def _fmt_phone(phone: str | None, ext: str | None) -> str:
    phone = (phone or "").strip()
    ext = (ext or "").strip()
    if phone and ext:
        return f"{phone} x{ext}"
    return phone


def _fmt_address(obj) -> str:
    parts = []
    for attr in ("address1", "address2"):
        v = (getattr(obj, attr, "") or "").strip()
        if v:
            parts.append(v)

    city = (getattr(obj, "city", "") or "").strip()
    state = (getattr(obj, "state", "") or "").strip()
    postal = (getattr(obj, "postal_code", "") or "").strip()

    line2 = " ".join([p for p in [city, state] if p]).strip()
    if postal:
        line2 = (line2 + " " + postal).strip() if line2 else postal

    if line2:
        parts.append(line2)

    return ", ".join([p for p in parts if p])


def _safe_or_ilike(model, term: str, fields: list[str]):
    """Build an OR( col ILIKE %term% ) across existing columns only."""
    clauses = []
    for f in fields:
        col = getattr(model, f, None)
        if col is not None:
            try:
                clauses.append(col.ilike(f"%{term}%"))
            except Exception:
                pass
    return or_(*clauses) if clauses else None


# Helper: get first non-empty value from form/args for any alias key
def _get_form_or_args(*keys: str) -> str:
    """Return first non-empty value from request.form/request.args for any of the given keys."""
    for k in keys:
        v = (request.form.get(k) or request.args.get(k) or "").strip()
        if v:
            return v
    return ""


# Helper: Normalize fax cover payload from request.form / request.args
def _fax_cover_payload_from_request(settings: Settings) -> dict:
    """Normalize fax cover payload from request.form / request.args.

    We support multiple historical key names because the edit/print templates and JS have changed a few times.
    """

    # To
    to_name = _get_form_or_args("to_name", "to", "toName", "recipient_name", "recipient")
    to_fax = _get_form_or_args("to_fax", "toFax", "fax", "recipient_fax")
    to_phone = _get_form_or_args("to_phone", "toPhone", "phone", "recipient_phone")
    to_email = _get_form_or_args("to_email", "toEmail", "email", "recipient_email")

    # From defaults
    from_name_default = (settings.business_name or "").strip()
    from_phone_default = (getattr(settings, "phone", "") or "").strip()
    from_email_default = (getattr(settings, "email", "") or "").strip()
    from_fax_default = (getattr(settings, "fax", "") or "").strip()
    case_manager_name_default = (getattr(settings, "responsible_case_manager", "") or "").strip()

    from_name = (_get_form_or_args("from_name", "from", "fromName") or from_name_default).strip()
    from_phone = (_get_form_or_args("from_phone", "fromPhone") or from_phone_default).strip()
    from_fax = (_get_form_or_args("from_fax", "fromFax") or from_fax_default).strip()
    from_email = (_get_form_or_args("from_email", "fromEmail") or from_email_default).strip()
    from_case_manager = (
        _get_form_or_args("from_case_manager", "fromCaseManager", "case_manager") or case_manager_name_default
    ).strip()

    # Meta
    pages = _get_form_or_args("pages", "page_count", "num_pages", "pageCount")
    subject = _get_form_or_args("subject", "re_subject", "re", "regarding")

    # The bottom section has drifted between keys; capture both.
    contents = _get_form_or_args(
        "contents",
        "contents_description",
        "description",
        "description_of_contents",
        "message",
        "body",
    )
    message = _get_form_or_args("message", "note", "notes", "body")

    # If only one was provided, mirror it so templates using either name still populate.
    if contents and not message:
        message = contents
    if message and not contents:
        contents = message

    return {
        "to_name": to_name,
        "to_fax": to_fax,
        "to_phone": to_phone,
        "to_email": to_email,
        "from_name": from_name,
        "from_case_manager": from_case_manager,
        "from_phone": from_phone,
        "from_fax": from_fax,
        "from_email": from_email,
        "pages": pages,
        "subject": subject,
        "contents": contents,
        "message": message,
    }


def _playwright_pdf_from_url(url: str, *, page_size: str = "Letter") -> bytes:
    """Render the given URL to PDF via headless Chromium and return PDF bytes."""

    # Keep timeouts conservative to avoid hanging a request.
    nav_timeout_ms = int(current_app.config.get("PDF_NAV_TIMEOUT_MS", 20_000))
    pdf_timeout_ms = int(current_app.config.get("PDF_RENDER_TIMEOUT_MS", 20_000))

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        context = browser.new_context()
        page = context.new_page()
        page.set_default_navigation_timeout(nav_timeout_ms)
        page.set_default_timeout(nav_timeout_ms)

        # Ensure we only return once the page is fully rendered.
        page.goto(url, wait_until="networkidle")
        # Bump timeout for the render step (some Playwright versions don't accept timeout= on page.pdf)
        page.set_default_timeout(pdf_timeout_ms)
        pdf_bytes = page.pdf(
            format=page_size,
            print_background=True,
            margin={"top": "0.5in", "right": "0.5in", "bottom": "0.5in", "left": "0.5in"},
        )

        context.close()
        browser.close()

    return pdf_bytes


@bp.route("/forms/fax-cover/<int:claim_id>/pdf", methods=["GET", "POST"])
def fax_cover_pdf(claim_id: int):
    """Generate a Fax Cover Sheet PDF (in-memory) via Playwright and return it inline."""

    settings = _ensure_settings()
    claim = Claim.query.get_or_404(claim_id)

    # Use helper to normalize payload
    payload = _fax_cover_payload_from_request(settings)
    _fax_save_to_session(claim.id, payload)
    # IMPORTANT: generate PDF from the real print URL (same HTML as preview),
    # so static assets (logo/CSS) load correctly and PDF matches preview.
    print_url = url_for(
        "main.fax_cover_print",
        claim_id=claim.id,
        _external=True,
        to_name=payload["to_name"],
        to_fax=payload["to_fax"],
        to_phone=payload["to_phone"],
        to_email=payload["to_email"],
        from_name=payload["from_name"],
        from_case_manager=payload["from_case_manager"],
        from_phone=payload["from_phone"],
        from_fax=payload["from_fax"],
        from_email=payload["from_email"],
        pages=payload["pages"],
        subject=payload["subject"],
        message=payload["message"],
        contents=payload["contents"],
    )

    try:
        pdf_bytes = _playwright_pdf_from_url(print_url)
    except Exception as e:
        flash(f"Could not generate PDF: {e}", "error")
        return redirect(url_for("main.fax_cover_edit", claim_id=claim.id))

    filename = f"Fax_Cover_{datetime.now().strftime('%Y-%m-%d')}.pdf"
    resp = make_response(pdf_bytes)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return resp


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
    <a class="list-group-item list-group-item-action" href="{{ url_for('main.forms_fax_cover', reset=1) }}">
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


@bp.route("/api/fax-cover-search")
def api_fax_cover_search():
    """Search across carriers, employers, providers, claimants, and contacts.

    Returns a unified list of results suitable for the fax cover sheet autocomplete.
    """

    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"results": []})

    term = q
    results: list[dict] = []

    def add_result(
        *,
        kind: str,
        display: str,
        name: str = "",
        phone: str = "",
        fax: str = "",
        email: str = "",
    ):
        # Provide stable keys expected by the autocomplete JS.
        # `label` is the human-facing line in the dropdown.
        results.append(
            {
                "kind": kind,
                "type": kind,  # alias
                "kind_label": kind.title(),
                "display": name or display,
                "label": display,
                "name": name or display,
                "phone": phone,
                "fax": fax,
                "email": email,
            }
        )

    # Providers
    try:
        clause = _safe_or_ilike(Provider, term, ["name", "city", "email", "phone"])  # type: ignore[name-defined]
        if clause is not None:
            for p in Provider.query.filter(clause).order_by(Provider.name.asc()).limit(20).all():
                p_name = (getattr(p, "name", "") or "").strip()
                p_spec = (getattr(p, "specialty", "") or "").strip()
                if p_spec:
                    label = f"{p_name} — {p_spec} (Provider)"
                else:
                    label = f"{p_name} (Provider)"
                add_result(
                    kind="provider",
                    display=label,
                    name=p_name,
                    phone=_fmt_phone(getattr(p, "phone", None), getattr(p, "phone_ext", None)),
                    fax=(getattr(p, "fax", "") or "").strip(),
                    email=(getattr(p, "email", "") or "").strip(),
                )
    except Exception:
        pass

    # Employers
    try:
        clause = _safe_or_ilike(Employer, term, ["name", "city", "email", "phone"])  # type: ignore[name-defined]
        if clause is not None:
            for e in Employer.query.filter(clause).order_by(Employer.name.asc()).limit(20).all():
                add_result(
                    kind="employer",
                    display=f"{(getattr(e, 'name', '') or '').strip()} (Employer)",
                    name=(getattr(e, "name", "") or "").strip(),
                    phone=_fmt_phone(getattr(e, "phone", None), getattr(e, "phone_ext", None)),
                    fax=(getattr(e, "fax", "") or "").strip(),
                    email=(getattr(e, "email", "") or "").strip(),
                )
    except Exception:
        pass

    # Carriers
    try:
        clause = _safe_or_ilike(Carrier, term, ["name", "city", "email", "phone"])  # type: ignore[name-defined]
        if clause is not None:
            for c in Carrier.query.filter(clause).order_by(Carrier.name.asc()).limit(20).all():
                add_result(
                    kind="carrier",
                    display=f"{(getattr(c, 'name', '') or '').strip()} (Carrier)",
                    name=(getattr(c, "name", "") or "").strip(),
                    phone=_fmt_phone(getattr(c, "phone", None), getattr(c, "phone_ext", None)),
                    fax=(getattr(c, "fax", "") or "").strip(),
                    email=(getattr(c, "email", "") or "").strip(),
                )
    except Exception:
        pass

    # Claimants (Claims)
    try:
        # Support a couple possible claimant name fields.
        claimant_clause = None
        if hasattr(Claim, "claimant"):
            claimant_clause = Claim.claimant.ilike(f"%{term}%")
            claimant_name_field = "claimant"
        elif hasattr(Claim, "claimant_name"):
            claimant_clause = Claim.claimant_name.ilike(f"%{term}%")
            claimant_name_field = "claimant_name"
        elif hasattr(Claim, "claimant_full_name"):
            claimant_clause = Claim.claimant_full_name.ilike(f"%{term}%")
            claimant_name_field = "claimant_full_name"
        else:
            claimant_name_field = ""

        if claimant_clause is not None:
            for cl in Claim.query.filter(claimant_clause).order_by(Claim.id.desc()).limit(20).all():
                nm = (getattr(cl, claimant_name_field, "") or "").strip() if claimant_name_field else ""
                add_result(
                    kind="claimant",
                    display=f"{nm} (Claimant)",
                    name=nm,
                    phone=_fmt_phone(getattr(cl, "claimant_phone", None), getattr(cl, "claimant_phone_ext", None)),
                    fax="",
                    email=(getattr(cl, "claimant_email", "") or "").strip(),
                )
    except Exception:
        pass

    # Contacts (role + parent entity)
    try:
        role_map = {r.id: (r.label or "").strip() for r in ContactRole.query.all()}

        contact_clause = _safe_or_ilike(Contact, term, ["name", "first_name", "last_name", "email", "phone"])  # type: ignore[name-defined]
        if contact_clause is not None:
            for ct in Contact.query.filter(contact_clause).order_by(Contact.id.desc()).limit(30).all():
                base_name = (getattr(ct, "name", "") or "").strip()
                if not base_name:
                    fn = (getattr(ct, "first_name", "") or "").strip()
                    ln = (getattr(ct, "last_name", "") or "").strip()
                    base_name = (" ".join([fn, ln])).strip()

                role_id = getattr(ct, "contact_role_id", None) or getattr(ct, "role_id", None)
                role_label = role_map.get(role_id, "") if role_id else ""

                parent_type = (getattr(ct, "parent_type", "") or "").strip()
                parent_id = getattr(ct, "parent_id", None)
                parent_display = ""
                try:
                    if parent_type == "carrier" and parent_id:
                        parent = Carrier.query.get(parent_id)
                        parent_display = (getattr(parent, "name", "") or "").strip() if parent else ""
                    elif parent_type == "employer" and parent_id:
                        parent = Employer.query.get(parent_id)
                        parent_display = (getattr(parent, "name", "") or "").strip() if parent else ""
                    elif parent_type == "provider" and parent_id:
                        parent = Provider.query.get(parent_id)
                        parent_display = (getattr(parent, "name", "") or "").strip() if parent else ""
                except Exception:
                    parent_display = ""

                bits = [base_name]
                if role_label:
                    bits.append(role_label)
                display = " — ".join([b for b in bits if b])
                if parent_display:
                    display = f"{display} (Contact) @ {parent_display}"
                else:
                    display = f"{display} (Contact)"

                add_result(
                    kind="contact",
                    display=display,
                    name=base_name,
                    phone=_fmt_phone(getattr(ct, "phone", None), getattr(ct, "phone_ext", None)),
                    fax=(getattr(ct, "fax", "") or "").strip(),
                    email=(getattr(ct, "email", "") or "").strip(),
                )
    except Exception:
        pass

    # De-dupe by label (or display) + kind
    seen = set()
    deduped = []
    for r in results:
        key = (r.get("kind"), r.get("label") or r.get("display"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    return jsonify({"results": deduped[:50]})





# New Fax Cover Sheet routes using Jinja templates

@bp.route("/forms/fax-cover", methods=["GET", "POST"])
def forms_fax_cover():
    """Standalone Fax Cover Sheet (no claim required)."""

    settings = _ensure_settings()

    # If opened from the Forms page (reset=1), start fresh.
    if request.method == "GET" and (request.args.get("reset") in ("1", "true", "yes")):
        session.pop(_fax_session_key(None), None)

    # Defaults from Settings
    from_name_default = (settings.business_name or "").strip()
    from_phone_default = (getattr(settings, "phone", "") or "").strip()
    from_email_default = (getattr(settings, "email", "") or "").strip()
    from_fax_default = (getattr(settings, "fax", "") or "").strip()
    case_manager_name_default = (getattr(settings, "responsible_case_manager", "") or "").strip()

    # On POST, persist draft values to session so "Print/PDF" flows don't lose work.
    if request.method == "POST":
        payload = _fax_cover_payload_from_request(settings)
        _fax_save_to_session(None, payload)
        flash("Fax cover sheet draft saved.", "success")
        return redirect(url_for("main.forms_fax_cover"))

    saved = _fax_load_from_session(None)

    form = {
        "to_name": saved.get("to_name", "").strip(),
        "to_fax": saved.get("to_fax", "").strip(),
        "to_phone": saved.get("to_phone", "").strip(),
        "to_email": saved.get("to_email", "").strip(),
        "from_name": (saved.get("from_name") or from_name_default).strip(),
        "from_phone": (saved.get("from_phone") or from_phone_default).strip(),
        "from_fax": (saved.get("from_fax") or from_fax_default).strip(),
        "from_email": (saved.get("from_email") or from_email_default).strip(),
        "from_case_manager": (saved.get("from_case_manager") or case_manager_name_default).strip(),
        "pages": saved.get("pages", "").strip(),
        "subject": saved.get("subject", "").strip(),
        # Templates drift between message/contents; keep both populated.
        "message": (saved.get("message") or saved.get("contents") or "").strip(),
    }

    return render_template(
        "forms/fax_cover_edit.html",
        active_page="forms",
        claim=None,
        settings=settings,
        form=form,
        today=_now_local(settings).strftime("%m/%d/%Y"),
        logo_url=_settings_logo_url(settings),
        search_url=url_for("main.api_fax_cover_search"),
        pdf_url=url_for("main.fax_cover_pdf_standalone"),
        print_url=url_for("main.fax_cover_print_standalone"),
    )


# Standalone HTML print route for Fax Cover Sheet (no claim)
@bp.route("/forms/fax-cover/print", methods=["GET", "POST"])
def fax_cover_print_standalone():
    """Standalone Fax Cover Sheet HTML print view (no claim)."""

    settings = _ensure_settings()

    payload = _fax_cover_payload_from_request(settings)
    _fax_save_to_session(None, payload)

    return render_template(
        "forms/fax_cover_print.html",
        active_page="forms",
        claim=None,
        settings=settings,
        logo_url=_settings_logo_url(settings),
        now=_now_local(settings).strftime("%m/%d/%Y %I:%M %p"),
        to_name=payload["to_name"],
        to_fax=payload["to_fax"],
        to_phone=payload["to_phone"],
        to_email=payload["to_email"],
        from_name=payload["from_name"],
        from_case_manager=payload["from_case_manager"],
        from_phone=payload["from_phone"],
        from_fax=payload["from_fax"],
        from_email=payload["from_email"],
        pages=payload["pages"],
        subject=payload["subject"],
        contents=payload["contents"],
        message=payload["message"],
        to=payload["to_name"],
    )


# Standalone (no-claim) Fax Cover Sheet PDF route
@bp.route("/forms/fax-cover/pdf", methods=["GET", "POST"])
def fax_cover_pdf_standalone():
    """Generate a standalone Fax Cover Sheet PDF (no claim) via Playwright and return it inline."""
    settings = _ensure_settings()

    payload = _fax_cover_payload_from_request(settings)
    _fax_save_to_session(None, payload)

    # IMPORTANT: generate PDF from the real print URL (same HTML as preview)
    print_url = url_for(
        "main.fax_cover_print_standalone",
        _external=True,
        to_name=payload["to_name"],
        to_fax=payload["to_fax"],
        to_phone=payload["to_phone"],
        to_email=payload["to_email"],
        from_name=payload["from_name"],
        from_case_manager=payload["from_case_manager"],
        from_phone=payload["from_phone"],
        from_fax=payload["from_fax"],
        from_email=payload["from_email"],
        pages=payload["pages"],
        subject=payload["subject"],
        message=payload["message"],
        contents=payload["contents"],
    )

    try:
        pdf_bytes = _playwright_pdf_from_url(print_url)
    except Exception as e:
        flash(f"Could not generate PDF: {e}", "error")
        return redirect(url_for("main.forms_fax_cover"))

    filename = f"Fax_Cover_{datetime.now().strftime('%Y-%m-%d')}.pdf"
    resp = make_response(pdf_bytes)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return resp


@bp.route("/forms/fax-cover/<int:claim_id>", methods=["GET", "POST"])
def fax_cover_edit(claim_id: int):
    """Edit screen for the fax cover sheet (persists draft values in session)."""

    settings = _ensure_settings()
    claim = Claim.query.get_or_404(claim_id)

    # On POST, persist draft values so users can Print/PDF and return without losing work.
    if request.method == "POST":
        payload = _fax_cover_payload_from_request(settings)
        _fax_save_to_session(claim.id, payload)
        flash("Fax cover sheet draft saved.", "success")
        return redirect(url_for("main.fax_cover_edit", claim_id=claim.id))

    saved = _fax_load_from_session(claim.id)

    # Defaults from Settings
    from_name_default = (settings.business_name or "").strip()
    from_phone_default = (getattr(settings, "phone", "") or "").strip()
    from_email_default = (getattr(settings, "email", "") or "").strip()
    from_fax_default = (getattr(settings, "fax", "") or "").strip()
    case_manager_name_default = (getattr(settings, "responsible_case_manager", "") or "").strip()

    # Form values. Default to saved session draft on GET.
    form = {
        "to_name": saved.get("to_name", "").strip(),
        "to_fax": saved.get("to_fax", "").strip(),
        "to_phone": saved.get("to_phone", "").strip(),
        "to_email": saved.get("to_email", "").strip(),
        "from_name": (saved.get("from_name") or from_name_default).strip(),
        "from_phone": (saved.get("from_phone") or from_phone_default).strip(),
        "from_fax": (saved.get("from_fax") or from_fax_default).strip(),
        "from_email": (saved.get("from_email") or from_email_default).strip(),
        "from_case_manager": (saved.get("from_case_manager") or case_manager_name_default).strip(),
        "pages": saved.get("pages", "").strip(),
        "subject": saved.get("subject", "").strip(),
        # Templates drift between message/contents; keep both populated.
        "message": (saved.get("message") or saved.get("contents") or "").strip(),
    }

    return render_template(
        "forms/fax_cover_edit.html",
        active_page="forms",
        claim=claim,
        settings=settings,
        form=form,
        today=_now_local(settings).strftime("%m/%d/%Y"),
        logo_url=_settings_logo_url(settings),
        search_url=url_for("main.api_fax_cover_search"),
        pdf_url=url_for("main.fax_cover_pdf", claim_id=claim.id),
        print_url=url_for("main.fax_cover_print", claim_id=claim.id),
    )


@bp.route("/forms/fax-cover/<int:claim_id>/print", methods=["GET", "POST"])
def fax_cover_print(claim_id: int):
    """HTML print view for the fax cover sheet (used for browser print)."""

    settings = _ensure_settings()
    claim = Claim.query.get_or_404(claim_id)

    payload = _fax_cover_payload_from_request(settings)
    _fax_save_to_session(claim.id, payload)

    return render_template(
        "forms/fax_cover_print.html",
        active_page="forms",
        claim=claim,
        settings=settings,
        logo_url=_settings_logo_url(settings),
        now=_now_local(settings).strftime("%m/%d/%Y %I:%M %p"),
        to_name=payload["to_name"],
        to_fax=payload["to_fax"],
        to_phone=payload["to_phone"],
        to_email=payload["to_email"],
        from_name=payload["from_name"],
        from_case_manager=payload["from_case_manager"],
        from_phone=payload["from_phone"],
        from_fax=payload["from_fax"],
        from_email=payload["from_email"],
        pages=payload["pages"],
        subject=payload["subject"],
        contents=payload["contents"],
        message=payload["message"],
        to=payload["to_name"],
    )