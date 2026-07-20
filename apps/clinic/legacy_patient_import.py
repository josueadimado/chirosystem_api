"""
Shared logic: import patients from the legacy Excel export.

Expected columns (row 1):
  ID | First Name | Last Name | Last Visit | Birth date | Address | City | State | Zip | Email (work) | Cell Phone

Rules:
  - Existing patients → skip (no updates).
  - New → create patient + one completed historical visit (last visit date @ 9:00 AM).
  - With phone: match first+last+phone OR first+last+DOB.
  - Without phone: match first+last+DOB; create with blank phone if no match.
"""

from __future__ import annotations

import logging
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.clinic.models import Appointment, Patient, Provider, Service, Visit
from apps.clinic.patient_phone import names_equal_casefold
from apps.clinic.utils import normalize_phone

logger = logging.getLogger(__name__)

XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

HISTORICAL_SERVICE_NAME = "Historical visit (imported)"
IMPORT_START = time(9, 0)
IMPORT_END = time(9, 15)
IMPORT_NOTE = (
    "Imported from legacy patient list. Last visit date from prior system; "
    "time set to 9:00 AM (spreadsheet had no real appointment time)."
)


class LegacyImportError(Exception):
    """User-facing import problem (bad file, missing provider, etc.)."""


@dataclass
class SheetRow:
    row_num: int
    legacy_id: str
    first_name: str
    last_name: str
    last_visit_raw: str
    birth_raw: str
    address: str
    city: str
    state: str
    zip_code: str
    email: str
    phone_raw: str


@dataclass
class ParsedRow:
    sheet: SheetRow
    first_name: str
    last_name: str
    dob: date | None
    last_visit_date: date | None
    phone_e164: str
    phone_ok: bool
    email: str
    address_line1: str
    city_state_zip: str
    skip_reason: str = ""


def _col_to_idx(cell_ref: str) -> int:
    m = re.match(r"([A-Z]+)", cell_ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _load_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    out: list[str] = []
    for si in root.findall("m:si", XLSX_NS):
        texts = [t.text or "" for t in si.findall(".//m:t", XLSX_NS)]
        out.append("".join(texts))
    return out


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    t = cell.attrib.get("t")
    v = cell.find("m:v", XLSX_NS)
    if v is None:
        is_el = cell.find("m:is", XLSX_NS)
        if is_el is not None:
            return "".join(x.text or "" for x in is_el.findall(".//m:t", XLSX_NS))
        return ""
    raw = v.text or ""
    if t == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    return raw


def read_xlsx_rows(path: Path) -> list[list[str]]:
    """Read first sheet of an .xlsx without openpyxl (stdlib zip + XML)."""
    try:
        with zipfile.ZipFile(path) as zf:
            shared = _load_shared_strings(zf)
            sheet_path = "xl/worksheets/sheet1.xml"
            if sheet_path not in zf.namelist():
                raise LegacyImportError("This workbook has no first sheet we can read.")
            root = ET.fromstring(zf.read(sheet_path))
            rows_out: list[list[str]] = []
            for row in root.findall("m:sheetData/m:row", XLSX_NS):
                cells: dict[int, str] = {}
                for c in row.findall("m:c", XLSX_NS):
                    cells[_col_to_idx(c.attrib.get("r", ""))] = _cell_value(c, shared)
                if not cells:
                    rows_out.append([])
                    continue
                maxc = max(cells)
                rows_out.append([cells.get(i, "") for i in range(maxc + 1)])
            return rows_out
    except zipfile.BadZipFile as exc:
        raise LegacyImportError("That file is not a valid Excel .xlsx workbook.") from exc


def _parse_dob(raw: str) -> date | None:
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_last_visit(raw: str) -> date | None:
    s = (raw or "").strip()
    if not s:
        return None
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", s)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %b %Y").date()
        except ValueError:
            pass
    return _parse_dob(s)


def _city_state_zip(city: str, state: str, zip_code: str) -> str:
    city = (city or "").strip()
    state = (state or "").strip()
    zip_code = (zip_code or "").strip()
    left = ", ".join(p for p in (city, state) if p)
    if left and zip_code:
        return f"{left} {zip_code}"
    return left or zip_code


def _normalize_phone_safe(raw: str) -> tuple[str, bool]:
    s = (raw or "").strip()
    if not s:
        return "", False
    try:
        e164 = normalize_phone(s)
        return (e164, bool(e164))
    except Exception:
        return "", False


def parse_sheet(path: Path) -> list[ParsedRow]:
    grid = read_xlsx_rows(path)
    if not grid:
        raise LegacyImportError("Spreadsheet is empty.")
    header = [str(c or "").strip() for c in grid[0]]
    if len(header) < 11:
        raise LegacyImportError(
            f"Expected columns like First Name, Last Name, Last Visit, Birth date, Address, "
            f"City, State, Zip, Email, Cell Phone. Found {len(header)} columns: {header}"
        )

    parsed: list[ParsedRow] = []
    for i, raw in enumerate(grid[1:], start=2):

        def g(idx: int) -> str:
            return str(raw[idx] if idx < len(raw) and raw[idx] is not None else "").strip()

        sheet = SheetRow(
            row_num=i,
            legacy_id=g(0),
            first_name=g(1),
            last_name=g(2),
            last_visit_raw=g(3),
            birth_raw=g(4),
            address=g(5),
            city=g(6),
            state=g(7),
            zip_code=g(8),
            email=g(9),
            phone_raw=g(10),
        )
        fn = sheet.first_name.strip()
        ln = sheet.last_name.strip()
        dob = _parse_dob(sheet.birth_raw)
        last_visit = _parse_last_visit(sheet.last_visit_raw)
        phone_e164, phone_ok = _normalize_phone_safe(sheet.phone_raw)
        email = sheet.email.strip()
        skip = ""
        if not fn or not ln:
            skip = "missing_name"
        elif last_visit is None:
            skip = "missing_or_bad_last_visit"
        elif dob is None and not phone_ok:
            skip = "no_phone_and_no_dob"

        parsed.append(
            ParsedRow(
                sheet=sheet,
                first_name=fn,
                last_name=ln,
                dob=dob,
                last_visit_date=last_visit,
                phone_e164=phone_e164 if phone_ok else "",
                phone_ok=phone_ok,
                email=email,
                address_line1=sheet.address.strip(),
                city_state_zip=_city_state_zip(sheet.city, sheet.state, sheet.zip_code),
                skip_reason=skip,
            )
        )
    return parsed


def _find_existing(row: ParsedRow, patients: list[Patient]) -> Patient | None:
    for p in patients:
        if not names_equal_casefold(p, row.first_name, row.last_name):
            continue
        if row.phone_ok and row.phone_e164:
            stored = (p.phone or "").strip()
            if stored == row.phone_e164:
                return p
            try:
                if stored and normalize_phone(stored) == row.phone_e164:
                    return p
            except Exception:
                pass
        if row.dob is not None and p.date_of_birth == row.dob:
            return p
    return None


def default_provider(provider_id: int | None = None) -> Provider:
    if provider_id:
        p = Provider.objects.filter(pk=provider_id, active=True).first()
        if not p:
            raise LegacyImportError(f"No active provider with id={provider_id}")
        return p
    p = (
        Provider.objects.filter(active=True, primary_service_type="chiropractic")
        .order_by("id")
        .first()
    )
    if not p:
        p = Provider.objects.filter(active=True).order_by("id").first()
    if not p:
        raise LegacyImportError("No active provider found. Create a doctor first.")
    return p


def historical_service() -> Service:
    svc, _ = Service.objects.get_or_create(
        name=HISTORICAL_SERVICE_NAME,
        defaults={
            "description": "Placeholder for last-visit dates imported from the legacy patient list. Not for public booking.",
            "duration_minutes": 15,
            "price": Decimal("0.00"),
            "billing_code": "IMPORT",
            "is_active": True,
            "show_in_public_booking": False,
            "visible_to_chiropractic_staff": False,
            "visible_to_massage_staff": False,
            "service_type": Service.ServiceType.CHIROPRACTIC,
            "is_new_client_intake": False,
            "charges_patient": False,
        },
    )
    return svc


def _clinic_tz() -> ZoneInfo:
    name = getattr(settings, "CLINIC_TIMEZONE", None) or settings.TIME_ZONE or "America/Detroit"
    return ZoneInfo(name)


def _create_patient_and_visit(
    *,
    row: ParsedRow,
    provider: Provider,
    service: Service,
) -> Patient:
    assert row.last_visit_date is not None

    notify = "email" if row.email else "none"
    patient = Patient.objects.create(
        first_name=row.first_name,
        last_name=row.last_name,
        phone=row.phone_e164,
        email=row.email,
        date_of_birth=row.dob,
        date_established=row.last_visit_date,
        address_line1=row.address_line1,
        city_state_zip=row.city_state_zip,
        sms_consent=False,
        notify_booking=notify,
        notify_reminders=notify,
        notify_bills="email" if row.email else "none",
        online_chiro_intake_waived=True,
    )

    completed_at = timezone.make_aware(
        datetime.combine(row.last_visit_date, IMPORT_START),
        _clinic_tz(),
    )
    appt = Appointment.objects.create(
        patient=patient,
        provider=provider,
        booked_service=service,
        appointment_date=row.last_visit_date,
        start_time=IMPORT_START,
        end_time=IMPORT_END,
        status=Appointment.Status.COMPLETED,
        completed_at=completed_at,
        notes=IMPORT_NOTE,
    )
    Visit.objects.create(
        appointment=appt,
        patient=patient,
        provider=provider,
        status=Visit.Status.COMPLETED,
        reason_for_visit="Legacy import — historical last visit",
        doctor_notes=IMPORT_NOTE,
        completed_at=completed_at,
    )
    return patient


def run_legacy_patient_import(
    *,
    path: Path,
    dry_run: bool = True,
    provider_id: int | None = None,
) -> dict:
    """
    Run import against ``path``.

    Returns a JSON-friendly dict with counts and a capped sample of row results.
    """
    rows = parse_sheet(path)
    patients = list(
        Patient.objects.only("id", "first_name", "last_name", "phone", "date_of_birth")
    )
    provider = default_provider(provider_id)
    provider_label = (
        getattr(getattr(provider, "user", None), "full_name", None) or str(provider)
    )

    counts = {
        "total_rows": len(rows),
        "skip_existing": 0,
        "skip_bad_row": 0,
        "would_add": 0,
        "added": 0,
        "no_phone_add": 0,
        "errors": 0,
    }
    sample: list[dict[str, str]] = []
    SAMPLE_LIMIT = 40

    service = None if dry_run else historical_service()

    for row in rows:
        base = {
            "row": str(row.sheet.row_num),
            "legacy_id": row.sheet.legacy_id,
            "first_name": row.first_name,
            "last_name": row.last_name,
            "dob": row.dob.isoformat() if row.dob else "",
            "phone": row.phone_e164 or row.sheet.phone_raw,
            "last_visit": row.last_visit_date.isoformat() if row.last_visit_date else "",
        }

        def _push(action: str, detail: str) -> None:
            if len(sample) < SAMPLE_LIMIT:
                sample.append({**base, "action": action, "detail": detail})

        if row.skip_reason:
            counts["skip_bad_row"] += 1
            _push("skip_bad_row", row.skip_reason)
            continue

        existing = _find_existing(row, patients)
        if existing is not None:
            counts["skip_existing"] += 1
            _push("skip_existing", f"matches patient #{existing.pk}")
            continue

        if dry_run:
            counts["would_add"] += 1
            if not row.phone_ok:
                counts["no_phone_add"] += 1
            _push("would_add", "no_phone" if not row.phone_ok else "ok")
            continue

        try:
            with transaction.atomic():
                assert service is not None
                created = _create_patient_and_visit(row=row, provider=provider, service=service)
            patients.append(created)
            counts["added"] += 1
            if not row.phone_ok:
                counts["no_phone_add"] += 1
            _push("added", f"patient #{created.pk}")
        except Exception as exc:
            counts["errors"] += 1
            logger.exception("Legacy patient import failed row=%s", row.sheet.row_num)
            _push("error", str(exc))

    return {
        "dry_run": dry_run,
        "provider_id": provider.pk,
        "provider_name": provider_label,
        "counts": counts,
        "sample_rows": sample,
        "sample_note": (
            f"Showing up to {SAMPLE_LIMIT} example rows. "
            "Existing patients were not changed."
        ),
    }
