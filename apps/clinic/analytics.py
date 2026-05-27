"""Admin analytics dashboard — single payload for /admin/analytics/."""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Max, Min, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import Appointment, Invoice, Patient, Payment, Visit, VisitRenderedService, VoiceCallLog

_APPT_EXCLUDED = (
    Appointment.Status.CANCELLED,
    Appointment.Status.NO_SHOW,
)
_SCHEDULED_STATUSES = (
    Appointment.Status.BOOKED,
    Appointment.Status.CHECKED_IN,
    Appointment.Status.IN_CONSULTATION,
    Appointment.Status.AWAITING_PAYMENT,
)


def _clinic_tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo(getattr(settings, "CLINIC_TIMEZONE", "America/Detroit"))


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """Aware datetimes [start, end) for a calendar month in clinic TZ."""
    tz = _clinic_tz()
    start = datetime(year, month, 1, 0, 0, 0, tzinfo=tz)
    if month == 12:
        end = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=tz)
    else:
        end = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=tz)
    return start, end


def _quantize_money(value: Decimal | int | float | None) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return Decimal(value).quantize(Decimal("0.01"))


def _money_str(value: Decimal | int | float | None) -> str:
    return str(_quantize_money(value))


def _pct_change(current: Decimal | int, previous: Decimal | int) -> float | None:
    prev = Decimal(previous)
    cur = Decimal(current)
    if prev == 0:
        return 100.0 if cur > 0 else 0.0
    return float(((cur - prev) / prev * 100).quantize(Decimal("0.1")))


def _active_patient_ids_through(end_date: date) -> set[int]:
    return set(
        Appointment.objects.filter(appointment_date__lte=end_date)
        .exclude(status__in=_APPT_EXCLUDED)
        .values_list("patient_id", flat=True)
        .distinct()
    )


def _payments_collected_between(start: datetime, end: datetime) -> Decimal:
    agg = Payment.objects.filter(
        status=Payment.Status.SUCCESSFUL,
        paid_at__gte=start,
        paid_at__lt=end,
    ).aggregate(total=Sum("amount"))
    return _quantize_money(agg["total"])


def _invoice_outstanding(inv: Invoice) -> Decimal:
    paid = inv.payments.filter(status=Payment.Status.SUCCESSFUL).aggregate(s=Sum("amount"))["s"]
    paid = _quantize_money(paid)
    due = _quantize_money(inv.total_amount) - paid
    return max(due, Decimal("0.00"))


def _global_outstanding_balance() -> Decimal:
    paid_sub = (
        Payment.objects.filter(
            invoice_id=OuterRef("pk"),
            status=Payment.Status.SUCCESSFUL,
        )
        .values("invoice_id")
        .annotate(s=Sum("amount"))
        .values("s")[:1]
    )
    rows = (
        Invoice.objects.filter(status__in=(Invoice.Status.ISSUED, Invoice.Status.OVERDUE))
        .annotate(paid=Coalesce(Subquery(paid_sub), Decimal("0.00")))
        .values_list("total_amount", "paid")
    )
    total = Decimal("0.00")
    for amount, paid in rows:
        due = _quantize_money(amount) - _quantize_money(paid)
        if due > 0:
            total += due
    return _quantize_money(total)


def _new_clients_in_month(start: datetime, end: datetime) -> int:
    """Patients whose first non-cancelled appointment falls in [start, end)."""
    start_d = start.date()
    end_d = end.date()
    return (
        Appointment.objects.exclude(status__in=_APPT_EXCLUDED)
        .values("patient_id")
        .annotate(first=Min("appointment_date"))
        .filter(first__gte=start_d, first__lt=end_d)
        .count()
    )


def _revenue_by_service_month(start: datetime, end: datetime, top_n: int = 5) -> list[dict]:
    """Top services by rendered charges on invoices paid in the month."""
    rows = (
        VisitRenderedService.objects.filter(
            visit__invoice__status=Invoice.Status.PAID,
            visit__invoice__paid_at__gte=start,
            visit__invoice__paid_at__lt=end,
            charges_patient=True,
        )
        .values("service__name")
        .annotate(revenue=Sum("total_price"))
        .order_by("-revenue")[:top_n]
    )
    items = [
        {"name": (r["service__name"] or "Unknown").strip(), "revenue": _quantize_money(r["revenue"])}
        for r in rows
    ]
    grand = sum((i["revenue"] for i in items), Decimal("0.00"))
    if not items:
        return []
    # Include all paid lines for percentage denominator (not only top 5)
    grand_all = VisitRenderedService.objects.filter(
        visit__invoice__status=Invoice.Status.PAID,
        visit__invoice__paid_at__gte=start,
        visit__invoice__paid_at__lt=end,
        charges_patient=True,
    ).aggregate(s=Sum("total_price"))["s"]
    grand_all = _quantize_money(grand_all) or grand
    result = []
    for item in items:
        pct = float((item["revenue"] / grand_all * 100).quantize(Decimal("0.1"))) if grand_all else 0.0
        result.append(
            {
                "name": item["name"],
                "revenue": _money_str(item["revenue"]),
                "percentage": pct,
            }
        )
    return result


def _client_health_counts(today: date) -> dict:
    """Active / at-risk / inactive from last completed visit date."""
    active_cutoff = today - timedelta(days=30)
    at_risk_cutoff = today - timedelta(days=89)
    last_by_patient = (
        Visit.objects.filter(status=Visit.Status.COMPLETED, completed_at__isnull=False)
        .values("patient_id")
        .annotate(last=Max("completed_at"))
    )
    active = last_by_patient.filter(last__date__gte=active_cutoff).count()
    at_risk = last_by_patient.filter(
        last__date__lt=active_cutoff,
        last__date__gte=at_risk_cutoff,
    ).count()
    with_visit = last_by_patient.count()
    inactive_with_visit = last_by_patient.filter(last__date__lt=at_risk_cutoff).count()
    never_visited = Patient.objects.count() - with_visit
    return {
        "active_30d": active,
        "at_risk_60d": at_risk,
        "inactive_90d": inactive_with_visit + never_visited,
    }


def _build_revenue_chart(months: int) -> list[dict]:
    """Monthly collected vs outstanding added for the last N calendar months."""
    tz = _clinic_tz()
    now = timezone.now().astimezone(tz)
    months = max(3, min(int(months), 12))
    revenue_chart = []
    y, m = now.year, now.month
    for _ in range(months):
        m_start, m_end = _month_bounds(y, m)
        collected = _payments_collected_between(m_start, m_end)
        outstanding_added = _quantize_money(
            Invoice.objects.filter(
                issued_at__gte=m_start,
                issued_at__lt=m_end,
            )
            .filter(status__in=(Invoice.Status.ISSUED, Invoice.Status.OVERDUE))
            .aggregate(s=Sum("total_amount"))["s"]
        )
        label = datetime(y, m, 1, tzinfo=tz).strftime("%b %y")
        revenue_chart.append(
            {
                "month": label,
                "collected": float(collected),
                "outstanding": float(outstanding_added),
            }
        )
        m -= 1
        if m < 1:
            m = 12
            y -= 1
    revenue_chart.reverse()
    return revenue_chart


def parse_analytics_months(raw: str | None, *, default: int = 6) -> int:
    try:
        n = int((raw or "").strip() or default)
    except ValueError:
        n = default
    return n if n in (3, 6, 12) else default


def build_admin_analytics_payload(*, months: int = 6) -> dict:
    tz = _clinic_tz()
    now = timezone.now().astimezone(tz)
    today = now.date()
    cur_start, cur_end = _month_bounds(now.year, now.month)

    if now.month == 1:
        prev_start, prev_end = _month_bounds(now.year - 1, 12)
        prev_month_last_day = date(now.year - 1, 12, 31)
    else:
        prev_start, prev_end = _month_bounds(now.year, now.month - 1)
        prev_month_last_day = date(now.year, now.month - 1, calendar.monthrange(now.year, now.month - 1)[1])

    # --- KPIs ---
    active_now = len(_active_patient_ids_through(today))
    active_prev = len(_active_patient_ids_through(prev_month_last_day))
    revenue_cur = _payments_collected_between(cur_start, cur_end)
    revenue_prev = _payments_collected_between(prev_start, prev_end)
    outstanding = _global_outstanding_balance()
    new_cur = _new_clients_in_month(cur_start, cur_end)
    new_prev = _new_clients_in_month(prev_start, prev_end)

    kpis = {
        "total_clients": active_now,
        "total_clients_change": _pct_change(active_now, active_prev),
        "revenue_this_month": _money_str(revenue_cur),
        "revenue_change": _pct_change(revenue_cur, revenue_prev),
        "outstanding_balance": _money_str(outstanding),
        "new_clients_this_month": new_cur,
        "new_clients_change": _pct_change(new_cur, new_prev),
    }

    chart_months = parse_analytics_months(str(months), default=6)
    revenue_chart = _build_revenue_chart(chart_months)

    # --- Appointments this week (Mon–Sun) ---
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    week_qs = Appointment.objects.filter(appointment_date__gte=week_start, appointment_date__lte=week_end)
    scheduled = week_qs.filter(status__in=_SCHEDULED_STATUSES).count()
    completed = week_qs.filter(status=Appointment.Status.COMPLETED).count()
    cancelled = week_qs.filter(status=Appointment.Status.CANCELLED).count()
    no_shows = week_qs.filter(status=Appointment.Status.NO_SHOW).count()
    denom = completed + cancelled + no_shows
    no_show_rate = round((no_shows / denom * 100) if denom else 0.0, 1)

    appointments_this_week = {
        "scheduled": scheduled,
        "completed": completed,
        "cancelled": cancelled,
        "no_shows": no_shows,
        "no_show_rate": no_show_rate,
    }

    # --- Billing summary (this month) ---
    issued_this_month = Invoice.objects.filter(issued_at__gte=cur_start, issued_at__lt=cur_end)
    total_billed = _quantize_money(issued_this_month.aggregate(s=Sum("total_amount"))["s"])
    collected = revenue_cur
    open_issued_month = issued_this_month.filter(status__in=(Invoice.Status.ISSUED, Invoice.Status.OVERDUE))
    outstanding_month = Decimal("0.00")
    for inv in open_issued_month.prefetch_related("payments"):
        outstanding_month += _invoice_outstanding(inv)
    outstanding_month = _quantize_money(outstanding_month)
    waived = _quantize_money(
        issued_this_month.filter(status=Invoice.Status.VOID).aggregate(s=Sum("total_amount"))["s"]
    ) + _quantize_money(issued_this_month.aggregate(s=Sum("discount"))["s"])
    no_show_fees = _quantize_money(
        issued_this_month.filter(
            kind=Invoice.Kind.NO_SHOW_FEE,
            status__in=(Invoice.Status.ISSUED, Invoice.Status.OVERDUE),
        ).aggregate(s=Sum("total_amount"))["s"]
    )
    collection_rate = (
        float((collected / total_billed * 100).quantize(Decimal("0.1")))
        if total_billed > 0
        else 0.0
    )

    billing_summary = {
        "total_billed": _money_str(total_billed),
        "collected": _money_str(collected),
        "outstanding": _money_str(outstanding_month),
        "waived": _money_str(waived),
        "no_show_fees": _money_str(no_show_fees),
        "collection_rate": collection_rate,
    }

    revenue_by_service = _revenue_by_service_month(cur_start, cur_end)

    client_health = _client_health_counts(today)

    voice_qs = VoiceCallLog.objects.filter(created_at__gte=cur_start, created_at__lt=cur_end)
    total_calls = voice_qs.count()
    booked = voice_qs.filter(outcome=VoiceCallLog.Outcome.BOOKED).count()
    failed = voice_qs.exclude(outcome=VoiceCallLog.Outcome.BOOKED).count()
    book_rate = round((booked / total_calls * 100) if total_calls else 0.0, 1)

    voice_summary = {
        "total_calls": total_calls,
        "booked": booked,
        "failed": failed,
        "book_rate": book_rate,
    }

    return {
        "kpis": kpis,
        "revenue_chart": revenue_chart,
        "revenue_chart_months": chart_months,
        "appointments_this_week": appointments_this_week,
        "billing_summary": billing_summary,
        "revenue_by_service": revenue_by_service,
        "client_health": client_health,
        "voice_summary": voice_summary,
    }
