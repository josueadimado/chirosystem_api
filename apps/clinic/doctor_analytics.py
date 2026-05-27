"""Doctor portal analytics — /doctor/my-analytics/ (filtered by logged-in provider)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from django.conf import settings
from django.db.models import Min
from django.utils import timezone

from .models import Appointment, Patient, Provider, Service, Visit

# Typical chiropractic care plan length when no per-patient plan is stored in the DB.
DEFAULT_CARE_PLAN_SESSIONS = 12

_REMAINING_STATUSES = (
    Appointment.Status.BOOKED,
    Appointment.Status.CHECKED_IN,
    Appointment.Status.IN_CONSULTATION,
    Appointment.Status.AWAITING_PAYMENT,
)
_MISSED_STATUSES = (Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW)
_FUTURE_EXCLUDED = (
    Appointment.Status.CANCELLED,
    Appointment.Status.NO_SHOW,
    Appointment.Status.COMPLETED,
)


def _clinic_tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo(getattr(settings, "CLINIC_TIMEZONE", "America/Detroit"))


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    tz = _clinic_tz()
    start = datetime(year, month, 1, 0, 0, 0, tzinfo=tz)
    if month == 12:
        end = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=tz)
    else:
        end = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=tz)
    return start, end


def _appt_start_aware(appt: Appointment) -> datetime:
    tz = _clinic_tz()
    return datetime.combine(appt.appointment_date, appt.start_time, tzinfo=tz)


def _patient_program_label(appt: Appointment | None, provider: Provider) -> str:
    if appt and appt.booked_service_id:
        return (appt.booked_service.name or "General care").strip()
    if provider.primary_service_type == Service.ServiceType.MASSAGE:
        return "Massage therapy"
    return "Chiropractic care"


def _completed_visit_count(patient_id: int, provider_id: int) -> int:
    return Visit.objects.filter(
        patient_id=patient_id,
        provider_id=provider_id,
        status=Visit.Status.COMPLETED,
        completed_at__isnull=False,
    ).count()


def _sessions_left(patient_id: int, provider_id: int, plan_sessions: int = DEFAULT_CARE_PLAN_SESSIONS) -> int:
    done = _completed_visit_count(patient_id, provider_id)
    return max(0, plan_sessions - done)


def _missed_streak(patient_id: int, provider_id: int) -> int:
    """Consecutive most-recent appointments that were cancelled or no-show."""
    recent = (
        Appointment.objects.filter(patient_id=patient_id, provider_id=provider_id)
        .order_by("-appointment_date", "-start_time")[:12]
    )
    streak = 0
    for appt in recent:
        if appt.status in _MISSED_STATUSES:
            streak += 1
        else:
            break
    return streak


def _latest_patient_appt(patient_id: int, provider_id: int) -> Appointment | None:
    return (
        Appointment.objects.filter(patient_id=patient_id, provider_id=provider_id)
        .select_related("booked_service")
        .order_by("-appointment_date", "-start_time")
        .first()
    )


def _last_completed_visit_date(patient_id: int, provider_id: int) -> date | None:
    v = (
        Visit.objects.filter(
            patient_id=patient_id,
            provider_id=provider_id,
            status=Visit.Status.COMPLETED,
            completed_at__isnull=False,
        )
        .order_by("-completed_at")
        .first()
    )
    if not v or not v.completed_at:
        return None
    return timezone.localtime(v.completed_at).date()


def _build_today(provider: Provider) -> dict:
    today = timezone.localdate()
    now = timezone.localtime(timezone.now())
    appts = list(
        Appointment.objects.filter(provider=provider, appointment_date=today)
        .select_related("patient", "booked_service")
        .order_by("start_time")
    )
    active_today = [a for a in appts if a.status != Appointment.Status.CANCELLED]
    total = len({a.patient_id for a in active_today})
    completed = sum(1 for a in appts if a.status == Appointment.Status.COMPLETED)
    remaining = sum(1 for a in appts if a.status in _REMAINING_STATUSES)

    next_patient = None
    for a in appts:
        if a.status not in _REMAINING_STATUSES:
            continue
        start = _appt_start_aware(a)
        if start >= now or a.status != Appointment.Status.BOOKED:
            next_patient = a
            break
    if next_patient is None:
        for a in appts:
            if a.status in _REMAINING_STATUSES:
                next_patient = a
                break

    next_payload = None
    if next_patient:
        start = _appt_start_aware(next_patient)
        delta = start - now
        minutes_until = max(0, int(delta.total_seconds() // 60))
        next_payload = {
            "patient_id": next_patient.patient_id,
            "name": f"{next_patient.patient.first_name} {next_patient.patient.last_name}".strip(),
            "time": next_patient.start_time.strftime("%I:%M %p"),
            "minutes_until": minutes_until,
        }

    return {
        "total": total,
        "completed": completed,
        "remaining": remaining,
        "next_patient": next_payload,
    }


def _build_monthly_kpis(provider: Provider) -> dict:
    now = timezone.localtime(timezone.now())
    cur_start, cur_end = _month_bounds(now.year, now.month)

    visits_month = Visit.objects.filter(
        provider=provider,
        status=Visit.Status.COMPLETED,
        completed_at__gte=cur_start,
        completed_at__lt=cur_end,
    )
    patients_seen = visits_month.values("patient_id").distinct().count()
    sessions_completed = visits_month.count()

    first_with_provider = (
        Appointment.objects.filter(provider=provider)
        .values("patient_id")
        .annotate(first=Min("appointment_date"))
    )
    start_d = cur_start.date()
    end_d = cur_end.date()
    new_patients = first_with_provider.filter(first__gte=start_d, first__lt=end_d).count()

    appts_month = Appointment.objects.filter(
        provider=provider,
        appointment_date__gte=start_d,
        appointment_date__lt=end_d,
    )
    no_shows = appts_month.filter(status=Appointment.Status.NO_SHOW).count()
    completed_appts = appts_month.filter(status=Appointment.Status.COMPLETED).count()
    cancelled = appts_month.filter(status=Appointment.Status.CANCELLED).count()
    denom = completed_appts + no_shows + cancelled
    no_show_rate = round((no_shows / denom * 100) if denom else 0.0, 1)

    return {
        "patients_seen": patients_seen,
        "new_patients": new_patients,
        "sessions_completed": sessions_completed,
        "no_show_rate": no_show_rate,
    }


def _build_needs_attention(provider: Provider, today: date) -> dict:
    patient_ids = list(
        Appointment.objects.filter(provider=provider).values_list("patient_id", flat=True).distinct()
    )
    missed_sessions = []
    completing_soon = []
    unscheduled = []

    future_by_pid: dict[int, Appointment] = {}
    for a in Appointment.objects.filter(
        provider=provider,
        patient_id__in=patient_ids,
        appointment_date__gte=today,
    ).exclude(status__in=_FUTURE_EXCLUDED).select_related("patient", "booked_service").order_by(
        "appointment_date", "start_time"
    ):
        future_by_pid.setdefault(a.patient_id, a)

    for pid in patient_ids:
        patient = Patient.objects.filter(pk=pid).first()
        if not patient:
            continue
        name = f"{patient.first_name} {patient.last_name}".strip() or "Patient"
        latest = _latest_patient_appt(pid, provider.id)
        program = _patient_program_label(latest, provider)
        last_seen = _last_completed_visit_date(pid, provider.id)
        last_seen_str = last_seen.isoformat() if last_seen else None
        left = _sessions_left(pid, provider.id)

        if _missed_streak(pid, provider.id) >= 2:
            missed_sessions.append(
                {
                    "patient_id": pid,
                    "name": name,
                    "program": program,
                    "last_seen": last_seen_str,
                }
            )

        if 0 < left <= 2 and _completed_visit_count(pid, provider.id) > 0:
            completing_soon.append(
                {
                    "patient_id": pid,
                    "name": name,
                    "program": program,
                    "sessions_left": left,
                }
            )

        if pid not in future_by_pid and _completed_visit_count(pid, provider.id) > 0:
            unscheduled.append(
                {
                    "patient_id": pid,
                    "name": name,
                    "program": program,
                    "last_session": last_seen_str,
                }
            )

    missed_sessions.sort(key=lambda x: x.get("last_seen") or "")
    completing_soon.sort(key=lambda x: x["sessions_left"])
    unscheduled.sort(key=lambda x: x.get("last_session") or "", reverse=True)

    return {
        "missed_sessions": missed_sessions[:25],
        "completing_soon": completing_soon[:25],
        "unscheduled": unscheduled[:25],
    }


def _build_completions_this_month(provider: Provider) -> list[dict]:
    now = timezone.localtime(timezone.now())
    cur_start, cur_end = _month_bounds(now.year, now.month)
    start_d = cur_start.date()
    end_d = cur_end.date()

    completed_visits = (
        Visit.objects.filter(
            provider=provider,
            status=Visit.Status.COMPLETED,
            completed_at__gte=cur_start,
            completed_at__lt=cur_end,
        )
        .select_related("appointment__booked_service")
    )
    by_program: dict[str, dict] = {}

    for v in completed_visits:
        program = _patient_program_label(v.appointment, provider)
        bucket = by_program.setdefault(
            program,
            {"program": program, "clients_completed": 0, "certificates_issued": 0, "session_counts": []},
        )
        bucket["clients_completed"] += 1
        total_done = _completed_visit_count(v.patient_id, provider.id)
        if total_done >= DEFAULT_CARE_PLAN_SESSIONS:
            prev_done = total_done - 1
            if prev_done < DEFAULT_CARE_PLAN_SESSIONS:
                bucket["certificates_issued"] += 1
                bucket["session_counts"].append(total_done)

    rows = []
    for program, bucket in sorted(by_program.items(), key=lambda x: -x[1]["clients_completed"]):
        counts = bucket["session_counts"]
        avg_sessions = round(sum(counts) / len(counts), 1) if counts else None
        rows.append(
            {
                "program": program,
                "clients_completed": bucket["clients_completed"],
                "certificates_issued": bucket["certificates_issued"],
                "avg_sessions_to_complete": avg_sessions,
            }
        )
    return rows


def _build_weekly_sessions(provider: Provider, weeks: int = 8) -> list[dict]:
    today = timezone.localdate()
    week_start = today - timedelta(days=today.weekday())
    rows = []
    for i in range(weeks - 1, -1, -1):
        ws = week_start - timedelta(weeks=i)
        we = ws + timedelta(days=6)
        label = ws.strftime("%b %d")
        qs = Appointment.objects.filter(
            provider=provider,
            appointment_date__gte=ws,
            appointment_date__lte=we,
        )
        total = qs.exclude(status=Appointment.Status.CANCELLED).count()
        completed = qs.filter(status=Appointment.Status.COMPLETED).count()
        missed = qs.filter(status__in=_MISSED_STATUSES).count()
        rows.append(
            {
                "week": label,
                "sessions": total,
                "completed": completed,
                "missed": missed,
            }
        )
    return rows


def parse_analytics_weeks(raw: str | None, *, default: int = 8) -> int:
    try:
        n = int((raw or "").strip() or default)
    except ValueError:
        n = default
    return n if n in (4, 8, 12, 16, 24) else default


def build_doctor_my_analytics_payload(provider: Provider, *, weeks: int = 8) -> dict:
    today = timezone.localdate()
    chart_weeks = parse_analytics_weeks(str(weeks), default=8)
    return {
        "today": _build_today(provider),
        "monthly_kpis": _build_monthly_kpis(provider),
        "needs_attention": _build_needs_attention(provider, today),
        "completions_this_month": _build_completions_this_month(provider),
        "weekly_sessions": _build_weekly_sessions(provider, weeks=chart_weeks),
        "weekly_sessions_weeks": chart_weeks,
        "care_plan_sessions": DEFAULT_CARE_PLAN_SESSIONS,
    }
