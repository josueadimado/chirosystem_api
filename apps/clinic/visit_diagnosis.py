"""Sync visit diagnosis catalog selections to visit.diagnosis text for bills and chart."""

from __future__ import annotations

from .models import DiagnosisCode, Visit, VisitDiagnosis


def format_diagnosis_display(code: str, description: str) -> str:
    c = (code or "").strip()
    d = (description or "").strip()
    if c and d:
        return f"{c} — {d}"
    return c or d


def format_visit_diagnosis_text(rows: list[VisitDiagnosis]) -> str:
    lines = [format_diagnosis_display(r.code, r.description) for r in rows if (r.code or r.description).strip()]
    return "\n".join(lines)


def serialize_visit_diagnoses(visit: Visit) -> list[dict]:
    return [
        {
            "id": row.diagnosis_id,
            "code": row.code,
            "description": row.description,
        }
        for row in visit.visit_diagnoses.all()
    ]


def apply_visit_diagnosis_from_ids(visit: Visit, diagnosis_ids: list[int] | None) -> None:
    """
    Replace visit diagnosis rows from catalog IDs and refresh visit.diagnosis text.
    Caller must save visit after this (diagnosis field updated in memory).
    """
    visit.visit_diagnoses.all().delete()
    if not diagnosis_ids:
        visit.diagnosis = ""
        return
    unique_ids: list[int] = []
    seen: set[int] = set()
    for raw in diagnosis_ids:
        pk = int(raw)
        if pk in seen:
            continue
        seen.add(pk)
        unique_ids.append(pk)
    codes = {
        dc.pk: dc
        for dc in DiagnosisCode.objects.filter(pk__in=unique_ids, is_active=True)
    }
    created: list[VisitDiagnosis] = []
    for pk in unique_ids:
        dc = codes.get(pk)
        if not dc:
            continue
        created.append(
            VisitDiagnosis.objects.create(
                visit=visit,
                diagnosis=dc,
                code=dc.code,
                description=dc.description,
            )
        )
    visit.diagnosis = format_visit_diagnosis_text(created)


def diagnosis_ids_from_visit(visit: Visit) -> list[int]:
    return [row.diagnosis_id for row in visit.visit_diagnoses.all() if row.diagnosis_id]


def update_visit_diagnosis_fields(visit: Visit, payload: dict) -> list[str]:
    """
    Apply diagnosis from payload. Returns extra visit fields to include in save(update_fields=...).
    Prefer diagnosis_ids when present; otherwise fall back to free-text diagnosis.
    """
    if "diagnosis_ids" in payload:
        apply_visit_diagnosis_from_ids(visit, payload.get("diagnosis_ids") or [])
        return ["diagnosis"]
    if "diagnosis" in payload:
        visit.diagnosis = payload.get("diagnosis", "") or ""
        return ["diagnosis"]
    return []
