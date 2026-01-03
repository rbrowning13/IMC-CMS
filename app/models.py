from datetime import datetime, date


from sqlalchemy.orm import synonym
from sqlalchemy import Numeric


from .extensions import db


# ============================================================
#  HELPER: Phone number formatting with extension
# ============================================================
def _format_phone_with_ext(phone: str | None, ext: str | None) -> str | None:
    """
    Format a phone number with an optional extension.
    Returns None if phone is falsy.
    """
    phone = (phone or "").strip()
    ext = (ext or "").strip()
    if not phone:
        return None
    if not ext:
        return phone
    return f"{phone} ext {ext}"


# ============================================================
#  CONTACT / ENTITY MODELS
# ============================================================

class Carrier(db.Model):
    __tablename__ = "carrier"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)

    # Full mailing info
    address1 = db.Column(db.String(255))
    address2 = db.Column(db.String(255))
    city = db.Column(db.String(120))
    state = db.Column(db.String(10))
    postal_code = db.Column(db.String(20))

    phone = db.Column(db.String(50))
    phone_ext = db.Column(db.String(20))
    fax = db.Column(db.String(50))
    email = db.Column(db.String(255))

    # Carrier-specific billing rates (override Settings when present)
    hourly_rate = db.Column(Numeric(10, 2))
    telephonic_rate = db.Column(Numeric(10, 2))
    mileage_rate = db.Column(Numeric(10, 4))

    @property
    def phone_display(self):
        return _format_phone_with_ext(self.phone, self.phone_ext)

    claims = db.relationship("Claim", back_populates="carrier")
    contacts = db.relationship(
        "Contact",
        backref="carrier",
        lazy=True,
        cascade="all, delete-orphan",
        foreign_keys="Contact.carrier_id",
    )

    def __repr__(self):
        return f"<Carrier {self.name}>"


class Employer(db.Model):
    __tablename__ = "employer"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)

    address1 = db.Column(db.String(255))
    address2 = db.Column(db.String(255))
    city = db.Column(db.String(120))
    state = db.Column(db.String(10))
    postal_code = db.Column(db.String(20))

    phone = db.Column(db.String(50))
    phone_ext = db.Column(db.String(20))
    fax = db.Column(db.String(50))
    email = db.Column(db.String(255))
    carrier_id = db.Column(db.Integer, db.ForeignKey("carrier.id"))
    carrier = db.relationship("Carrier", backref="employers")

    @property
    def phone_display(self):
        return _format_phone_with_ext(self.phone, self.phone_ext)

    claims = db.relationship("Claim", back_populates="employer")
    contacts = db.relationship(
        "Contact",
        backref="employer",
        lazy=True,
        cascade="all, delete-orphan",
        foreign_keys="Contact.employer_id",
    )

    def __repr__(self):
        return f"<Employer {self.name}>"


class Provider(db.Model):
    __tablename__ = "provider"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)

    address1 = db.Column(db.String(255))
    address2 = db.Column(db.String(255))
    city = db.Column(db.String(120))
    state = db.Column(db.String(10))
    postal_code = db.Column(db.String(20))

    phone = db.Column(db.String(50))
    phone_ext = db.Column(db.String(20))
    fax = db.Column(db.String(50))
    email = db.Column(db.String(255))

    # Provider specialty (e.g., Orthopedics, PT, Neurology)
    specialty = db.Column(db.String(255))

    notes = db.Column(db.Text)

    @property
    def phone_display(self):
        return _format_phone_with_ext(self.phone, self.phone_ext)

    # Claims where this provider is the PCP (future use)
    pcp_for_claims = db.relationship(
        "Claim",
        back_populates="pcp_provider",
        foreign_keys="Claim.pcp_provider_id",
    )

    # Reports where this provider is the treating provider
    reports = db.relationship(
        "Report",
        back_populates="treating_provider",
        foreign_keys="Report.treating_provider_id",
    )

    contacts = db.relationship(
        "Contact",
        backref="provider",
        lazy=True,
        cascade="all, delete-orphan",
        foreign_keys="Contact.provider_id",
    )

    def __repr__(self):
        return f"<Provider {self.name}>"


class Contact(db.Model):
    """
    Contact linked to a carrier, employer, or provider.
    """
    __tablename__ = "contact"

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(255), nullable=False)

    # Legacy free-text role (kept for backwards compatibility)
    role = db.Column(db.String(120))

    # Settings-managed role/title dropdown
    contact_role_id = db.Column(db.Integer, db.ForeignKey("contact_role.id"))
    contact_role = db.relationship("ContactRole")

    phone = db.Column(db.String(50))
    phone_ext = db.Column(db.String(20))
    fax = db.Column(db.String(50))
    email = db.Column(db.String(255))
    notes = db.Column(db.Text)

    @property
    def phone_display(self):
        return _format_phone_with_ext(self.phone, self.phone_ext)

    @property
    def role_display(self) -> str | None:
        """Best-effort display value for role/title."""
        if self.contact_role is not None:
            return self.contact_role.label
        return (self.role or "").strip() or None

    carrier_id = db.Column(db.Integer, db.ForeignKey("carrier.id"))
    employer_id = db.Column(db.Integer, db.ForeignKey("employer.id"))
    provider_id = db.Column(db.Integer, db.ForeignKey("provider.id"))

    def __repr__(self):
        return f"<Contact {self.name}>"


# ============================================================
#  CLAIM MODEL
# ============================================================

class Claim(db.Model):
    __tablename__ = "claim"

    id = db.Column(db.Integer, primary_key=True)

    claimant_name = db.Column(db.String(255), nullable=False)
    claim_number = db.Column(db.String(120))

    # Dates as actual Date objects (parsed from HTML date inputs)
    dob = db.Column(db.Date)   # Date of birth
    doi = db.Column(db.Date)   # Date of injury

    # Additional claim-level context shown in claim + report headers
    injured_body_part = db.Column(db.String(255))
    surgery_date = db.Column(db.Date)

    claim_state = db.Column(db.String(10))  # e.g. "ID"

    # Claimant contact info (from existing UI/routes)
    claimant_address1 = db.Column(db.String(255))
    claimant_address2 = db.Column(db.String(255))
    claimant_city = db.Column(db.String(120))
    claimant_state = db.Column(db.String(10))
    claimant_postal_code = db.Column(db.String(20))
    claimant_phone = db.Column(db.String(50))
    claimant_phone_ext = db.Column(db.String(20))
    claimant_email = db.Column(db.String(255))

    @property
    def claimant_phone_display(self):
        return _format_phone_with_ext(self.claimant_phone, self.claimant_phone_ext)

    # Telephonic flag (used in some flows)
    is_telephonic = db.Column(db.Boolean, default=False)

    # PCP as free-text for now (captured on Initial report)
    primary_care_provider = db.Column(db.String(255))

    # Future: PCP as a linked Provider
    pcp_provider_id = db.Column(db.Integer, db.ForeignKey("provider.id"))
    pcp_provider = db.relationship("Provider", back_populates="pcp_for_claims")

    # Carrier / Employer / Adjuster
    carrier_id = db.Column(db.Integer, db.ForeignKey("carrier.id"))
    employer_id = db.Column(db.Integer, db.ForeignKey("employer.id"))
    carrier_contact_id = db.Column(db.Integer, db.ForeignKey("contact.id"))

    # Basic open/closed state
    is_closed = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(50), default="open")

    carrier = db.relationship("Carrier", back_populates="claims")
    employer = db.relationship("Employer", back_populates="claims")
    carrier_contact = db.relationship(
        "Contact",
        foreign_keys=[carrier_contact_id],
    )

    documents = db.relationship(
        "ClaimDocument",
        back_populates="claim",
        cascade="all, delete-orphan",
    )

    artifacts = db.relationship(
        "DocumentArtifact",
        back_populates="claim",
        cascade="all, delete-orphan",
        order_by="DocumentArtifact.created_at",
    )
    reports = db.relationship(
        "Report",
        back_populates="claim",
        cascade="all, delete-orphan",
        order_by="Report.created_at",
    )
    billables = db.relationship(
        "BillableItem",
        back_populates="claim",
        cascade="all, delete-orphan",
    )
    invoices = db.relationship(
        "Invoice",
        back_populates="claim",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<Claim {self.claimant_name} — {self.claim_number}>"


# ============================================================
#  CLAIM & REPORT DOCUMENTS
# ============================================================

class ClaimDocument(db.Model):
    __tablename__ = "claim_document"

    id = db.Column(db.Integer, primary_key=True)
    claim_id = db.Column(db.Integer, db.ForeignKey("claim.id"), nullable=False)
    claim = db.relationship("Claim", back_populates="documents")

    original_filename = db.Column(db.String(255), nullable=False)
    filename_stored = db.Column(db.String(255), nullable=False)

    doc_type = db.Column(db.String(120))
    description = db.Column(db.String(255))
    document_date = db.Column(db.String(50))  # YYYY-MM-DD string is fine for now

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<ClaimDocument {self.original_filename}>"


class ReportDocument(db.Model):
    __tablename__ = "report_document"

    id = db.Column(db.Integer, primary_key=True)

    report_id = db.Column(db.Integer, db.ForeignKey("report.id"), nullable=False)
    report = db.relationship("Report", back_populates="documents")

    # optional back-link to Claim for convenience
    claim_id = db.Column(db.Integer, db.ForeignKey("claim.id"))
    claim = db.relationship("Claim")

    original_filename = db.Column(db.String(255), nullable=False)
    stored_path = db.Column(db.String(512), nullable=False)

    doc_type = db.Column(db.String(120))
    description = db.Column(db.String(255))
    document_date = db.Column(db.String(50))

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<ReportDocument {self.original_filename}>"


# ============================================================
#  PDF / ARTIFACT STORAGE (Reports + Invoices)
# ============================================================

class DocumentArtifact(db.Model):
    """Persisted generated artifacts (PDFs) for reports/invoices.

    Supports either:
      - DB-backed storage (content bytes in `content`), or
      - Filesystem-backed storage (absolute/relative path in `stored_path`).

    We keep a human-friendly `download_filename` so browser downloads are sane.
    """

    __tablename__ = "document_artifact"

    id = db.Column(db.Integer, primary_key=True)

    # Ownership / scoping
    claim_id = db.Column(db.Integer, db.ForeignKey("claim.id"), nullable=False)
    claim = db.relationship("Claim", back_populates="artifacts")

    # Optional links (exactly one is typical, but not enforced at DB level)
    report_id = db.Column(db.Integer, db.ForeignKey("report.id"))
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"))

    # What is this?
    artifact_type = db.Column(db.String(50), nullable=False)  # e.g., "report_pdf", "invoice_pdf"

    # Human / UX
    download_filename = db.Column(db.String(255), nullable=False)

    # Storage
    storage_backend = db.Column(db.String(10), nullable=False, default="db")  # "db" or "fs"
    stored_path = db.Column(db.String(512))  # used when storage_backend == "fs"
    content_type = db.Column(db.String(120), default="application/pdf")
    content = db.Column(db.LargeBinary)  # used when storage_backend == "db"

    # Metadata
    file_size_bytes = db.Column(db.Integer)
    sha256 = db.Column(db.String(64))

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<DocumentArtifact {self.artifact_type} claim={self.claim_id} id={self.id}>"


# ============================================================
#  BARRIER OPTIONS
# ============================================================

class BarrierOption(db.Model):
    """
    Master list of "Possible Barriers to Recovery" options.
    Edited from Settings; rendered as checkboxes in report edit.
    """
    __tablename__ = "barrier_option"

    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(120), nullable=False, default="General")
    label = db.Column(db.String(255), nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)

    def __repr__(self):
        return f"<BarrierOption {self.label}>"


# ============================================================
#  REPORT ↔ PROVIDER (Approved Treating Provider(s))
# ============================================================

class ReportApprovedProvider(db.Model):
    """Association row linking a Report to one approved treating Provider."""
    __tablename__ = "report_approved_provider"

    id = db.Column(db.Integer, primary_key=True)

    report_id = db.Column(db.Integer, db.ForeignKey("report.id"), nullable=False)
    provider_id = db.Column(db.Integer, db.ForeignKey("provider.id"), nullable=False)

    # Optional ordering for display/print (lower = earlier)
    sort_order = db.Column(db.Integer, default=0)

    report = db.relationship("Report", back_populates="approved_provider_links")
    provider = db.relationship("Provider")

    def __repr__(self):
        return f"<ReportApprovedProvider report={self.report_id} provider={self.provider_id}>"

# ============================================================
#  REPORT MODEL
# ============================================================

class Report(db.Model):
    """
    Single table for all report types: initial / progress / closure.
    """
    __tablename__ = "report"

    id = db.Column(db.Integer, primary_key=True)

    claim_id = db.Column(db.Integer, db.ForeignKey("claim.id"), nullable=False)
    claim = db.relationship("Claim", back_populates="reports")

    report_type = db.Column(db.String(50), nullable=False)  # initial/progress/closure

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Shared dates/meta
    referral_date = db.Column(db.Date)
    dos_start = db.Column(db.Date)
    dos_end = db.Column(db.Date)
    next_report_due = db.Column(db.Date)

    # Treating provider per report
    treating_provider_id = db.Column(db.Integer, db.ForeignKey("provider.id"))
    treating_provider = db.relationship("Provider", back_populates="reports")

    # Approved Treating Provider(s) (supports multiple providers per report)
    approved_provider_links = db.relationship(
        "ReportApprovedProvider",
        back_populates="report",
        cascade="all, delete-orphan",
        order_by="ReportApprovedProvider.sort_order",
    )

    @property
    def approved_treating_providers(self):
        """Convenience list of Provider objects in display order."""
        return [link.provider for link in (self.approved_provider_links or [])]

    # Shared long-text fields (roll-forward candidates)
    status_treatment_plan = db.Column(db.Text)
    work_status = db.Column(db.Text)
    employment_status = db.Column(db.String(255))
    case_management_plan = db.Column(db.Text)

    # PCP captured on Initial Report only (free-text)
    primary_care_provider = db.Column(db.String(255))

    # Barriers JSON (list of BarrierOption IDs)
    barriers_json = db.Column(db.Text)

    # INITIAL-specific fields
    initial_diagnosis = db.Column(db.Text)
    initial_mechanism_of_injury = db.Column(db.Text)
    initial_coexisting_conditions = db.Column(db.Text)
    initial_surgical_history = db.Column(db.Text)
    initial_medications = db.Column(db.Text)
    initial_diagnostics = db.Column(db.Text)

    initial_next_appt_datetime = db.Column(db.DateTime)
    initial_next_appt_provider_name = db.Column(db.String(255))

    # CLOSURE-specific fields
    closure_reason = db.Column(db.String(120))
    closure_details = db.Column(db.Text)
    closure_case_management_impact = db.Column(db.Text)

    documents = db.relationship(
        "ReportDocument",
        back_populates="report",
        cascade="all, delete-orphan",
    )

    artifacts = db.relationship(
        "DocumentArtifact",
        primaryjoin="Report.id==foreign(DocumentArtifact.report_id)",
        cascade="all, delete-orphan",
        order_by="DocumentArtifact.created_at",
    )

    billables = db.relationship("BillableItem", back_populates="report")

    def __repr__(self):
        return f"<Report {self.report_type} #{self.id} on Claim {self.claim_id}>"


# ============================================================
#  BILLING ACTIVITY CODES (future editable list)
# ============================================================

class BillingActivityCode(db.Model):
    __tablename__ = "billing_activity_code"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False, unique=True)
    label = db.Column(db.String(255), nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)

    def __repr__(self):
        return f"<BillingActivityCode {self.code}>"


# ============================================================
#  BILLABLE ITEMS
# ============================================================

class BillableItem(db.Model):
    __tablename__ = "billable_item"

    id = db.Column(db.Integer, primary_key=True)

    claim_id = db.Column(db.Integer, db.ForeignKey("claim.id"), nullable=False)
    claim = db.relationship("Claim", back_populates="billables")

    report_id = db.Column(db.Integer, db.ForeignKey("report.id"))
    report = db.relationship("Report", back_populates="billables")

    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"))

    # When the activity occurred
    date_of_service = db.Column(db.Date)

    description = db.Column(db.String(255), nullable=False)
    notes = db.Column(db.Text)
    activity_code = db.Column(db.String(20))  # MIL, EXP, NO BILL, etc.

    quantity = db.Column(db.Float)  # hours, miles, dollars, etc.

    # Completion flag (used by invoice logic)
    is_complete = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<BillableItem {self.description}>"


# ============================================================
#  INVOICES
# ============================================================

class Invoice(db.Model):
    __tablename__ = "invoice"

    id = db.Column(db.Integer, primary_key=True)

    claim_id = db.Column(db.Integer, db.ForeignKey("claim.id"), nullable=False)
    claim = db.relationship("Claim", back_populates="invoices")

    # For reference on the invoice itself
    carrier_id = db.Column(db.Integer, db.ForeignKey("carrier.id"))
    employer_id = db.Column(db.Integer, db.ForeignKey("employer.id"))

    invoice_number = db.Column(db.String(50))
    status = db.Column(db.String(50), default="Draft")  # Draft/Sent/Paid/Void
    invoice_date = db.Column(db.Date)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # DOS range
    dos_start = db.Column(db.Date)
    dos_end = db.Column(db.Date)

    # Totals persisted on the invoice
    total_hours = db.Column(db.Float, default=0.0)
    total_miles = db.Column(db.Float, default=0.0)
    total_expenses = db.Column(db.Float, default=0.0)
    total_amount = db.Column(db.Float, default=0.0)

    notes = db.Column(db.Text)

    items = db.relationship("BillableItem", backref="invoice", lazy=True)

    artifacts = db.relationship(
        "DocumentArtifact",
        primaryjoin="Invoice.id==foreign(DocumentArtifact.invoice_id)",
        cascade="all, delete-orphan",
        order_by="DocumentArtifact.created_at",
    )

    # Payments applied to this invoice (Billing / A/R)
    payments = db.relationship(
        "Payment",
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="Payment.payment_date",
    )

    @property
    def total_paid(self) -> float:
        """Sum of payments applied to this invoice."""
        return float(sum((p.amount or 0) for p in (self.payments or [])))

    @property
    def balance_due(self) -> float:
        """Remaining balance (never below zero unless you allow overpayment intentionally)."""
        return float((self.total_amount or 0) - self.total_paid)

    def __repr__(self):
        return f"<Invoice {self.invoice_number or self.id} – Claim {self.claim_id}>"


# ============================================================
#  PAYMENTS (A/R)
# ============================================================

class Payment(db.Model):
    __tablename__ = "payment"

    id = db.Column(db.Integer, primary_key=True)

    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False)
    invoice = db.relationship("Invoice", back_populates="payments")

    payment_date = db.Column(db.Date, nullable=False, default=date.today)

    # Use Numeric for currency to avoid float rounding issues
    amount = db.Column(Numeric(12, 2), nullable=False)

    # Optional metadata for bookkeeping
    method = db.Column(db.String(50))         # e.g., Check, EFT, ACH, Card, Cash
    reference = db.Column(db.String(120))     # check number / transaction id
    notes = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<Payment {self.amount} on {self.payment_date} for Invoice {self.invoice_id}>"


# ============================================================
#  SETTINGS
# ============================================================

class Settings(db.Model):
    """
    Global configuration and defaults.
    """
    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)

    business_name = db.Column(db.String(255))

    # Business contact info
    address1 = db.Column(db.String(255))
    address2 = db.Column(db.String(255))
    city = db.Column(db.String(120))
    state = db.Column(db.String(10))
    postal_code = db.Column(db.String(20))
    phone = db.Column(db.String(50))
    phone_ext = db.Column(db.String(20))
    fax = db.Column(db.String(50))
    email = db.Column(db.String(255))
    ein = db.Column(db.String(50))
    responsible_case_manager = db.Column(db.String(255))

    @property
    def phone_display(self):
        return _format_phone_with_ext(self.phone, self.phone_ext)

    # Billing rates
    hourly_rate = db.Column(db.Float)
    telephonic_rate = db.Column(db.Float)
    mileage_rate = db.Column(db.Float)

    # Defaults / text
    payment_terms_default = db.Column(db.String(255))

    dormant_claim_days = db.Column(db.Integer)
    target_min_hours_per_week = db.Column(db.Float)
    target_max_hours_per_week = db.Column(db.Float)

    # Branding / appearance
    logo_path = db.Column(db.String(255))  # relative to static/, e.g. "logos/abcd_logo.png"
    signature_path = db.Column(db.String(255))  # relative to static/, e.g. "signatures/abcd_sig.png"
    accent_color = db.Column(db.String(20))
    report_footer_text = db.Column(db.Text)
    invoice_footer_text = db.Column(db.Text)

    # Filesystem root for documents
    documents_root = db.Column(db.String(255))

    # Report billing defaults (for auto billable items)
    initial_report_hours = db.Column(db.Float, default=1.0)
    progress_report_hours = db.Column(db.Float, default=0.5)
    closure_report_hours = db.Column(db.Float, default=0.5)

    contact_roles_json = db.Column(db.Text)

    def __repr__(self):
        return "<Settings>"


# ============================================================
#  CONTACT ROLES (Settings-managed list)
# ============================================================

class ContactRole(db.Model):
    """Editable list of contact roles/titles shown in contact forms."""
    __tablename__ = "contact_role"

    id = db.Column(db.Integer, primary_key=True)

    # DB column (per schema): contact_role.label
    # Examples: "Adjuster", "Nurse Case Manager"
    label = db.Column(db.String(255), nullable=False)

    # Backwards-compatible alias so older code can still do ContactRole(name="...")
    # NOTE: this is a SQLAlchemy synonym (mapped attribute), not a Python @property.
    name = synonym("label")

    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)

    def __repr__(self):
        return f"<ContactRole {self.label}>"

    def __str__(self):
        return self.label