"""CMS-1500 PDF attachment for insurance claim emails."""

from apps.clinic.insurance_claim_pdf import build_cms1500_pdf_bytes, cms1500_pdf_filename


def _sample_claim() -> dict:
    return {
        "invoice_id": 99,
        "invoice_number": "INV-TEST-1500",
        "patient_account_no": "42",
        "patient_name": "DOE JANE",
        "patient_dob": "01 15 80",
        "patient_sex": "F",
        "patient_address": "123 MAIN ST",
        "patient_city": "DETROIT",
        "patient_state": "MI",
        "patient_zip": "48201",
        "patient_phone_area": "313",
        "patient_phone": "5551212",
        "relationship": "self",
        "insured_id": "ABC123",
        "insured_name": "DOE JANE",
        "insured_group_number": "GRP1",
        "patient_signature": "Signature on File",
        "insured_signature": "Signature on File",
        "date_of_current_illness": "09 01 26",
        "referring_provider": "MEAD DC",
        "referring_npi": "1700186277",
        "payer_name": "Test Payer",
        "plan_checks": {"other": True},
        "diagnosis_codes": ["M54 5", "M99 01"],
        "service_lines": [
            {
                "date_from": "09 01 26",
                "date_to": "09 01 26",
                "place_of_service": "11",
                "cpt": "98940",
                "modifiers": [],
                "diagnosis_pointer": "AB",
                "charges_dollars": "55",
                "charges_cents": "00",
                "units": "1",
                "rendering_npi": "1700186277",
            }
        ],
        "federal_tax_id": "453798678",
        "tax_id_is_ein": True,
        "accept_assignment": True,
        "total_charge_dollars": "55",
        "total_charge_cents": "00",
        "physician_signature": "MEAD DC",
        "physician_signature_date": "09 01 26",
        "service_facility_name": "RELIEF CHIROPRACTIC",
        "service_facility_address": "1 CLINIC WAY",
        "service_facility_city_state_zip": "DETROIT MI 48201",
        "service_facility_npi": "1700186277",
        "billing_provider_name": "RELIEF CHIROPRACTIC",
        "billing_provider_address": "1 CLINIC WAY",
        "billing_provider_city_state_zip": "DETROIT MI 48201",
        "billing_provider_phone_area": "313",
        "billing_provider_phone": "5550000",
        "billing_provider_npi": "1700186277",
        "warnings": [],
    }


def test_cms1500_pdf_starts_with_pdf_header():
    pdf = build_cms1500_pdf_bytes(_sample_claim())
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000


def test_cms1500_pdf_filename_is_safe():
    name = cms1500_pdf_filename(_sample_claim())
    assert name.startswith("CMS-1500_")
    assert name.endswith(".pdf")
    assert " " not in name
