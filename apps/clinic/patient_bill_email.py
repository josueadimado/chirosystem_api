"""Email paid patient bills to patients (HTML statement matching print data)."""

from __future__ import annotations

import html
import logging
import smtplib
import ssl

from django.conf import settings

logger = logging.getLogger(__name__)


class PatientBillEmailError(Exception):
    """Raised when a bill cannot be emailed; message is safe for API clients."""


def _smtp_configured() -> bool:
    return bool((getattr(settings, "EMAIL_HOST", None) or "").strip())


def smtp_mail_status() -> dict:
    """
    Safe summary for Admin (no passwords). Used to diagnose bill-email failures.
    """
    host = (getattr(settings, "EMAIL_HOST", "") or "").strip()
    user = (getattr(settings, "EMAIL_HOST_USER", "") or "").strip()
    password_set = bool((getattr(settings, "EMAIL_HOST_PASSWORD", "") or "").strip())
    from_email = (
        getattr(settings, "PATIENT_BILL_FROM_EMAIL", None)
        or getattr(settings, "DEFAULT_FROM_EMAIL", "")
        or ""
    ).strip()
    port = int(getattr(settings, "EMAIL_PORT", 587) or 587)
    use_tls = bool(getattr(settings, "EMAIL_USE_TLS", True))
    backend = (getattr(settings, "EMAIL_BACKEND", "") or "").strip()

    ready = bool(host and user and password_set)
    summary = "Patient bill email is ready."
    if not host:
        summary = (
            "Email is not configured: EMAIL_HOST is missing on the API server. "
            "Add EMAIL_HOST=smtp.gmail.com (and user/password) in Dokploy / apps/api/.env, then redeploy the API."
        )
    elif not user or not password_set:
        summary = (
            "EMAIL_HOST is set, but EMAIL_HOST_USER or EMAIL_HOST_PASSWORD is missing. "
            "For Gmail use an App Password, not the normal login password."
        )
    elif "console" in backend.lower():
        summary = (
            "Mail is set to console (print-only) mode — bills will not reach patients. "
            "Set EMAIL_HOST so the API uses real SMTP."
        )

    return {
        "ready": ready and "console" not in backend.lower(),
        "summary": summary,
        "checks": [
            {"id": "email_host", "label": "EMAIL_HOST set", "ok": bool(host), "value": host or None},
            {"id": "email_user", "label": "EMAIL_HOST_USER set", "ok": bool(user), "value": user or None},
            {"id": "email_password", "label": "EMAIL_HOST_PASSWORD set", "ok": password_set, "value": None},
            {"id": "from_email", "label": "Bill From address", "ok": bool(from_email), "value": from_email or None},
            {
                "id": "port_tls",
                "label": f"Port {port}, TLS={'on' if use_tls else 'off'}",
                "ok": True,
                "value": f"{port}/tls={use_tls}",
            },
            {
                "id": "backend",
                "label": "Django email backend",
                "ok": "console" not in backend.lower(),
                "value": backend.split(".")[-1] if backend else None,
            },
        ],
    }


def _friendly_smtp_error(exc: BaseException) -> str:
    """Turn SMTP exceptions into staff-readable guidance."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if "authentication" in text or "username and password" in text or "534" in text or "535" in text:
        return (
            "Gmail/SMTP rejected the login. For Gmail, use an App Password "
            "(Google Account → Security → App passwords), set EMAIL_HOST_USER and EMAIL_HOST_PASSWORD "
            "on the API, and redeploy."
        )
    if "connection refused" in text or "timed out" in text or "name or service not known" in text:
        return (
            "Could not connect to the mail server. Check EMAIL_HOST (e.g. smtp.gmail.com) "
            "and that the server can reach the internet on port 587."
        )
    if "sender" in text or ("from" in text and ("not allowed" in text or "denied" in text)):
        return (
            "The From address was rejected. Set PATIENT_BILL_FROM_EMAIL to the same address as "
            "EMAIL_HOST_USER (your Gmail), then redeploy."
        )
    if isinstance(exc, (smtplib.SMTPException, ssl.SSLError, OSError, TimeoutError)):
        return (
            f"Mail server error ({type(exc).__name__}). "
            "Check EMAIL_HOST / App Password on the API, or see Admin → Errors for details."
        )
    return (
        "Could not send the email. Check EMAIL_HOST, EMAIL_HOST_USER, and EMAIL_HOST_PASSWORD "
        "on the API server, then try again."
    )


def _money_label(value: str | None) -> str:
    v = (value or "").strip()
    if not v:
        return "$0.00"
    return v if v.startswith("$") else f"${v}"


def build_patient_bill_email_html(bill: dict) -> str:
    """Email-safe HTML patient bill (table layout, inline styles)."""
    clinic = html.escape(bill.get("clinic_name") or "Relief Chiropractic")
    clinic_street = html.escape((bill.get("address_line1") or "").strip())
    clinic_csz = html.escape((bill.get("city_state_zip") or "").strip())
    clinic_phone = html.escape((bill.get("phone") or "").strip())
    clinic_email = html.escape((bill.get("email") or "").strip())
    office_employer_id = html.escape(
        (bill.get("office_employer_id") or bill.get("employer_tax_id") or "").strip()
    )
    provider_npi = html.escape(
        (bill.get("provider_npi") or bill.get("provider_billing_id") or "").strip()
    )
    inv_no = html.escape(bill.get("invoice_number") or "")
    patient = html.escape(bill.get("patient_name") or "")
    addr = html.escape(bill.get("patient_address") or "")
    diagnosis = html.escape((bill.get("diagnosis") or "—").strip() or "—")
    dos = html.escape(bill.get("date_of_service") or "")
    billing_date = html.escape(bill.get("billing_date_display") or dos)
    stmt_date = html.escape(bill.get("statement_date_display") or "")
    provider = html.escape(bill.get("provider_name") or "")
    cred = html.escape(bill.get("provider_credential") or "")

    line_rows = []
    for line in bill.get("lines") or []:
        line_rows.append(
            "<tr>"
            f"<td style=\"padding:6px 8px;border-bottom:1px solid #e2e8f0;\">{html.escape(line.get('cpt_code') or '')}</td>"
            f"<td style=\"padding:6px 8px;border-bottom:1px solid #e2e8f0;\">{html.escape(line.get('description') or '')}</td>"
            f"<td style=\"padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:right;\">{_money_label(line.get('fees'))}</td>"
            f"<td style=\"padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:center;\">{html.escape(line.get('units') or '')}</td>"
            "</tr>"
        )
    lines_html = "".join(line_rows) or (
        "<tr><td colspan=\"4\" style=\"padding:8px;color:#64748b;\">No line items</td></tr>"
    )

    totals_rows = [
        ("Bill charges", bill.get("bill_charges_total") or bill.get("subtotal")),
        ("Patient payments (clinic charge)", bill.get("patient_charge_total") or bill.get("total_amount")),
        ("Payments received", bill.get("payments_received_total")),
        ("Remaining client responsibility", bill.get("remaining_client_responsibility_total")),
    ]
    totals_html = "".join(
        f"<tr><td style=\"padding:4px 8px;color:#475569;\">{html.escape(label)}</td>"
        f"<td style=\"padding:4px 8px;text-align:right;font-weight:600;\">{_money_label(str(val) if val is not None else '')}</td></tr>"
        for label, val in totals_rows
    )

    clinic_address_lines = "<br/>".join(part for part in (clinic_street, clinic_csz) if part)
    provider_bits = []
    if provider:
        provider_bits.append(provider + (f", {cred}" if cred else ""))
    if clinic_address_lines:
        provider_bits.append(f"Office: {clinic_street}{', ' + clinic_csz if clinic_csz else ''}")
    provider_bits.append(f"Provider/Office Employer ID#: {office_employer_id or '—'}")
    provider_bits.append(f"NPI: {provider_npi or '—'}")
    provider_block = (
        "<p style=\"margin:12px 0 0;font-size:12px;font-weight:700;color:#0f766e;"
        "text-transform:uppercase;letter-spacing:0.06em;\">Provider / clinic</p>"
        + "".join(
            f"<p style=\"margin:4px 0 0;font-size:13px;color:#334155;\">{bit}</p>"
            for bit in provider_bits
        )
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"/><title>Patient Bill {inv_no}</title></head>
<body style="margin:0;padding:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f8fafc;color:#0f172a;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;padding:24px 12px;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;">
        <tr><td style="background:#0f766e;padding:20px 24px;color:#ffffff;">
          <p style="margin:0;font-size:12px;letter-spacing:0.08em;text-transform:uppercase;opacity:0.9;">Patient bill / receipt</p>
          <h1 style="margin:8px 0 0;font-size:22px;font-weight:700;">{clinic}</h1>
          <p style="margin:10px 0 0;font-size:13px;line-height:1.45;opacity:0.95;">
            {clinic_address_lines or "—"}
            {f"<br/>{clinic_phone}" if clinic_phone else ""}
            {f"<br/>{clinic_email}" if clinic_email else ""}
          </p>
        </td></tr>
        <tr><td style="padding:24px;">
          <p style="margin:0 0 16px;font-size:14px;line-height:1.5;color:#334155;">
            Hello {patient},<br/><br/>
            Thank you for your visit. Attached below is your paid statement for your records.
          </p>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;font-size:13px;">
            <tr><td style="padding:4px 0;color:#64748b;width:180px;">Invoice</td><td style="padding:4px 0;font-weight:600;">{inv_no}</td></tr>
            <tr><td style="padding:4px 0;color:#64748b;">Date of service</td><td style="padding:4px 0;">{dos}</td></tr>
            <tr><td style="padding:4px 0;color:#64748b;">Billing date</td><td style="padding:4px 0;">{billing_date}</td></tr>
            <tr><td style="padding:4px 0;color:#64748b;">Statement date</td><td style="padding:4px 0;">{stmt_date}</td></tr>
            <tr><td style="padding:4px 0;color:#64748b;">Provider/Office Employer ID#</td><td style="padding:4px 0;font-weight:600;">{office_employer_id or "—"}</td></tr>
            <tr><td style="padding:4px 0;color:#64748b;">NPI</td><td style="padding:4px 0;font-weight:600;">{provider_npi or "—"}</td></tr>
          </table>
          <p style="margin:0 0 4px;font-size:12px;font-weight:700;color:#0f766e;text-transform:uppercase;letter-spacing:0.06em;">Patient</p>
          <p style="margin:0;font-size:14px;font-weight:600;">{patient}</p>
          <p style="margin:4px 0 0;font-size:13px;color:#475569;">{addr}</p>
          {provider_block}
          <p style="margin:16px 0 6px;font-size:12px;font-weight:700;color:#0f766e;text-transform:uppercase;letter-spacing:0.06em;">Diagnosis</p>
          <p style="margin:0 0 16px;font-size:13px;color:#334155;">{diagnosis}</p>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px;margin-bottom:16px;">
            <thead>
              <tr style="background:#f1f5f9;text-align:left;">
                <th style="padding:8px;">Code</th>
                <th style="padding:8px;">Description</th>
                <th style="padding:8px;text-align:right;">Fee</th>
                <th style="padding:8px;text-align:center;">Units</th>
              </tr>
            </thead>
            <tbody>{lines_html}</tbody>
          </table>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;max-width:360px;margin-left:auto;">
            {totals_html}
          </table>
          <p style="margin:20px 0 0;font-size:12px;color:#64748b;line-height:1.5;">
            Questions? Call {clinic_phone or "the clinic"} or reply to this email.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""


def build_patient_bill_plain_text(bill: dict) -> str:
    clinic = bill.get("clinic_name") or "Relief Chiropractic"
    office_employer_id = (bill.get("office_employer_id") or bill.get("employer_tax_id") or "").strip()
    provider_npi = (bill.get("provider_npi") or bill.get("provider_billing_id") or "").strip()
    lines = [
        f"{clinic} — Patient bill / receipt",
        f"{(bill.get('address_line1') or '').strip()} {(bill.get('city_state_zip') or '').strip()}".strip(),
        f"Phone: {(bill.get('phone') or '').strip()}",
        f"Invoice: {bill.get('invoice_number') or ''}",
        f"Patient: {bill.get('patient_name') or ''}",
        f"Date of service: {bill.get('date_of_service') or ''}",
        f"Provider/Office Employer ID#: {office_employer_id or '—'}",
        f"NPI: {provider_npi or '—'}",
        f"Provider: {bill.get('provider_name') or ''}",
        "",
        "Line items:",
    ]
    for line in bill.get("lines") or []:
        lines.append(
            f"  - {line.get('cpt_code') or ''} {line.get('description') or ''} "
            f"{_money_label(line.get('fees'))} x{line.get('units') or ''}"
        )
    lines.append("")
    lines.append(f"Bill charges: {_money_label(bill.get('bill_charges_total') or bill.get('subtotal'))}")
    lines.append(
        f"Patient payments (clinic charge): {_money_label(bill.get('patient_charge_total') or bill.get('total_amount'))}"
    )
    lines.append(f"Payments received: {_money_label(bill.get('payments_received_total'))}")
    return "\n".join(lines)


def send_patient_bill_email(inv, bill: dict) -> str:
    """
    Email the paid patient bill to the patient's email on file.
    ``bill`` must be the same payload as print/PDF (_invoice_bill_dict).
    Returns the recipient address on success.
    """
    from apps.clinic.models import Invoice

    if inv.status != Invoice.Status.PAID:
        raise PatientBillEmailError(
            "Patient bill can only be emailed after the invoice is paid."
        )

    from apps.clinic.patient_communication_prefs import patient_wants_bill_email

    patient = inv.patient
    if not patient_wants_bill_email(patient):
        if not (patient.email or "").strip():
            raise PatientBillEmailError(
                "This patient has no email address on file. Add an email on the patient profile, then try again."
            )
        raise PatientBillEmailError(
            "This patient's profile has paid bills/receipts set to None. "
            "Open the patient chart → Demographics → Communication preferences and set "
            "'Paid bills / receipts' to Email only or Text and email."
        )

    to_email = (patient.email or "").strip()
    if not to_email:
        raise PatientBillEmailError(
            "This patient has no email address on file. Add an email on the patient profile, then try again."
        )

    if not _smtp_configured():
        raise PatientBillEmailError(
            "Email is not configured on the server (EMAIL_HOST is empty). "
            "In Dokploy / the API environment, set EMAIL_HOST=smtp.gmail.com, "
            "EMAIL_HOST_USER, EMAIL_HOST_PASSWORD (Gmail App Password), "
            "and PATIENT_BILL_FROM_EMAIL to that same Gmail address. Then redeploy the API."
        )

    html_body = build_patient_bill_email_html(bill)
    text_body = build_patient_bill_plain_text(bill)
    clinic_name = bill.get("clinic_name") or "Relief Chiropractic"
    subject = f"Your receipt from {clinic_name} — {bill.get('invoice_number', '')}"

    from_email = (
        getattr(settings, "PATIENT_BILL_FROM_EMAIL", None)
        or getattr(settings, "DEFAULT_FROM_EMAIL", None)
        or "reliefchiropracticmi@gmail.com"
    )
    from_email = (from_email or "").strip()
    smtp_user = (getattr(settings, "EMAIL_HOST_USER", "") or "").strip()
    if smtp_user and from_email and smtp_user.lower() != from_email.lower():
        # Gmail commonly rejects mismatched From; prefer the authenticated mailbox.
        logger.warning(
            "patient_bill_email From %s differs from EMAIL_HOST_USER %s — using EMAIL_HOST_USER",
            from_email,
            smtp_user,
        )
        from_email = smtp_user

    from django.core.mail import EmailMultiAlternatives

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=from_email,
        to=[to_email],
    )
    clinic_reply = (bill.get("email") or "").strip()
    if clinic_reply:
        msg.reply_to = [clinic_reply]
    msg.attach_alternative(html_body, "text/html")

    try:
        msg.send(fail_silently=False)
    except Exception as exc:
        logger.exception(
            "patient_bill_email failed invoice=%s to=%s from=%s host=%s",
            inv.pk,
            to_email,
            from_email,
            getattr(settings, "EMAIL_HOST", ""),
        )
        try:
            from apps.clinic.error_tracking import capture_application_error

            capture_application_error(
                exc=exc,
                source="patient_bill_email",
                message=f"patient_bill_email failed invoice={inv.pk} to={to_email}",
                extra={"invoice_id": inv.pk, "to": to_email, "from": from_email},
            )
        except Exception:
            pass
        raise PatientBillEmailError(_friendly_smtp_error(exc)) from exc

    logger.info("patient_bill_email sent invoice=%s to=%s from=%s", inv.pk, to_email, from_email)
    return to_email
