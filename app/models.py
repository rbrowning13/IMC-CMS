from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.utils import secure_filename

from . import db
from .models import Claim, ClaimDocument, Report, ReportDocument

bp = Blueprint("routes", __name__)


@bp.route("/claim/<int:claim_id>", methods=["GET", "POST"])
def claim_detail(claim_id):
    claim = Claim.query.get_or_404(claim_id)

    if request.method == "POST":
        # Handle file upload for claim documents
        if "claim_document" in request.files:
            file = request.files["claim_document"]
            if file.filename:
                original_filename = secure_filename(file.filename)
                stored_filename = f"claim_{claim.id}_{original_filename}"
                file.save(f"/path/to/uploads/{stored_filename}")

                description = request.form.get("description")
                category = request.form.get("category")

                doc = ClaimDocument(
                    claim_id=claim.id,
                    filename_original=original_filename,
                    filename_stored=stored_filename,
                    description=description,
                    category=category,
                )

                db.session.add(doc)
                db.session.commit()

                flash("Claim document uploaded successfully.", "success")
                return redirect(url_for("routes.claim_detail", claim_id=claim.id))

    return render_template("claim_detail.html", claim=claim)


@bp.route("/report/<int:report_id>", methods=["GET", "POST"])
def report_detail(report_id):
    report = Report.query.get_or_404(report_id)

    if request.method == "POST":
        # Handle file upload for report documents
        if "report_document" in request.files:
            file = request.files["report_document"]
            if file.filename:
                original_filename = secure_filename(file.filename)
                stored_filename = f"report_{report.id}_{original_filename}"
                file.save(f"/path/to/uploads/{stored_filename}")

                description = request.form.get("description")

                report_doc = ReportDocument(
                    report_id=report.id,
                    filename_original=original_filename,
                    filename_stored=stored_filename,
                    description=description,
                )

                db.session.add(report_doc)
                db.session.commit()

                flash("Report document uploaded successfully.", "success")
                return redirect(url_for("routes.report_detail", report_id=report.id))

    return render_template("report_detail.html", report=report)
