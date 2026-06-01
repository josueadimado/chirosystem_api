"""Patient SMS/email when a visit is marked no-show (manual or automatic)."""

from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings as django_settings
from django.core.mail import send_mail
from django.utils import timezone

from apps.clinic.models import Appointment, ClinicSettings, Invoice
from apps.clinic.patient_communication_prefs import patient_wants_bill_email, patient_wants_bill_sms
from apps.clinic.twilio_sms import no_show_fee_notice_sms_body, send_sms, twilio_configured
from apps.clinic.utils import format_time_12h, format_usd_plain

logger = logging.getLogger(__name__)


def _smtp_configured() -> bool:
    return bool((getattr(django_settings, "EMAIL_HOST", "") or "").strip())


def send_no_show_patient_notice(
    appointment: Appointment,
    *,
    invoice: Invoice | None,
    card_charged: bool,
) -> dict[str, int | bool]:
    """
    Notify patient per notify_bills preferences (SMS and/or email).
    Returns counts/flags for logging.
    """
    patient = appointment.patient
    solo = ClinicSettings.get_solo()
    clinic_phone = (solo.phone or "269-408-0303").strip()
    service_name = (
        appointment.booked_service.label_for_public_booking()
        if appointment.booked_service
        else "appointment"
    )
    date_disp = appointment.appointment_date.strftime("%A, %B %d, %Y")
    time_disp = format_time_12h(appointment.start_time)
    provider_display = str(appointment.provider)
    first = (patient.first_name or "").strip() or "there"

    fee_amt = invoice.total_amount if invoice else Decimal("0")
    fee_disp = format_usd_plain(fee_amt) if fee_amt > 0 else ""

    sms_sent = 0
    email_sent = 0
    twilio_on = twilio_configured()
    smtp_on = _smtp_configured()

    if (
        twilio_on
        and patient_wants_bill_sms(patient)
        and appointment.no_show_notice_sms_at is None
    ):
        to_phone = (patient.phone or "").strip()
        if to_phone:
            body = no_show_fee_notice_sms_body(
                first_name=first,
                service_name=service_name,
                appt_date_display=date_disp,
                appt_time_display=time_disp,
                provider_display=provider_display,
                fee_display=fee_disp,
                card_charged=card_charged,
                clinic_phone=clinic_phone,
            )
            sid = send_sms(to_e164=to_phone, body=body)
            if sid:
                updated = Appointment.objects.filter(
                    pk=appointment.pk,
                    no_show_notice_sms_at__isnull=True,
                ).update(no_show_notice_sms_at=timezone.now())
                if updated:
                    sms_sent = 1

    if (
        smtp_on
        and patient_wants_bill_email(patient)
        and appointment.no_show_notice_email_at is None
    ):
        em = (patient.email or "").strip()
        if em:
            if card_charged and fee_disp:
                pay_line = f"We charged the card on file for {fee_disp}."
            elif fee_disp:
                pay_line = f"A no-show fee of {fee_disp} is due. Please contact us to pay or discuss."
            else:
                pay_line = "Please contact us if you have questions about this visit."

            subject = f"Missed appointment — {service_name}"
            msg = (
                f"Hi {first},\n\n"
                f"Our records show you missed your scheduled {service_name} appointment on "
                f"{date_disp} at {time_disp} with {provider_display}.\n\n"
                f"{pay_line}\n\n"
                f"Call us at {clinic_phone} or reply to this email if you need to reschedule.\n\n"
                f"— {solo.clinic_name or 'Relief Chiropractic'}"
            )
            try:
                send_mail(
                    subject=subject,
                    message=msg,
                    from_email=django_settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[em],
                    fail_silently=False,
                )
                updated = Appointment.objects.filter(
                    pk=appointment.pk,
                    no_show_notice_email_at__isnull=True,
                ).update(no_show_notice_email_at=timezone.now())
                if updated:
                    email_sent = 1
            except Exception:
                logger.exception(
                    "Auto no-show notice email failed: appt=%s to=%s",
                    appointment.pk,
                    em,
                )

    return {
        "sms_sent": sms_sent,
        "email_sent": email_sent,
        "twilio": twilio_on,
        "smtp": smtp_on,
    }
