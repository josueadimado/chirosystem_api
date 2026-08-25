"""
Build CMS-1500 (02/12) insurance claim payloads from visit invoices.

Printable / emailable JSON — mirrors the standard claim boxes clinics use.
"""

from __future__ import annotations

import re
from decimal import Decimal

from django.utils import timezone

from apps.clinic.digital_intake import _split_city_state_zip
from apps.clinic.models import Invoice, Visit, VisitDiagnosis


def _mmddyy(d) -> str:
    if not d:
        return ""
    return d.strftime("%m %d %y")


def _money_parts(amount) -> tuple[str, str]:
    try:
        n = Decimal(str(amount or "0")).quantize(Decimal("0.01"))
    except Exception:
        n = Decimal("0.00")
    whole = int(n)
    cents = int((n - whole) * 100)
    return str(whole), f"{cents:02d}"


def _split_cpt(billing_code: str) -> tuple[str, list[str]]:
    """Split '98940' or '97012 GP 59' into CPT + up to 4 modifiers."""
    parts = [p for p in re.split(r"[\s,]+", (billing_code or "").strip().upper()) if p]
    if not parts:
        return "", []
    return parts[0], parts[1:5]


def _provider_display_name(provider) -> str:
    if not provider:
        return ""
    user = getattr(provider, "user", None)
    if user:
        full = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
        if full:
            cred = (provider.credential or provider.title or "").strip()
            return f"{full} {cred}".strip().upper() if cred else full.upper()
    return str(provider).upper()


def _npi_for_invoice(inv: Invoice, header: dict) -> str:
    prov = inv.appointment.provider if inv.appointment_id else None
    if prov:
        per = (getattr(prov, "billing_provider_id", None) or "").strip()
        if per:
            return per
    return (header.get("provider_billing_id") or "").strip()


def _clinic_header() -> dict:
    from apps.clinic.models import ClinicSettings

    s = ClinicSettings.get_cached()
    return {
        "clinic_name": (s.clinic_name or "").strip(),
        "address_line1": (s.address_line1 or "").strip(),
        "city_state_zip": (s.city_state_zip or "").strip(),
        "phone": (s.phone or "").strip(),
        "email": (s.email or "").strip(),
        "employer_tax_id": (s.employer_tax_id or "").strip(),
        "provider_billing_id": (s.provider_billing_id or "").strip(),
        "pos_default": (s.pos_default or "11").strip() or "11",
    }


# ICD-10-CM-ish token (letter + digits, optional decimal section).
_ICD_TOKEN_RE = re.compile(
    r"\b([A-TV-Z][0-9][0-9A-Z](?:\.[0-9A-Z]{1,4})?)\b",
    re.IGNORECASE,
)


def _format_dx_for_claim(code: str) -> str:
    """CMS-1500 box 21 often prints codes with a space instead of the decimal point."""
    cleaned = re.sub(r"[^A-Za-z0-9.]", "", (code or "").strip().upper())
    if not cleaned:
        return ""
    return cleaned.replace(".", " ")


def _diagnosis_codes_for_visit(visit: Visit) -> list[str]:
    """
    Collect up to 12 ICD codes for CMS-1500 box 21.

    Prefer VisitDiagnosis catalog rows; fall back to parsing visit.diagnosis text
    (handles em dashes, hyphens, and codes embedded in sentences).
    """
    codes: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        display = _format_dx_for_claim(raw)
        if not display:
            return
        key = display.replace(" ", "")
        if key in seen:
            return
        seen.add(key)
        codes.append(display)

    # Direct query so we never miss rows if prefetch was skipped/stale.
    for row in VisitDiagnosis.objects.filter(visit_id=visit.pk).order_by("id"):
        add(row.code)

    text = (visit.diagnosis or "").strip()
    if text in {"—", "–", "-", "n/a", "N/A", "none", "None"}:
        text = ""

    if not codes and text:
        for line in re.split(r"[\r\n]+", text):
            line = line.strip()
            if not line:
                continue
            # "M54.13 — Cervicalgia" / "M54.13 - Cervicalgia" / "M54.13: ..."
            head = re.split(r"\s*[—–\-:|/]\s*", line, maxsplit=1)[0].strip()
            if _ICD_TOKEN_RE.match(head) or re.match(r"^[A-TV-Z]\d", head, re.I):
                add(head)
                continue
            embedded = _ICD_TOKEN_RE.search(line)
            if embedded:
                add(embedded.group(1))

        if not codes:
            for match in _ICD_TOKEN_RE.finditer(text):
                add(match.group(1))

    return codes[:12]


def build_cms1500_claim(inv: Invoice, *, header: dict | None = None) -> dict:
    """
    Build a CMS-1500 claim dict from a visit invoice.

    ``header`` should be the clinic bill header (name, address, tax id, NPI, pos).
    """
    if header is None:
        header = _clinic_header()

    visit: Visit = inv.visit
    pat = inv.patient
    appt = inv.appointment
    dos = appt.appointment_date
    provider = appt.provider if appt else None

    addr = _split_city_state_zip(pat.city_state_zip or "")
    phone_digits = re.sub(r"\D", "", pat.phone or "")
    phone_area = phone_digits[-10:-7] if len(phone_digits) >= 10 else ""
    phone_rest = phone_digits[-7:] if len(phone_digits) >= 7 else phone_digits

    diagnosis_codes = _diagnosis_codes_for_visit(visit)
    # Pointers A–L for up to 12 diagnoses (service lines use first 4).
    pointer_letters = "".join(chr(ord("A") + i) for i in range(min(4, len(diagnosis_codes)))) or "A"

    lines_out: list[dict] = []
    total = Decimal("0.00")
    for rs in visit.rendered_services.select_related("service").order_by("id"):
        svc = rs.service
        cpt, modifiers = _split_cpt(svc.billing_code or "")
        dollars, cents = _money_parts(rs.total_price)
        total += Decimal(str(rs.total_price))
        lines_out.append(
            {
                "date_from": _mmddyy(dos),
                "date_to": _mmddyy(dos),
                "place_of_service": (header.get("pos_default") or "11").strip() or "11",
                "emg": "",
                "cpt": cpt,
                "modifiers": modifiers,
                "diagnosis_pointer": pointer_letters[:4],
                "charges_dollars": dollars,
                "charges_cents": cents,
                "units": str(rs.quantity or 1),
                "epsdt": "",
                "family_plan": "",
                "rendering_npi": _npi_for_invoice(inv, header),
                "description": (svc.name or "")[:80],
                "charges_patient": bool(rs.charges_patient),
            }
        )

    total_d, total_c = _money_parts(total)
    rel = (pat.insurance_relationship or "self").strip().lower() or "self"
    patient_name = f"{pat.last_name} {pat.first_name}".strip().upper()
    insured_name = (pat.insured_name or "").strip().upper()
    if not insured_name and rel == "self":
        insured_name = patient_name

    plan = (pat.insurance_plan_type or "group").strip().lower() or "group"
    plan_checks = {
        "medicare": plan == "medicare",
        "medicaid": plan == "medicaid",
        "tricare": plan == "tricare",
        "champva": plan == "champva",
        "group": plan == "group",
        "feca": plan == "feca",
        "other": plan == "other"
        or plan
        not in {
            "medicare",
            "medicaid",
            "tricare",
            "champva",
            "group",
            "feca",
        },
    }

    clinic_name = (header.get("clinic_name") or "").strip().upper()
    clinic_addr = (header.get("address_line1") or "").strip().upper()
    clinic_csz = (header.get("city_state_zip") or "").strip().upper()
    clinic_phone = re.sub(r"\D", "", header.get("phone") or "")
    clinic_phone_area = clinic_phone[-10:-7] if len(clinic_phone) >= 10 else ""
    clinic_phone_rest = clinic_phone[-7:] if len(clinic_phone) >= 7 else clinic_phone

    npi = _npi_for_invoice(inv, header)
    tax_id = (header.get("employer_tax_id") or "").strip()
    provider_name = _provider_display_name(provider)
    today = timezone.localdate()

    return {
        "form_title": "Health Insurance Claim Form (CMS-1500)",
        "invoice_id": inv.pk,
        "invoice_number": inv.invoice_number,
        "patient_id": pat.pk,
        "appointment_id": appt.pk if appt else None,
        "generated_at": timezone.now().isoformat(),
        # Box 1 / 1a
        "plan_type": plan,
        "plan_checks": plan_checks,
        "insured_id": (pat.insurance_member_id or "").strip().upper(),
        "payer_name": (pat.insurance_payer_name or "").strip().upper(),
        "payer_email": (
            (pat.insurance_company.claim_email or "").strip()
            if getattr(pat, "insurance_company", None)
            else ""
        ),
        # Box 2–7 patient / insured
        "patient_name": patient_name,
        "patient_dob": _mmddyy(pat.date_of_birth),
        "patient_sex": (pat.sex or "").strip().upper(),
        "patient_address": (pat.address_line1 or "").strip().upper(),
        "patient_city": (addr.get("city") or "").upper(),
        "patient_state": (addr.get("state") or "").upper(),
        "patient_zip": (addr.get("zip") or "").upper(),
        "patient_phone_area": phone_area,
        "patient_phone": phone_rest,
        "relationship": rel,
        "insured_name": insured_name,
        "insured_group_number": (pat.insurance_group_number or "").strip().upper(),
        # Box 10 condition related
        "employment_related": False,
        "auto_accident": False,
        "other_accident": False,
        # Box 12 / 13
        "patient_signature": "SIGNATURE ON FILE",
        "insured_signature": "SIGNATURE ON FILE",
        # Box 14–23
        "date_of_current_illness": _mmddyy(dos),
        "referring_provider": provider_name,
        "referring_npi": npi,
        "diagnosis_codes": diagnosis_codes,
        "prior_auth_number": "",
        # Box 24 service lines
        "service_lines": lines_out,
        # Box 25–33
        "federal_tax_id": tax_id,
        "tax_id_is_ein": True,
        "patient_account_no": (inv.invoice_number or str(pat.pk)).upper(),
        "accept_assignment": True,
        "total_charge_dollars": total_d,
        "total_charge_cents": total_c,
        "amount_paid_dollars": "0",
        "amount_paid_cents": "00",
        "physician_signature": provider_name or "SIGNATURE ON FILE",
        "physician_signature_date": _mmddyy(today),
        "service_facility_name": clinic_name,
        "service_facility_address": clinic_addr,
        "service_facility_city_state_zip": clinic_csz,
        "service_facility_npi": npi,
        "billing_provider_name": clinic_name,
        "billing_provider_address": clinic_addr,
        "billing_provider_city_state_zip": clinic_csz,
        "billing_provider_phone_area": clinic_phone_area,
        "billing_provider_phone": clinic_phone_rest,
        "billing_provider_npi": npi,
        "warnings": _claim_warnings(pat, lines_out, diagnosis_codes),
    }


def _claim_warnings(patient, lines: list[dict], diagnosis_codes: list[str]) -> list[str]:
    notes: list[str] = []
    if not (patient.insurance_member_id or "").strip():
        notes.append("Member / insured ID is blank — add it on the patient chart or edit before sending.")
    if not (patient.insurance_payer_name or "").strip():
        notes.append("Insurance company name is blank.")
    if not (patient.sex or "").strip():
        notes.append("Patient sex is blank (needed for box 3).")
    if not patient.date_of_birth:
        notes.append("Patient date of birth is blank.")
    if not diagnosis_codes:
        notes.append(
            "No diagnosis codes on this visit — open the visit in Billing (Edit billing) or the "
            "doctor chart and add ICD codes, then generate the claim again."
        )
    if not lines:
        notes.append("No service lines on this visit.")
    return notes
