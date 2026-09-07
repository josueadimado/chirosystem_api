"""Email a CMS-1500 insurance claim to a payer / staff-entered address."""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils.html import escape

from apps.clinic.insurance_claim_pdf import build_cms1500_pdf_bytes, cms1500_pdf_filename

logger = logging.getLogger(__name__)


def send_insurance_claim_email(*, claim: dict, to_email: str, from_email: str | None = None) -> str:
    """
    Send a short cover note plus the CMS-1500 claim as a PDF attachment.
    Returns the recipient address on success; raises on failure.
    """
    recipient = (to_email or "").strip()
    if not recipient or "@" not in recipient:
        raise ValueError("Enter a valid insurance / recipient email address.")

    sender = (from_email or getattr(settings, "DEFAULT_FROM_EMAIL", "") or "").strip()
    if not sender:
        raise ValueError("Clinic outbound email (DEFAULT_FROM_EMAIL) is not configured.")

    patient = escape(claim.get("patient_name") or "Patient")
    invoice = escape(claim.get("invoice_number") or "")
    payer = escape(claim.get("payer_name") or "Insurance")
    subject = f"Insurance claim (CMS-1500) — {claim.get('patient_name') or 'Patient'} — {claim.get('invoice_number') or ''}"

    total = f"${claim.get('total_charge_dollars') or '0'}.{claim.get('total_charge_cents') or '00'}"
    dx = ", ".join(claim.get("diagnosis_codes") or []) or "—"
    pdf_name = cms1500_pdf_filename(claim)

    text = (
        f"Insurance claim (CMS-1500)\n\n"
        f"Payer: {claim.get('payer_name') or '—'}\n"
        f"Patient: {claim.get('patient_name') or '—'}\n"
        f"Invoice / account: {claim.get('invoice_number') or '—'}\n"
        f"Insured ID: {claim.get('insured_id') or '—'}\n"
        f"Diagnoses: {dx}\n"
        f"Total charge: {total}\n"
        f"Billing provider NPI: {claim.get('billing_provider_npi') or '—'}\n\n"
        f"The full CMS-1500 claim form is attached as a PDF ({pdf_name}).\n"
    )

    html = f"""
    <div style="font-family:Arial,sans-serif;font-size:14px;color:#0f172a;line-height:1.45">
      <h2 style="margin:0 0 8px;color:#0d5c2e">CMS-1500 Insurance Claim</h2>
      <p style="margin:0 0 12px">
        Claim for <strong>{patient}</strong> (invoice {invoice}) to <strong>{payer}</strong>.
      </p>
      <p style="margin:0 0 8px">
        <strong>Insured ID:</strong> {escape(claim.get('insured_id') or '—')}<br/>
        <strong>Diagnoses:</strong> {escape(dx)}<br/>
        <strong>Total charge:</strong> {escape(total)}
      </p>
      <p style="margin:12px 0 0;padding:10px 12px;background:#ecfdf5;border:1px solid #a7f3d0;border-radius:8px;">
        <strong>Attached:</strong> Full CMS-1500 claim form PDF
        (<code style="font-size:12px">{escape(pdf_name)}</code>).
        Open the attachment for the complete claim (same layout as the clinic printout).
      </p>
      <p style="color:#64748b;font-size:12px;margin-top:14px">Generated from Relief Chiropractic clinic portal.</p>
    </div>
    """

    try:
        pdf_bytes = build_cms1500_pdf_bytes(claim)
    except Exception:
        logger.exception("CMS-1500 PDF build failed invoice=%s", claim.get("invoice_id"))
        raise ValueError(
            "Could not build the CMS-1500 PDF attachment. Try Print from the portal, or contact support."
        ) from None

    if not pdf_bytes.startswith(b"%PDF"):
        raise ValueError("Could not build a valid CMS-1500 PDF attachment.")

    msg = EmailMultiAlternatives(
        subject=subject.strip(),
        body=text,
        from_email=sender,
        to=[recipient],
    )
    msg.attach_alternative(html, "text/html")
    msg.attach(pdf_name, pdf_bytes, "application/pdf")
    msg.send(fail_silently=False)
    logger.info(
        "Insurance claim emailed invoice=%s to=%s pdf=%s bytes=%s",
        claim.get("invoice_id"),
        recipient,
        pdf_name,
        len(pdf_bytes),
    )
    return recipient
