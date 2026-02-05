

from flask import Blueprint, request, jsonify

from app.ai.clarity import ask_clarity

bp = Blueprint("ai", __name__, url_prefix="/ai")


@bp.post("/clarity/query")
def clarity_query():
    """
    Universal Clarity AI endpoint.
    All UI surfaces (navbar, claim, invoice, mobile) should call this.
    """

    payload = request.get_json(silent=True) or {}

    question = payload.get("question")
    if not question:
        return jsonify({
            "error": "Missing 'question' in request payload"
        }), 400

    result = ask_clarity(
        question=question,
        scope=payload.get("scope", "global"),
        claim_id=payload.get("claim_id"),
        invoice_id=payload.get("invoice_id"),
        report_id=payload.get("report_id"),
        page=payload.get("page"),
        mode=payload.get("mode", "auto"),
    )

    return jsonify(result)