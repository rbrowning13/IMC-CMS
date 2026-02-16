from datetime import datetime
from app import create_app
from app.models import Invoice, _now_local
from app import db


def main():
    app = create_app()
    app.app_context().push()

    year_suffix = _now_local().strftime("%y")
    prefix = f"INV-{year_suffix}-"

    invoices = (
        Invoice.query
        .order_by(Invoice.created_at.asc(), Invoice.id.asc())
        .all()
    )

    print(f"Found {len(invoices)} invoices. Renumbering...")

    counter = 1

    for invoice in invoices:
        new_number = f"{prefix}{counter:03d}"

        old_number = invoice.invoice_number
        invoice.invoice_number = new_number

        print(f"{old_number}  →  {new_number}")

        counter += 1

    db.session.commit()
    print("Done.")


if __name__ == "__main__":
    main()