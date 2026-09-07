"""
Build a printable CMS-1500-style PDF from the claim payload (same fields as the portal).

Uses ReportLab only (no system WeasyPrint/Chrome deps) so Docker deploys stay simple.
"""

from __future__ import annotations

import io
import re
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


def _s(value: Any) -> str:
    return str(value or "").strip()


def _check(on: bool) -> str:
    return "[X]" if on else "[ ]"


def _safe_filename_part(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "").strip())
    return cleaned[:60] or "claim"


def cms1500_pdf_filename(claim: dict) -> str:
    patient = _safe_filename_part(_s(claim.get("patient_name")).replace(" ", "_") or "Patient")
    invoice = _safe_filename_part(_s(claim.get("invoice_number")) or "invoice")
    return f"CMS-1500_{patient}_{invoice}.pdf"


def build_cms1500_pdf_bytes(claim: dict) -> bytes:
    """Return PDF bytes for a CMS-1500 claim document."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.45 * inch,
        title=f"CMS-1500 — {_s(claim.get('patient_name'))}",
        author="Relief Chiropractic",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CmsTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=14,
        textColor=colors.HexColor("#0d5c2e"),
        spaceAfter=4,
        leading=16,
    )
    meta_style = ParagraphStyle(
        "CmsMeta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        textColor=colors.HexColor("#475569"),
        spaceAfter=8,
        leading=12,
    )
    label_style = ParagraphStyle(
        "CmsLabel",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7,
        textColor=colors.HexColor("#64748b"),
        leading=9,
        spaceAfter=2,
    )
    val_style = ParagraphStyle(
        "CmsVal",
        parent=styles["Normal"],
        fontName="Courier-Bold",
        fontSize=9,
        textColor=colors.black,
        leading=11,
    )
    small_style = ParagraphStyle(
        "CmsSmall",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=10,
    )
    warn_style = ParagraphStyle(
        "CmsWarn",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        textColor=colors.HexColor("#9a3412"),
        leading=10,
    )

    story: list = []
    story.append(Paragraph("Health Insurance Claim Form (CMS-1500)", title_style))
    story.append(
        Paragraph(
            f"Invoice {_s(claim.get('invoice_number'))} · Account {_s(claim.get('patient_account_no'))} · "
            f"Payer {_s(claim.get('payer_name')) or '—'}",
            meta_style,
        )
    )

    warnings = claim.get("warnings") or []
    if warnings:
        warn_bits = "<br/>".join(f"• {_s(w)}" for w in warnings)
        story.append(Paragraph(f"<b>Before filing:</b><br/>{warn_bits}", warn_style))
        story.append(Spacer(1, 6))

    plans = claim.get("plan_checks") or {}
    plan_line = "  ".join(
        [
            f"{_check(bool(plans.get('medicare')))} Medicare",
            f"{_check(bool(plans.get('medicaid')))} Medicaid",
            f"{_check(bool(plans.get('tricare')))} TRICARE",
            f"{_check(bool(plans.get('champva')))} CHAMPVA",
            f"{_check(bool(plans.get('group')))} Group health plan",
            f"{_check(bool(plans.get('feca')))} FECA",
            f"{_check(bool(plans.get('other')))} Other",
        ]
    )

    def labeled_box(label: str, value: str) -> list:
        return [Paragraph(label.upper(), label_style), Paragraph(value or "—", val_style)]

    # Box 1
    t = Table(
        [[Paragraph("1. INSURANCE TYPE", label_style)], [Paragraph(plan_line, small_style)]],
        colWidths=[7.5 * inch],
    )
    t.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#94a3b8")),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(t)
    story.append(Spacer(1, 6))

    # 1a / 4 / 11
    row = Table(
        [
            [
                labeled_box("1a. Insured's ID", _s(claim.get("insured_id"))),
                labeled_box("4. Insured's name", _s(claim.get("insured_name"))),
                labeled_box("11. Group number", _s(claim.get("insured_group_number"))),
            ]
        ],
        colWidths=[2.5 * inch, 2.5 * inch, 2.5 * inch],
    )
    row.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (0, 0), 0.6, colors.HexColor("#94a3b8")),
                ("BOX", (1, 0), (1, 0), 0.6, colors.HexColor("#94a3b8")),
                ("BOX", (2, 0), (2, 0), 0.6, colors.HexColor("#94a3b8")),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(row)
    story.append(Spacer(1, 6))

    rel = (_s(claim.get("relationship")) or "self").lower()
    rel_line = (
        f"{_check(rel == 'self')} Self  {_check(rel == 'spouse')} Spouse  "
        f"{_check(rel == 'child')} Child  {_check(rel == 'other')} Other"
    )
    row = Table(
        [
            [
                labeled_box("2. Patient's name", _s(claim.get("patient_name"))),
                labeled_box(
                    "3. Birth date / sex",
                    f"{_s(claim.get('patient_dob'))}  {_s(claim.get('patient_sex')) or '—'}",
                ),
                [Paragraph("6. RELATIONSHIP", label_style), Paragraph(rel_line, small_style)],
            ]
        ],
        colWidths=[2.5 * inch, 2.5 * inch, 2.5 * inch],
    )
    row.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (0, 0), 0.6, colors.HexColor("#94a3b8")),
                ("BOX", (1, 0), (1, 0), 0.6, colors.HexColor("#94a3b8")),
                ("BOX", (2, 0), (2, 0), 0.6, colors.HexColor("#94a3b8")),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(row)
    story.append(Spacer(1, 6))

    addr = (
        f"{_s(claim.get('patient_address'))}<br/>"
        f"{_s(claim.get('patient_city'))} {_s(claim.get('patient_state'))} {_s(claim.get('patient_zip'))}<br/>"
        f"({_s(claim.get('patient_phone_area'))}) {_s(claim.get('patient_phone'))}"
    )
    sigs = (
        f"Patient: {_s(claim.get('patient_signature')) or '—'}<br/>"
        f"Insured: {_s(claim.get('insured_signature')) or '—'}"
    )
    row = Table(
        [
            [
                [Paragraph("5. PATIENT ADDRESS", label_style), Paragraph(addr, val_style)],
                [Paragraph("12 / 13. SIGNATURES", label_style), Paragraph(sigs, val_style)],
            ]
        ],
        colWidths=[3.75 * inch, 3.75 * inch],
    )
    row.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (0, 0), 0.6, colors.HexColor("#94a3b8")),
                ("BOX", (1, 0), (1, 0), 0.6, colors.HexColor("#94a3b8")),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(row)
    story.append(Spacer(1, 6))

    row = Table(
        [
            [
                labeled_box("14. Date of current illness", _s(claim.get("date_of_current_illness"))),
                labeled_box("17. Referring / rendering provider", _s(claim.get("referring_provider"))),
                labeled_box("17b. NPI", _s(claim.get("referring_npi"))),
            ]
        ],
        colWidths=[2.5 * inch, 2.5 * inch, 2.5 * inch],
    )
    row.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (0, 0), 0.6, colors.HexColor("#94a3b8")),
                ("BOX", (1, 0), (1, 0), 0.6, colors.HexColor("#94a3b8")),
                ("BOX", (2, 0), (2, 0), 0.6, colors.HexColor("#94a3b8")),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(row)
    story.append(Spacer(1, 6))

    # Diagnoses A–L
    dx = list(claim.get("diagnosis_codes") or [])
    dx_cells = []
    for i in range(12):
        dx_letter = chr(ord("A") + i)
        code = _s(dx[i]) if i < len(dx) else ""
        dx_cells.append(
            Paragraph(f"<font color='#64748b' size='7'>{dx_letter}</font>  {code or '—'}", val_style)
        )
    dx_table = Table(
        [
            [Paragraph("21. DIAGNOSIS CODES (ICD)", label_style), "", "", ""],
            dx_cells[0:4],
            dx_cells[4:8],
            dx_cells[8:12],
        ],
        colWidths=[1.875 * inch] * 4,
    )
    dx_table.setStyle(
        TableStyle(
            [
                ("SPAN", (0, 0), (-1, 0)),
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#94a3b8")),
                ("INNERGRID", (0, 1), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(dx_table)
    story.append(Spacer(1, 6))

    # Service lines
    header = [
        Paragraph("#", label_style),
        Paragraph("From", label_style),
        Paragraph("To", label_style),
        Paragraph("POS", label_style),
        Paragraph("CPT", label_style),
        Paragraph("M1", label_style),
        Paragraph("M2", label_style),
        Paragraph("M3", label_style),
        Paragraph("M4", label_style),
        Paragraph("Dx", label_style),
        Paragraph("Charges", label_style),
        Paragraph("Units", label_style),
        Paragraph("NPI", label_style),
    ]
    line_data = [header]
    for idx, line in enumerate((claim.get("service_lines") or [])[:6], start=1):
        mods = list(line.get("modifiers") or []) + ["", "", "", ""]
        mods = mods[:4]
        line_data.append(
            [
                Paragraph(str(idx), small_style),
                Paragraph(_s(line.get("date_from")), small_style),
                Paragraph(_s(line.get("date_to")), small_style),
                Paragraph(_s(line.get("place_of_service")), small_style),
                Paragraph(_s(line.get("cpt")), small_style),
                Paragraph(_s(mods[0]), small_style),
                Paragraph(_s(mods[1]), small_style),
                Paragraph(_s(mods[2]), small_style),
                Paragraph(_s(mods[3]), small_style),
                Paragraph(_s(line.get("diagnosis_pointer")), small_style),
                Paragraph(f"{_s(line.get('charges_dollars'))} {_s(line.get('charges_cents'))}", small_style),
                Paragraph(_s(line.get("units")), small_style),
                Paragraph(_s(line.get("rendering_npi")), small_style),
            ]
        )
    if len(line_data) == 1:
        line_data.append([Paragraph("No service lines", small_style)] + [""] * 12)

    lines = Table(
        [[Paragraph("24. SERVICE LINES", label_style)] + [""] * 12, *line_data],
        colWidths=[
            0.28 * inch,
            0.62 * inch,
            0.62 * inch,
            0.35 * inch,
            0.55 * inch,
            0.32 * inch,
            0.32 * inch,
            0.32 * inch,
            0.32 * inch,
            0.4 * inch,
            0.7 * inch,
            0.4 * inch,
            1.3 * inch,
        ],
    )
    lines.setStyle(
        TableStyle(
            [
                ("SPAN", (0, 0), (-1, 0)),
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#94a3b8")),
                ("INNERGRID", (0, 1), (-1, -1), 0.4, colors.HexColor("#94a3b8")),
                ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#f1f5f9")),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 2), (0, -1), "CENTER"),
                ("ALIGN", (3, 2), (9, -1), "CENTER"),
                ("ALIGN", (11, 2), (11, -1), "CENTER"),
            ]
        )
    )
    story.append(lines)
    story.append(Spacer(1, 6))

    tax = _s(claim.get("federal_tax_id"))
    if claim.get("tax_id_is_ein"):
        tax = f"{tax} (EIN)" if tax else "(EIN)"
    total = f"${_s(claim.get('total_charge_dollars'))}.{_s(claim.get('total_charge_cents'))}"
    row = Table(
        [
            [
                labeled_box("25. Federal tax ID", tax),
                labeled_box("26. Patient account #", _s(claim.get("patient_account_no"))),
                labeled_box("27. Accept assignment", "YES" if claim.get("accept_assignment") else "NO"),
                labeled_box("28. Total charge", total),
            ]
        ],
        colWidths=[1.875 * inch] * 4,
    )
    row.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (0, 0), 0.6, colors.HexColor("#94a3b8")),
                ("BOX", (1, 0), (1, 0), 0.6, colors.HexColor("#94a3b8")),
                ("BOX", (2, 0), (2, 0), 0.6, colors.HexColor("#94a3b8")),
                ("BOX", (3, 0), (3, 0), 0.6, colors.HexColor("#94a3b8")),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(row)
    story.append(Spacer(1, 6))

    phys = (
        f"{_s(claim.get('physician_signature')) or '—'}<br/>"
        f"Date: {_s(claim.get('physician_signature_date')) or '—'}"
    )
    facility = (
        f"{_s(claim.get('service_facility_name'))}<br/>"
        f"{_s(claim.get('service_facility_address'))}<br/>"
        f"{_s(claim.get('service_facility_city_state_zip'))}<br/>"
        f"NPI: {_s(claim.get('service_facility_npi')) or '—'}"
    )
    billing = (
        f"{_s(claim.get('billing_provider_name'))}<br/>"
        f"{_s(claim.get('billing_provider_address'))}<br/>"
        f"{_s(claim.get('billing_provider_city_state_zip'))}<br/>"
        f"({_s(claim.get('billing_provider_phone_area'))}) {_s(claim.get('billing_provider_phone'))}<br/>"
        f"NPI: {_s(claim.get('billing_provider_npi')) or '—'}"
    )
    row = Table(
        [
            [
                [Paragraph("31. PHYSICIAN / SUPPLIER SIGNATURE", label_style), Paragraph(phys, val_style)],
                [Paragraph("32. SERVICE FACILITY", label_style), Paragraph(facility, val_style)],
                [Paragraph("33. BILLING PROVIDER", label_style), Paragraph(billing, val_style)],
            ]
        ],
        colWidths=[2.5 * inch] * 3,
    )
    row.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (0, 0), 0.6, colors.HexColor("#94a3b8")),
                ("BOX", (1, 0), (1, 0), 0.6, colors.HexColor("#94a3b8")),
                ("BOX", (2, 0), (2, 0), 0.6, colors.HexColor("#94a3b8")),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(row)
    story.append(Spacer(1, 10))
    story.append(
        Paragraph(
            "Generated from the Relief Chiropractic clinic portal. "
            "This PDF mirrors the CMS-1500 fields used for filing.",
            meta_style,
        )
    )

    doc.build(story)
    return buf.getvalue()
