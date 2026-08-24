"""Email a CMS-1500 insurance claim to a payer / staff-entered address."""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils.html import escape

logger = logging.getLogger(__name__)


def send_insurance_claim_email(*, claim: dict, to_email: str, from_email: str | None = None) -> str:
    """
    Send a plain + HTML summary of the CMS-1500 claim.
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

    lines_txt = []
    for i, line in enumerate(claim.get("service_lines") or [], start=1):
        lines_txt.append(
            f"  {i}. {line.get('date_from')}  POS {line.get('place_of_service')}  "
            f"CPT {line.get('cpt')}  ${line.get('charges_dollars')}.{line.get('charges_cents')}  "
            f"x{line.get('units')}"
        )
    dx = ", ".join(claim.get("diagnosis_codes") or []) or "—"

    text = (
        f"Insurance claim (CMS-1500)\n\n"
        f"Payer: {claim.get('payer_name') or '—'}\n"
        f"Patient: {claim.get('patient_name') or '—'}\n"
        f"Insured ID: {claim.get('insured_id') or '—'}\n"
        f"Invoice / account: {claim.get('invoice_number') or '—'}\n"
        f"Diagnoses: {dx}\n\n"
        f"Service lines:\n" + ("\n".join(lines_txt) or "  (none)") + "\n\n"
        f"Total charge: ${claim.get('total_charge_dollars')}.{claim.get('total_charge_cents')}\n"
        f"Billing provider: {claim.get('billing_provider_name') or '—'}\n"
        f"NPI: {claim.get('billing_provider_npi') or '—'}\n\n"
        f"Please see the attached claim details in this email. "
        f"A printable CMS-1500 is available in the clinic portal.\n"
    )

    rows_html = "".join(
        f"<tr>"
        f"<td>{escape(str(line.get('date_from') or ''))}</td>"
        f"<td>{escape(str(line.get('place_of_service') or ''))}</td>"
        f"<td>{escape(str(line.get('cpt') or ''))}</td>"
        f"<td>{escape(' '.join(line.get('modifiers') or []))}</td>"
        f"<td>${escape(str(line.get('charges_dollars') or '0'))}.{escape(str(line.get('charges_cents') or '00'))}</td>"
        f"<td>{escape(str(line.get('units') or '1'))}</td>"
        f"</tr>"
        for line in (claim.get("service_lines") or [])
    )

    html = f"""
    <div style="font-family:Arial,sans-serif;font-size:14px;color:#0f172a;line-height:1.45">
      <h2 style="margin:0 0 8px;color:#0d5c2e">CMS-1500 Insurance Claim</h2>
      <p style="margin:0 0 12px">Claim for <strong>{patient}</strong> (invoice {invoice}) to <strong>{payer}</strong>.</p>
      <p><strong>Insured ID:</strong> {escape(claim.get('insured_id') or '—')}<br/>
         <strong>Diagnoses:</strong> {escape(dx)}</p>
      <table cellpadding="6" cellspacing="0" border="1" style="border-collapse:collapse;width:100%;font-size:13px">
        <thead style="background:#ecfdf5">
          <tr>
            <th align="left">DOS</th><th align="left">POS</th><th align="left">CPT</th>
            <th align="left">Mod</th><th align="left">Charges</th><th align="left">Units</th>
          </tr>
        </thead>
        <tbody>{rows_html or '<tr><td colspan="6">No service lines</td></tr>'}</tbody>
      </table>
      <p style="margin-top:12px"><strong>Total:</strong>
        ${escape(str(claim.get('total_charge_dollars') or '0'))}.{escape(str(claim.get('total_charge_cents') or '00'))}
      </p>
      <p style="color:#64748b;font-size:12px">Generated from Relief Chiropractic clinic portal.</p>
    </div>
    """

    msg = EmailMultiAlternatives(
        subject=subject.strip(),
        body=text,
        from_email=sender,
        to=[recipient],
    )
    msg.attach_alternative(html, "text/html")
    msg.send(fail_silently=False)
    logger.info("Insurance claim emailed invoice=%s to=%s", claim.get("invoice_id"), recipient)
    return recipient
