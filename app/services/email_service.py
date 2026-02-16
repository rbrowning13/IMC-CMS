from datetime import datetime, date
import smtplib
import ssl
from email.message import EmailMessage
from flask import current_app


def _format_date(dt):
    if not dt:
        return ""
    if isinstance(dt, (datetime, date)):
        return dt.strftime("%m/%d/%Y")
    return str(dt)


def build_email_context(settings, report=None, invoice=None):
    """
    Build a safe context dictionary for token replacement.
    Handles report-only, invoice-only, or combined scenarios.
    Never assumes optional attributes exist.
    """
    claim = None
    adjuster_name = ""

    if report:
        claim = getattr(report, "claim", None)
        adjuster = getattr(report, "adjuster", None)
        if adjuster:
            adjuster_name = getattr(adjuster, "full_name", "") or ""
    elif invoice:
        claim = getattr(invoice, "claim", None)

    # Safe report values
    report_number = ""
    report_date = ""
    dos_range = ""
    report_pdf_filename = ""

    if report:
        report_number = (
            getattr(report, "display_report_number", None)
            or getattr(report, "id", "")
        )
        report_date = _format_date(getattr(report, "created_at", None))

        dos_start = _format_date(getattr(report, "dos_start", None))
        dos_end = _format_date(getattr(report, "dos_end", None))
        if dos_start or dos_end:
            dos_range = f"{dos_start} - {dos_end}".strip(" -")

        report_pdf_filename = (
            getattr(report, "latest_pdf_filename", None)
            or getattr(report, "pdf_filename", "")
            or ""
        )

    # Safe invoice values
    invoice_number = ""
    invoice_date = ""
    invoice_total = ""
    invoice_pdf_filename = ""

    if invoice:
        invoice_number = getattr(invoice, "invoice_number", "") or ""

        invoice_date = _format_date(
            getattr(invoice, "invoice_date", None)
            or getattr(invoice, "issue_date", None)
        )

        total_value = (
            getattr(invoice, "total", None)
            or getattr(invoice, "total_amount", None)
        )
        if total_value is not None:
            invoice_total = f"${float(total_value):.2f}"

        invoice_pdf_filename = (
            getattr(invoice, "latest_pdf_filename", None)
            or getattr(invoice, "pdf_filename", "")
            or ""
        )

    context = {
        # Claim
        "claimant_first_name": getattr(claim, "claimant_first_name", "") if claim else "",
        "claimant_last_name": getattr(claim, "claimant_last_name", "") if claim else "",
        "claim_number": getattr(claim, "claim_number", "") if claim else "",
        # Report
        "report_type": getattr(report, "report_type", "") if report else "",
        "report_number": report_number,
        "report_date": report_date,
        "dos_range": dos_range,
        "report_pdf_filename": report_pdf_filename,
        # Invoice
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "invoice_total": invoice_total,
        "invoice_pdf_filename": invoice_pdf_filename,
        # Other
        "adjuster_name": adjuster_name,
        "business_name": getattr(settings, "business_name", "") or "",
    }

    return context


def render_email_template(template_text, context):
    """
    Controlled token replacement.
    Supports both {{ token }} and {{token}} styles.
    Leaves unknown tokens untouched.
    """
    if not template_text:
        return ""

    rendered = template_text

    for key, value in context.items():
        value_str = str(value or "")
        rendered = rendered.replace(f"{{{{ {key} }}}}", value_str)
        rendered = rendered.replace(f"{{{{{key}}}}}", value_str)

    return rendered


def render_template_string_safe(template_text, context):
    """
    Backwards-compatible wrapper used by older routes.
    Delegates to render_email_template.
    """
    return render_email_template(template_text, context)


def send_smtp_email(settings, to_email, subject, body, attachments=None):
    """
    Send email using SMTP settings stored in Settings.
    attachments: list of tuples (filename, bytes_data, mimetype)
    """
    if not settings.smtp_host:
        raise ValueError("SMTP host is not configured.")

    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = to_email
    msg["Subject"] = subject

    signature = getattr(settings, "email_signature", None)
    logo_path = getattr(settings, "logo_path", None)

    text_body = body or ""

    if signature:
        text_body = f"{text_body}\n\n{signature}"

    # Always provide plain text fallback
    msg.set_content(text_body)

    # Build optional HTML version (logo always above signature text)
    html_body = None

    if signature or logo_path:
        html_signature_parts = []

        # Logo (always above signature text if present)
        if logo_path:
            try:
                with open(logo_path, "rb") as f:
                    logo_data = f.read()

                msg.add_related(
                    logo_data,
                    maintype="image",
                    subtype="png",
                    cid="companylogo"
                )

                html_signature_parts.append(
                    '<div style="margin-bottom:8px;">'
                    '<img src="cid:companylogo" style="max-height:80px;">'
                    '</div>'
                )
            except Exception:
                pass  # Fail silently if logo can't be loaded

        # Signature text (always below logo)
        if signature:
            html_signature_parts.append(
                f'<div style="white-space:pre-wrap;">{signature}</div>'
            )

        html_signature = "".join(html_signature_parts)

        html_body = f"""
        <html>
          <body style="font-family: Arial, sans-serif; font-size: 14px;">
            <div>{body or ''}</div>
            <br>
            {html_signature}
          </body>
        </html>
        """

    if html_body:
        msg.add_alternative(html_body, subtype="html")

    if attachments:
        for filename, data, mimetype in attachments:
            maintype, subtype = mimetype.split("/", 1)
            msg.add_attachment(
                data,
                maintype=maintype,
                subtype=subtype,
                filename=filename,
            )

    ssl_context = ssl.create_default_context()

    if settings.smtp_encryption == "ssl":
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ssl_context) as server:
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            if settings.smtp_encryption == "tls":
                server.starttls(context=ssl_context)
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(msg)


# Backwards-compatible wrapper used by invoice/report routes.
def send_email_with_attachments(settings, to_email, subject, body, attachments=None):
    """
    Backwards-compatible wrapper used by invoice/report routes.
    Delegates to send_smtp_email.
    """
    return send_smtp_email(
        settings=settings,
        to_email=to_email,
        subject=subject,
        body=body,
        attachments=attachments,
    )
