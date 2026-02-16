import argparse
from datetime import timedelta
import random
import json
from faker import Faker

from app import create_app, db
from app.models import (
    Carrier, Employer, Provider, Contact, ContactRole,
    Claim, Report, ReportApprovedProvider,
    BillableItem, Invoice, Payment,
    Settings, BarrierOption, BillingActivityCode,
    DocumentArtifact, ReportDocument, ClaimDocument
)

fake = Faker()

# Helper functions for local date/time
from datetime import date, datetime

def _today_local():
    return date.today()

def _now_local():
    return datetime.now()

# ============================================================
#  WIPE (FK-safe order)
# ============================================================

def wipe_domain_data():
    print("⚠️  Wiping domain data in FK-safe order...")
    db.session.query(Payment).delete()
    db.session.query(DocumentArtifact).delete()
    db.session.query(ReportDocument).delete()
    db.session.query(ClaimDocument).delete()
    db.session.query(BillableItem).delete()
    db.session.query(Invoice).delete()
    db.session.query(ReportApprovedProvider).delete()
    db.session.query(Report).delete()
    db.session.query(Claim).delete()
    db.session.query(Contact).delete()
    db.session.query(Provider).delete()
    db.session.query(Employer).delete()
    db.session.query(Carrier).delete()
    db.session.query(BarrierOption).delete()
    db.session.query(BillingActivityCode).delete()
    db.session.query(ContactRole).delete()
    db.session.query(Settings).delete()
    db.session.commit()
    print("✅ Domain data wiped.")

# ============================================================
#  SEED HELPERS
# ============================================================

def seed_settings():
    s = Settings(
        business_name="Acme Medical Management",
        address1="1234 Main St.",
        address2="Suite 100",
        city="Metropolis",
        state="NY",
        postal_code="10001",
        phone="212-555-1234",
        fax="212-555-5678",
        email="info@acmemed.com",
        hourly_rate=125.00,
        telephonic_rate=95.00,
        mileage_rate=0.655,
        payment_terms_default=30,
        target_min_hours_per_week=35,
        target_max_hours_per_week=45,
        report_footer_text="This is a demo report footer for Acme Medical Management.",
        invoice_footer_text="Thank you for your business! Demo invoice footer.",
        responsible_case_manager="Jane Doe, RN"
    )
    db.session.add(s)
    db.session.commit()
    return s

def seed_contact_roles():
    roles = [
        "Adjuster",
        "Nurse Case Manager",
        "Billing",
        "Office"
    ]
    contact_roles = []
    for role_name in roles:
        cr = ContactRole(name=role_name)
        db.session.add(cr)
        contact_roles.append(cr)
    db.session.commit()
    return contact_roles

def seed_billing_activity_codes():
    codes = [
        ("REP", "Report Preparation"),
        ("TC", "Telephone Contact"),
        ("Email", "Email Correspondence"),
        ("Travel", "Travel Time"),
        ("MR", "Medical Record Review"),
        ("LTR", "Letter Writing"),
        ("FR", "File Review"),
        ("MIL", "Mileage"),
        ("EXP", "Expenses")
    ]
    bac_list = []
    for code, desc in codes:
        bac = BillingActivityCode(code=code, label=desc)
        db.session.add(bac)
        bac_list.append(bac)
    db.session.commit()
    return bac_list

def seed_barrier_options():
    barrier_labels = [
        "Language Barrier",
        "Transportation Issues",
        "Cognitive Limitations",
        "Financial Difficulty",
        "Lack of Insurance",
        "Work Restrictions",
        "Family Obligations",
        "Mental Health Concerns",
        "Physical Disability",
        "Cultural Differences"
    ]
    barriers = []
    for label in barrier_labels:
        b = BarrierOption(label=label)
        db.session.add(b)
        barriers.append(b)
    db.session.commit()
    return barriers

def seed_carriers(n=3):
    carriers = []
    for _ in range(n):
        c = Carrier(
            name=fake.company(),
            address1=fake.street_address(),
            address2=fake.secondary_address(),
            city=fake.city(),
            state=fake.state_abbr(),
            postal_code=fake.postcode(),
            phone=fake.phone_number(),
            fax=fake.phone_number(),
            email=fake.company_email(),
            hourly_rate=round(random.uniform(90, 150), 2),
            telephonic_rate=round(random.uniform(75, 120), 2),
            mileage_rate=0.655
        )
        db.session.add(c)
        carriers.append(c)
    db.session.commit()
    return carriers

def seed_employers(carriers, n=5):
    employers = []
    for _ in range(n):
        carrier = random.choice(carriers)
        e = Employer(
            name=fake.company(),
            carrier=carrier,
            address1=fake.street_address(),
            address2=fake.secondary_address(),
            city=fake.city(),
            state=fake.state_abbr(),
            postal_code=fake.postcode(),
            phone=fake.phone_number(),
            fax=fake.phone_number(),
            email=fake.company_email()
        )
        db.session.add(e)
        employers.append(e)
    db.session.commit()
    # Add contacts for employers
    for employer in employers:
        num_contacts = random.randint(1, 3)
        roles = ContactRole.query.filter(ContactRole.name.in_(["Adjuster", "Billing", "Office"])).all()
        for i in range(num_contacts):
            role = roles[i % len(roles)] if roles else None
            contact = Contact(
                employer=employer,
                name=fake.name(),
                phone=fake.phone_number(),
                email=fake.email(),
                contact_role=role
            )
            db.session.add(contact)
    db.session.commit()
    return employers

def seed_providers(n=6):
    specialties = [
        "Orthopedics",
        "Neurology",
        "Physical Therapy",
        "Occupational Therapy",
        "Pain Management",
        "Chiropractic",
        "Psychology",
        "Radiology"
    ]
    providers = []
    for _ in range(n):
        p = Provider(
            name=f"Dr. {fake.last_name()}",
            organization=fake.company(),
            specialty=random.choice(specialties),
            address1=fake.street_address(),
            address2=fake.secondary_address(),
            city=fake.city(),
            state=fake.state_abbr(),
            postal_code=fake.postcode(),
            phone=fake.phone_number(),
            fax=fake.phone_number(),
            email=fake.email(),
            notes=fake.paragraph(nb_sentences=3)
        )
        db.session.add(p)
        providers.append(p)
    db.session.commit()
    # Add 1-3 contacts per provider
    roles = ContactRole.query.filter(ContactRole.name.in_(["Billing", "Office"])).all()
    for provider in providers:
        num_contacts = random.randint(1, 3)
        for i in range(num_contacts):
            role = roles[i % len(roles)] if roles else None
            contact = Contact(
                provider=provider,
                name=fake.name(),
                phone=fake.phone_number(),
                email=fake.email(),
                contact_role=role
            )
            db.session.add(contact)
    db.session.commit()
    return providers

def seed_claims(carriers, employers, n=10):
    injured_body_parts = [
        "Low Back", "Left Knee", "Right Shoulder", "Cervical Spine",
        "Ankle", "Wrist", "Hip", "Elbow", "Neck", "Hand"
    ]
    status_choices = ["Open", "Closed", "Pending"]
    claims = []
    today = _today_local()
    for _ in range(n):
        first = fake.first_name()
        last = fake.last_name()
        dob = fake.date_of_birth(minimum_age=19, maximum_age=62)
        doi_offset_days = random.randint(0, 540)
        doi = today - timedelta(days=doi_offset_days)
        surgery_date = None
        if random.choice([True, False]):
            surgery_days = random.randint(7, 180)
            surgery_date = doi + timedelta(days=surgery_days)
        claimant_address1 = fake.street_address()
        claimant_address2 = fake.secondary_address()
        claimant_city = fake.city()
        claimant_state = fake.state_abbr()
        claimant_postal_code = fake.postcode()
        claimant_phone = fake.phone_number()
        claimant_email = fake.email()
        claim = Claim(
            claimant_name=f"{first} {last}",
            claimant_first_name=first,
            claimant_last_name=last,
            claim_number=f"WC-{fake.random_number(digits=6, fix_len=True)}",
            dob=dob,
            doi=doi,
            injured_body_part=random.choice(injured_body_parts),
            surgery_date=surgery_date,
            claim_state=fake.state_abbr(),
            claimant_address1=claimant_address1,
            claimant_address2=claimant_address2,
            claimant_city=claimant_city,
            claimant_state=claimant_state,
            claimant_postal_code=claimant_postal_code,
            claimant_phone=claimant_phone,
            claimant_email=claimant_email,
            is_telephonic=random.choice([True, False]),
            primary_care_provider=f"Dr. {fake.last_name()}",
            carrier=random.choice(carriers),
            employer=random.choice(employers),
            status=random.choice(status_choices),
            is_closed=False
        )
        if claim.status == "Closed":
            claim.is_closed = True
        db.session.add(claim)
        claims.append(claim)
    db.session.commit()
    return claims

def seed_reports(claims, providers, barriers):
    reports = []
    for claim in claims:
        base_start = claim.doi or (_today_local() - timedelta(days=90))
        treating_providers = random.sample(providers, k=random.randint(1, 2))
        barrier_ids = random.sample([b.id for b in barriers], k=random.randint(1, min(4, len(barriers))))
        # Initial report
        init_start = base_start
        init_end = init_start + timedelta(days=7)
        referral_date = init_start - timedelta(days=random.randint(1, 14))
        next_report_due = init_end + timedelta(days=14)
        r_init = Report(
            claim=claim,
            report_type="initial",
            referral_date=referral_date,
            dos_start=init_start,
            dos_end=init_end,
            next_report_due=next_report_due,
            treating_provider_id=random.choice(treating_providers).id,
            initial_diagnosis=fake.sentence(nb_words=6),
            initial_mechanism_of_injury=fake.paragraph(nb_sentences=2),
            status_treatment_plan=fake.paragraph(nb_sentences=2),
            work_status=fake.sentence(nb_words=8),
            case_management_plan=fake.paragraph(nb_sentences=2),
            barriers_json=json.dumps(barrier_ids)
        )
        db.session.add(r_init)
        db.session.flush()
        # Approved provider link
        for tp in treating_providers:
            approved_link = ReportApprovedProvider(report=r_init, provider=tp)
            db.session.add(approved_link)
        reports.append(r_init)
        # Progress reports 2-5
        n_progress = random.randint(2, 5)
        for i in range(n_progress):
            prog_start = init_end + timedelta(days=7 + i*14)
            prog_end = prog_start + timedelta(days=7)
            next_due = prog_end + timedelta(days=14)
            treating_provider = random.choice(treating_providers)
            r_prog = Report(
                claim=claim,
                report_type="progress",
                referral_date=prog_start - timedelta(days=random.randint(1, 7)),
                dos_start=prog_start,
                dos_end=prog_end,
                next_report_due=next_due,
                treating_provider_id=treating_provider.id,
                initial_diagnosis=fake.sentence(nb_words=6),
                initial_mechanism_of_injury=fake.paragraph(nb_sentences=2),
                status_treatment_plan=fake.paragraph(nb_sentences=2),
                work_status=fake.sentence(nb_words=8),
                case_management_plan=fake.paragraph(nb_sentences=2),
                barriers_json=json.dumps(random.sample(barrier_ids, k=random.randint(1, len(barrier_ids))))
            )
            db.session.add(r_prog)
            db.session.flush()
            approved_link = ReportApprovedProvider(report=r_prog, provider=treating_provider)
            db.session.add(approved_link)
            reports.append(r_prog)
        # Closure report if closed
        if claim.is_closed:
            close_start = init_end + timedelta(days=7 + n_progress*14)
            close_end = close_start + timedelta(days=7)
            next_due = None
            treating_provider = random.choice(treating_providers)
            r_close = Report(
                claim=claim,
                report_type="closure",
                referral_date=close_start - timedelta(days=random.randint(1, 7)),
                dos_start=close_start,
                dos_end=close_end,
                next_report_due=next_due,
                treating_provider_id=treating_provider.id,
                initial_diagnosis=fake.sentence(nb_words=6),
                initial_mechanism_of_injury=fake.paragraph(nb_sentences=2),
                status_treatment_plan=fake.paragraph(nb_sentences=2),
                work_status=fake.sentence(nb_words=8),
                case_management_plan=fake.paragraph(nb_sentences=2),
                barriers_json=json.dumps(random.sample(barrier_ids, k=random.randint(1, len(barrier_ids)))),
                closure_details=fake.paragraph(nb_sentences=3)
            )
            db.session.add(r_close)
            db.session.flush()
            approved_link = ReportApprovedProvider(report=r_close, provider=treating_provider)
            db.session.add(approved_link)
            reports.append(r_close)
    db.session.commit()
    return reports

def seed_billables(claims, reports):
    all_billables = []
    bac_map = {bac.code: bac for bac in BillingActivityCode.query.all()}
    for claim in claims:
        claim_reports = [r for r in reports if r.claim_id == claim.id]
        if not claim_reports:
            continue
        n_bill = random.randint(12, 40)
        for _ in range(n_bill):
            report = random.choice(claim_reports)
            activity_code = random.choice(["REP", "TC", "Email", "Travel", "MR", "LTR", "FR", "MIL", "EXP"])
            bac = bac_map.get(activity_code)
            if activity_code == "Travel":
                quantity = round(random.uniform(5, 120), 1)  # miles
                description = f"Travel for case management ({quantity} miles)"
                notes = fake.sentence()
            elif activity_code in ("TC", "Email"):
                quantity = round(random.uniform(0.25, 0.75), 2)  # hours
                description = f"{activity_code} with parties"
                notes = fake.sentence()
            elif activity_code == "MIL":
                quantity = round(random.uniform(5, 120), 1)  # miles
                description = f"Mileage reimbursement ({quantity} miles)"
                notes = fake.sentence()
            elif activity_code == "EXP":
                quantity = 1
                description = "Miscellaneous expenses"
                notes = fake.sentence()
            else:
                quantity = round(random.uniform(0.25, 2.5), 2)  # hours
                description = f"{activity_code} services"
                notes = fake.sentence()
            # Anchor billable dates within last 12 months (weighted recent)
            today = _today_local()
            roll = random.random()
            if roll < 0.4:
                # Last 30 days (40%)
                date_of_service = today - timedelta(days=random.randint(0, 30))
            elif roll < 0.7:
                # 30–180 days (30%)
                date_of_service = today - timedelta(days=random.randint(31, 180))
            elif roll < 0.9:
                # 180–365 days (20%)
                date_of_service = today - timedelta(days=random.randint(181, 365))
            else:
                # Older than 1 year (10%) for realism
                date_of_service = today - timedelta(days=random.randint(366, 540))
            billable = BillableItem(
                claim=claim,
                report=report,
                date_of_service=date_of_service,
                description=description,
                notes=notes,
                activity_code=activity_code,
                quantity=quantity,
                is_complete=True
            )
            db.session.add(billable)
            all_billables.append(billable)
    db.session.commit()
    return all_billables

def seed_invoices(claims, billables):
    all_invoices = []
    invoice_statuses = ["Draft", "Sent", "Paid"]
    from collections import defaultdict
    claim_billables = defaultdict(list)
    for b in billables:
        claim_billables[b.claim_id].append(b)
    for claim in claims:
        claim_bills = claim_billables.get(claim.id, [])
        if not claim_bills:
            continue
        n_invoices = random.randint(1, 3)
        random.shuffle(claim_bills)
        idx = 0
        for i in range(n_invoices):
            max_n = max(2, len(claim_bills)//n_invoices)
            n_this = random.randint(2, max_n)
            bill_subset = claim_bills[idx:idx+n_this]
            if not bill_subset:
                continue
            dos_start = min(b.date_of_service for b in bill_subset)
            dos_end = max(b.date_of_service for b in bill_subset)
            invoice_date = dos_end + timedelta(days=random.randint(0, 10))
            status = random.choice(invoice_statuses)
            invoice_number = f"INV-{claim.claim_number}-{i+1}"
            invoice = Invoice(
                claim=claim,
                invoice_number=invoice_number,
                status=status,
                invoice_date=invoice_date,
                dos_start=dos_start,
                dos_end=dos_end
            )
            db.session.add(invoice)
            db.session.flush()  # To get invoice.id
            total_amount = 0
            total_hours = 0
            total_miles = 0
            total_expenses = 0
            for b in bill_subset:
                b.invoice_id = invoice.id
                # Calculate line total: quantity * hourly_rate or mileage_rate for Travel/MIL
                activity_code = b.activity_code
                if activity_code in ("Travel", "MIL"):
                    line_total = b.quantity * float(claim.carrier.mileage_rate or 0)
                    total_miles += b.quantity
                elif activity_code == "EXP":
                    line_total = 50.0
                    total_expenses += line_total
                elif activity_code in ("TC", "Email"):
                    line_total = b.quantity * float(claim.carrier.telephonic_rate or 0)
                    total_hours += b.quantity
                else:
                    line_total = b.quantity * float(claim.carrier.hourly_rate or 0)
                    total_hours += b.quantity
                total_amount += line_total
            invoice.total_amount = round(total_amount, 2)
            invoice.total_hours = round(total_hours, 2)
            invoice.total_miles = round(total_miles, 2)
            invoice.total_expenses = round(total_expenses, 2)
            # Create DocumentArtifact stub for invoice_pdf
            doc_artifact = DocumentArtifact(
                claim_id=claim.id,
                invoice_id=invoice.id,
                artifact_type="invoice_pdf",
                download_filename=f"{invoice.invoice_number}.pdf",
                storage_backend="fs",
                stored_path=f"/invoices/{invoice.invoice_number}.pdf",
                content_type="application/pdf",
                file_size_bytes=0
            )
            db.session.add(doc_artifact)
            all_invoices.append(invoice)
            idx += n_this
    db.session.commit()
    return all_invoices

def seed_payments(invoices):
    payment_methods = ["Check", "Credit Card", "EFT", "Cash"]
    payments = []
    for invoice in invoices:
        # Apply payments to some invoices (partial or full)
        if invoice.status == "Paid" or random.random() < 0.5:
            total_due = getattr(invoice, "total_amount", 0) or 0
            if total_due <= 0:
                continue
            # Partial or full payment
            if random.random() < 0.3:
                amount = round(random.uniform(total_due*0.3, total_due), 2)
            else:
                amount = total_due
            pay_date = invoice.invoice_date + timedelta(days=random.randint(0, 30))
            payment = Payment(
                invoice=invoice,
                amount=amount,
                payment_date=pay_date,
                method=random.choice(payment_methods),
                notes=fake.sentence()
            )
            db.session.add(payment)
            payments.append(payment)
    db.session.commit()
    return payments

# ============================================================
#  MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wipe", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        if args.wipe:
            wipe_domain_data()
        if args.seed:
            settings = seed_settings()
            contact_roles = seed_contact_roles()
            billing_activity_codes = seed_billing_activity_codes()
            barrier_options = seed_barrier_options()
            carriers = seed_carriers()
            employers = seed_employers(carriers)
            providers = seed_providers()
            claims = seed_claims(carriers, employers, n=random.randint(8, 12))
            reports = seed_reports(claims, providers, barrier_options)
            billables = seed_billables(claims, reports)
            invoices = seed_invoices(claims, billables)
            payments = seed_payments(invoices)
            print("🌱 Demo data seeded successfully.")
            print(f"Settings: 1")
            print(f"ContactRoles: {len(contact_roles)}")
            print(f"BillingActivityCodes: {len(billing_activity_codes)}")
            print(f"BarrierOptions: {len(barrier_options)}")
            print(f"Carriers: {len(carriers)}")
            print(f"Employers: {len(employers)}")
            print(f"Providers: {len(providers)}")
            print(f"Claims: {len(claims)}")
            print(f"Reports: {len(reports)}")
            print(f"BillableItems: {len(billables)}")
            print(f"Invoices: {len(invoices)}")
            print(f"Payments: {len(payments)}")

if __name__ == "__main__":
    main()