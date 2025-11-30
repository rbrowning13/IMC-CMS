"""
SQLAlchemy models for Impact Medical Consulting CMS.

This file defines:
- Core entities: Carrier, Employer, Provider, Contact, Claim
- Documents: ClaimDocument, ReportDocument
- Reports with types (initial/progress/closure) and barriers
- Billable items and invoices
- Settings, BarrierOption, BillingActivityCode

All models are designed to work with a single-user, local Flask app
using SQLite and a filesystem-based document root.
"""

from datetime import datetime, date
from .extensions import db


# ============================================================
#  CONTACT / ENTITY MODELS
# ============================================================

class Carrier(db.Model):
    __tablename__ = "carrier"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)

    phone = db.Column(db.String(50))
    fax = db.Column(db.String(50))
    email = db.Column(db.String(255))
    address = db.Column(db.String(255))
    city = db.Column(db.String(120))
    state = db.Column(db.String(10))
    zip = db.Column(db.String(20))

    claims = db.relationship("Claim", back_populates="carrier")

    def __repr__(self):
        return f"<Carrier {self.name}>"


class Employer(db.Model):
    __tablename__ = "employer"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)

    phone = db.Column(db.String(50))
    email = db.Column(db.String(255))
    address = db.Column(db.String(255))
    city = db.Column(db.String(120))
    state = db.Column(db.String(10))
    zip = db.Column(db.String(20))

    claims = db.relationship("Claim", back_populates="employer")

    def __repr__(self):
        return f"<Employer {self.name}>"


class Provider(db.Model):
    __tablename__ = "provider"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)

    phone = db.Column(db.String(50))
    fax = db.Column(db.String(50))
    email = db.Column(db.String(255))

    # For ICS location
    address = db.Column(db.String(255))
    city = db.Column(db.String(120))
    state = db.Column(db.String(10))
    zip = db.Column(db.String(20))

    # Claims where this provider is the PCP
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

    def __repr__(self):
        return f"<Provider {self.name}>"


class Contact(db.Model):
    """
    Generic contact person (often carrier adjusters, employer contact, etc.).
    Currently simple and free-form; can be linked from other models/routes.
    """
    __tablename__ = "contact"

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(50))
    email = db.Column(db.String(255))

    role = db.Column(db.String(120))  # e.g. "Adjuster", "HR", etc.

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

    # Dates as strings or dates; can be migrated to Date later if desired
    dob = db.Column(db.String(50))            # Date of birth
    date_of_injury = db.Column(db.String(50)) # DOI / DOL

    claim_state = db.Column(db.String(10))    # State code, e.g. "ID"

    carrier_id = db.Column(db.Integer, db.ForeignKey("carrier.id"))
    employer_id = db.Column(db.Integer, db.ForeignKey("employer.id"))

    # Primary Care Provider (PCP) at the claim level
    pcp_provider_id = db.Column(db.Integer, db.ForeignKey("provider.id"))
    pcp_name = db.Column(db.String(255))  # optional text snapshot of PCP name

    # Basic "closed/open" state; can be extended with more statuses later
    is_closed = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(50), default="open")

    carrier = db.relationship("Carrier", back_populates="claims")
    employer = db.relationship("Employer", back_populates="claims")
    pcp_provider = db.relationship("Provider", back_populates="pcp_for_claims")

    documents = db.relationship(
        "ClaimDocument",
        back_populates="claim",
        cascade="all, delete-orphan",
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

    filename_original = db.Column(db.String(255), nullable=False)
    filename_stored = db.Column(db.String(255), nullable=False)

    description = db.Column(db.String(255))
    category = db.Column(db.String(120))

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<ClaimDocument {self.filename_original}>"


class ReportDocument(db.Model):
    __tablename__ = "report_document"

    id = db.Column(db.Integer, primary_key=True)

    report_id = db.Column(db.Integer, db.ForeignKey("report.id"), nullable=False)
    report = db.relationship("Report", back_populates="documents")

    # optional back-link to Claim for convenience / querying
    claim_id = db.Column(db.Integer, db.ForeignKey("claim.id"))
    claim = db.relationship("Claim")

    filename_original = db.Column(db.String(255), nullable=False)
    filename_stored = db.Column(db.String(255), nullable=False)

    description = db.Column(db.String(255))
    category = db.Column(db.String(120))

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<ReportDocument {self.filename_original}>"


# ============================================================
#  BARRIER OPTIONS
# ============================================================

class BarrierOption(db.Model):
    """
    Master list of "Possible Barriers to Recovery" options.
    These are edited from Settings and rendered as multi-select checkboxes
    on report edit screens. Selected IDs are stored on Report.barriers_json.
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
#  REPORT MODEL
# ============================================================

class Report(db.Model):
    """
    Single table for all report types: Initial, Progress, Closure.

    report_type controls which fields are relevant:
    - "initial": includes initial_* fields and next appointment
    - "progress": shares DOS + status/plan/work/case plan + barriers
    - "closure": adds closure_reason / closure_details / closure_case_management_impact
    """
    __tablename__ = "report"

    id = db.Column(db.Integer, primary_key=True)

    claim_id = db.Column(db.Integer, db.ForeignKey("claim.id"), nullable=False)
    claim = db.relationship("Claim", back_populates="reports")

    report_type = db.Column(db.String(50), nullable=False)  # initial/progress/closure

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Shared meta
    referral_date = db.Column(db.Date)         # often from claim/referral
    dos_start = db.Column(db.Date)             # Dates of Service start
    dos_end = db.Column(db.Date)               # Dates of Service end
    next_report_due = db.Column(db.Date)       # For initial/progress

    # Treating provider per report (dropdown from Provider table)
    treating_provider_id = db.Column(db.Integer, db.ForeignKey("provider.id"))
    treating_provider = db.relationship("Provider", back_populates="reports")

    # Shared long-text fields (roll-forward enabled)
    status_treatment_plan = db.Column(db.Text)
    work_status = db.Column(db.Text)
    employment_status = db.Column(db.String(255))  # one-line text
    case_management_plan = db.Column(db.Text)

    # Barriers stored as JSON array of BarrierOption IDs
    barriers_json = db.Column(db.Text)

    # INITIAL REPORT–specific fields
    initial_diagnosis = db.Column(db.Text)
    initial_mechanism_of_injury = db.Column(db.Text)
    initial_coexisting_conditions = db.Column(db.Text)
    initial_surgical_history = db.Column(db.Text)
    initial_medications = db.Column(db.Text)
    initial_diagnostics = db.Column(db.Text)

    # PCP is stored on Claim; here we keep only treating provider and next appt
    initial_next_appt_datetime = db.Column(db.DateTime)
    initial_next_appt_provider_name = db.Column(db.String(255))

    # CLOSURE REPORT–specific fields
    closure_reason = db.Column(db.String(120))  # AMA, Death, MMI, RTW, etc.
    closure_details = db.Column(db.Text)
    closure_case_management_impact = db.Column(db.Text)

    # Report-level documents (including generated PDFs)
    documents = db.relationship(
        "ReportDocument",
        back_populates="report",
        cascade="all, delete-orphan",
    )

    # Billable items auto-created for this report (1.0, 0.5, 0.5 hrs, etc.)
    billables = db.relationship("BillableItem", back_populates="report")

    def __repr__(self):
        return f"<Report {self.report_type} #{self.id} on Claim {self.claim_id}>"


# ============================================================
#  BILLING ACTIVITY CODES
# ============================================================

class BillingActivityCode(db.Model):
    """
    Editable list of billing activities:
    Admin, Email, Exp, Fax, FR, GDL, LTR, MR, MTG, MIL, REP, RR, TC, TCM, Text, Travel, Wait, NO BILL, etc.

    Used to populate dropdowns in the quick-entry Billable Item box and
    other billable forms.
    """
    __tablename__ = "billing_activity_code"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False, unique=True)   # e.g. 'REP', 'TC'
    label = db.Column(db.String(255), nullable=False)              # e.g. 'Report', 'Telephone Call'
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

    # Optional linkage to a specific Report (e.g., auto-created report-writing time)
    report_id = db.Column(db.Integer, db.ForeignKey("report.id"))
    report = db.relationship("Report", back_populates="billables")

    # Optional linkage to an Invoice
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"))

    # When the activity occurred; can be string now, migrated to Date later
    date = db.Column(db.String(50))

    # Free-form description and code
    description = db.Column(db.String(255), nullable=False)
    activity_code = db.Column(db.String(20))  # matches BillingActivityCode.code

    quantity = db.Column(db.Float)  # hours, units, miles, etc.
    rate = db.Column(db.Float)      # hourly or per-unit rate

    category = db.Column(db.String(120))  # optional extra classification

    is_billed = db.Column(db.Boolean, default=False)

    def amount(self) -> float:
        if self.quantity is not None and self.rate is not None:
            return float(self.quantity) * float(self.rate)
        return 0.0

    def __repr__(self):
        return f"<BillableItem {self.description} (${self.amount():.2f})>"


# ============================================================
#  INVOICES
# ============================================================

class Invoice(db.Model):
    __tablename__ = "invoice"

    id = db.Column(db.Integer, primary_key=True)

    claim_id = db.Column(db.Integer, db.ForeignKey("claim.id"), nullable=False)
    claim = db.relationship("Claim", back_populates="invoices")

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Optional DOS range for this invoice, often matching a Report's DOS
    dos_start = db.Column(db.Date)
    dos_end = db.Column(db.Date)

    total_amount = db.Column(db.Float, default=0.0)
    is_paid = db.Column(db.Boolean, default=False)

    notes = db.Column(db.Text)

    billables = db.relationship("BillableItem", backref="invoice", lazy=True)

    def calculate_total(self) -> float:
        self.total_amount = sum(b.amount() for b in self.billables)
        return self.total_amount

    def __repr__(self):
        return f"<Invoice {self.id} – Claim {self.claim_id} – ${self.total_amount:.2f}>"


# ============================================================
#  SETTINGS
# ============================================================

class Settings(db.Model):
    """
    Singleton-style settings row (usually only one record) for
    global configuration and defaults.
    """
    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)

    business_name = db.Column(db.String(255))
    footer_text = db.Column(db.Text)

    # Filesystem root for documents; should be a local path Gina can back up
    documents_root = db.Column(db.String(255))

    # Billing defaults (hours and/or rates can be configured here)
    default_hourly_rate = db.Column(db.Float)
    initial_report_hours = db.Column(db.Float, default=1.0)
    progress_report_hours = db.Column(db.Float, default=0.5)
    closure_report_hours = db.Column(db.Float, default=0.5)

    # Workload targets and claim dormancy
    dormant_claim_days = db.Column(db.Integer)        # days with no activity before "dormant"
    target_min_hours_per_week = db.Column(db.Float)
    target_max_hours_per_week = db.Column(db.Float)

    def __repr__(self):
        return "<Settings>"