"""Forms / templates routes.

This module is intentionally self-contained and keeps its UI in simple
`render_template_string` blocks so we don't have to maintain separate template
files yet. Later we can move these into proper Jinja templates.
"""

from __future__ import annotations

from datetime import datetime

from flask import flash, redirect, request, render_template_string, url_for, jsonify

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

    def add_result(*, kind: str, display: str, name: str = "", phone: str = "", fax: str = "", email: str = ""):
        results.append(
            {
                "kind": kind,
                "display": display,
                "name": name,
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
                add_result(
                    kind="provider",
                    display=f"{(getattr(p, 'name', '') or '').strip()} (Provider)",
                    name=(getattr(p, "name", "") or "").strip(),
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
                    display = f"{display} (Contact @ {parent_display})"
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

    # De-dupe by display + kind
    seen = set()
    deduped = []
    for r in results:
        key = (r.get("kind"), r.get("display"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    return jsonify({"results": deduped[:50]})


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
    Tip: Use the Search box to pull To details from carriers, employers, providers, claimants, and contacts.
  </p>

  <form method="post" class="mb-4">
    <input type="hidden" name="mode" value="preview">

    <div class="row g-3">
      <div class="col-12">
        <label class="form-label">Search</label>
        <input class="form-control" id="faxSearch" autocomplete="off" placeholder="Start typing a name...">
        <div id="faxSearchResults" class="list-group mt-2" style="max-height: 260px; overflow:auto; display:none;"></div>
        <div class="form-text">Searches carriers, employers, providers, claimants, and contacts.</div>
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
          <div class="col-12">
            <label class="form-label">Email</label>
            <input class="form-control" name="to_email" value="{{ to_email }}">
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
  <script>
    (function() {
      const input = document.getElementById('faxSearch');
      const results = document.getElementById('faxSearchResults');

      const toName = document.querySelector('input[name="to_name"]');
      const toFax = document.querySelector('input[name="to_fax"]');
      const toPhone = document.querySelector('input[name="to_phone"]');
      const toEmail = document.querySelector('input[name="to_email"]');

      let timer = null;

      function hideResults() {
        results.style.display = 'none';
        results.innerHTML = '';
      }

      function showResults(items) {
        results.innerHTML = '';
        if (!items || items.length === 0) {
          hideResults();
          return;
        }

        items.forEach((item) => {
          const a = document.createElement('button');
          a.type = 'button';
          a.className = 'list-group-item list-group-item-action';
          a.textContent = item.display || item.name || '(result)';
          a.addEventListener('click', () => {
            if (item.name && toName) toName.value = item.name;
            if (item.fax && toFax) toFax.value = item.fax;
            if (item.phone && toPhone) toPhone.value = item.phone;
            if (item.email && toEmail) toEmail.value = item.email;
            hideResults();
          });
          results.appendChild(a);
        });

        results.style.display = 'block';
      }

      async function runSearch(q) {
        const url = new URL("{{ url_for('main.api_fax_cover_search') }}", window.location.origin);
        url.searchParams.set('q', q);
        const resp = await fetch(url.toString(), { headers: { 'Accept': 'application/json' } });
        if (!resp.ok) return [];
        const data = await resp.json();
        return (data && data.results) ? data.results : [];
      }

      input.addEventListener('input', () => {
        const q = (input.value || '').trim();
        if (timer) clearTimeout(timer);
        if (q.length < 2) {
          hideResults();
          return;
        }
        timer = setTimeout(async () => {
          try {
            const items = await runSearch(q);
            showResults(items);
          } catch (e) {
            hideResults();
          }
        }, 200);
      });

      document.addEventListener('click', (e) => {
        if (!results.contains(e.target) && e.target !== input) {
          hideResults();
        }
      });
    })();
  </script>
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
        from_name=from_name,
        from_case_manager=from_case_manager,
        from_phone=from_phone,
        from_fax=from_fax,
        from_email=from_email,
        subject=subject,
        pages=pages,
        contents=contents,
    )