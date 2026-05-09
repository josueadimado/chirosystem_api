"""
One-time data fix: extend end_time on legacy massage rows that used service duration only
(no post-visit calendar buffer). Safe to re-run; idempotent for already-correct rows.

Run from apps/api (or wherever Django settings are configured):

  python manage.py fix_legacy_massage_end_times
  python manage.py fix_legacy_massage_end_times --dry-run
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.clinic.models import Appointment, Service
from apps.clinic.public_booking_service import MASSAGE_PUBLIC_BOOKING_BUFFER_AFTER_MINUTES


def _span_minutes(appointment: Appointment) -> int:
    """Calendar span from start_time to end_time; treats end before start as next calendar day."""
    start = datetime.combine(appointment.appointment_date, appointment.start_time)
    end = datetime.combine(appointment.appointment_date, appointment.end_time)
    if end <= start:
        end += timedelta(days=1)
    return int((end - start).total_seconds() // 60)


def _new_end_time_after_buffer(appointment: Appointment, duration: int, buffer_minutes: int) -> time:
    """start + duration + buffer as time on appointment_date (same cap as availability for midnight wrap)."""
    start = datetime.combine(appointment.appointment_date, appointment.start_time)
    new_end = start + timedelta(minutes=duration + buffer_minutes)
    if new_end.date() != appointment.appointment_date:
        return time(23, 59)
    t = new_end.time()
    return time(hour=t.hour, minute=t.minute, second=t.second)


class Command(BaseCommand):
    help = (
        "Update future massage appointments whose end_time equals start + duration only "
        f"to start + duration + {MASSAGE_PUBLIC_BOOKING_BUFFER_AFTER_MINUTES} minutes (public booking buffer)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change without writing to the database.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        today = timezone.localdate()
        buffer_m = MASSAGE_PUBLIC_BOOKING_BUFFER_AFTER_MINUTES

        qs = (
            Appointment.objects.filter(
                appointment_date__gte=today,
                booked_service__service_type=Service.ServiceType.MASSAGE,
            )
            .select_related("booked_service")
            .order_by("id")
        )

        total_scanned = 0
        legacy_count = 0
        already_correct = 0
        unexpected: list[str] = []
        to_update: list[Appointment] = []

        for appt in qs.iterator():
            total_scanned += 1
            svc = appt.booked_service
            if not svc:
                unexpected.append(f"id={appt.pk} skipped: missing booked_service")
                continue

            duration = int(svc.duration_minutes)
            span = _span_minutes(appt)

            if span == duration + buffer_m:
                already_correct += 1
                continue
            if span == duration:
                legacy_count += 1
                old_end = appt.end_time
                new_end = _new_end_time_after_buffer(appt, duration, buffer_m)
                appt.end_time = new_end
                to_update.append(appt)
                if self.verbosity >= 2:
                    self.stdout.write(
                        f"  would update id={appt.pk} {appt.appointment_date} "
                        f"{appt.start_time} end {old_end} -> {new_end} "
                        f"(duration={duration}m + buffer={buffer_m}m)"
                    )
                continue

            unexpected.append(
                f"id={appt.pk} date={appt.appointment_date} start={appt.start_time} end={appt.end_time} "
                f"span={span}m expected legacy={duration}m or correct={duration + buffer_m}m"
            )

        updated_count = 0
        failed: list[str] = []

        if not dry_run and to_update:
            try:
                now = timezone.now()
                for appt in to_update:
                    appt.updated_at = now
                with transaction.atomic():
                    Appointment.objects.bulk_update(to_update, ["end_time", "updated_at"])
                    updated_count = len(to_update)
            except Exception as exc:
                failed.append(f"bulk_update failed: {exc!r}")
                updated_count = 0
        elif dry_run:
            updated_count = 0
            if to_update and self.verbosity >= 1:
                for appt in to_update:
                    self.stdout.write(
                        f"[dry-run] id={appt.pk} {appt.appointment_date} "
                        f"new end_time={appt.end_time}"
                    )

        # Summary
        self.stdout.write("")
        self.stdout.write(self.style.NOTICE("— Summary —"))
        self.stdout.write(f"  Massage appointments scanned (date >= {today}): {total_scanned}")
        self.stdout.write(f"  Legacy pattern (span == duration only): {legacy_count}")
        if dry_run:
            self.stdout.write(self.style.WARNING(f"  Would update: {len(to_update)} (dry-run — no DB writes)"))
        else:
            self.stdout.write(f"  Updated: {updated_count}")
        self.stdout.write(f"  Skipped (already had duration + {buffer_m}m span): {already_correct}")
        if unexpected:
            self.stdout.write(self.style.WARNING(f"  Skipped (unexpected span / other): {len(unexpected)}"))
            for line in unexpected:
                self.stdout.write(f"    · {line}")
        else:
            self.stdout.write("  Skipped (unexpected span / other): 0")
        if failed:
            self.stdout.write(self.style.ERROR(f"  Failed: {len(failed)}"))
            for line in failed:
                self.stdout.write(self.style.ERROR(f"    · {line}"))
        else:
            self.stdout.write("  Failed: 0")

        if dry_run:
            self.stdout.write(self.style.WARNING("\nDry-run complete — re-run without --dry-run to apply."))
