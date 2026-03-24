"""Sync appointments to each provider's personal Google Calendar (OAuth per doctor)."""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from django.conf import settings

if TYPE_CHECKING:
    from .models import Appointment

logger = logging.getLogger(__name__)

CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def google_oauth_configured() -> bool:
    cid = (getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", None) or "").strip()
    sec = (getattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", None) or "").strip()
    redir = (getattr(settings, "GOOGLE_OAUTH_REDIRECT_URI", None) or "").strip()
    return bool(cid and sec and redir)


def _web_client_config() -> dict:
    return {
        "web": {
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID.strip(),
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET.strip(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.GOOGLE_OAUTH_REDIRECT_URI.strip()],
        }
    }


def build_oauth_flow():
    from google_auth_oauthlib.flow import Flow

    if not google_oauth_configured():
        raise RuntimeError("Google OAuth is not configured.")
    return Flow.from_client_config(
        _web_client_config(),
        scopes=[CALENDAR_EVENTS_SCOPE],
        redirect_uri=settings.GOOGLE_OAUTH_REDIRECT_URI.strip(),
    )


def exchange_oauth_code(*, authorization_response_url: str, state: str) -> int:
    """
    Validate signed state (user id), exchange code, save refresh token on Provider.
    Returns the Django user id that connected.
    """
    from django.contrib.auth import get_user_model
    from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

    from .models import Provider

    signer = TimestampSigner(salt="google-calendar-oauth")
    try:
        user_id = int(signer.unsign(state, max_age=900))
    except (BadSignature, SignatureExpired, ValueError) as exc:
        raise ValueError("Invalid or expired OAuth state.") from exc

    User = get_user_model()
    user = User.objects.filter(pk=user_id).first()
    if not user or user.role != "doctor":
        raise ValueError("Invalid user for calendar connection.")

    flow = build_oauth_flow()
    flow.fetch_token(authorization_response=authorization_response_url)
    creds = flow.credentials
    refresh = creds.refresh_token
    if not refresh:
        raise ValueError("Google did not return a refresh token. Try revoking app access in Google Account settings and connect again.")

    provider = Provider.objects.filter(user=user).first()
    if not provider:
        raise ValueError("No provider profile for this user.")

    provider.google_refresh_token = refresh
    if not (provider.google_calendar_id or "").strip():
        provider.google_calendar_id = "primary"
    provider.save(update_fields=["google_refresh_token", "google_calendar_id", "updated_at"])
    return user_id


def _calendar_service_for_provider(provider):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token = (provider.google_refresh_token or "").strip()
    if not token:
        return None
    creds = Credentials(
        token=None,
        refresh_token=token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID.strip(),
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET.strip(),
        scopes=[CALENDAR_EVENTS_SCOPE],
    )
    creds.refresh(Request())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _clinic_tz() -> ZoneInfo:
    return ZoneInfo(getattr(settings, "CLINIC_TIMEZONE", "America/Detroit") or "America/Detroit")


def _appointment_datetimes(appt: Appointment) -> tuple[dt.datetime, dt.datetime]:
    tz = _clinic_tz()
    start = dt.datetime.combine(appt.appointment_date, appt.start_time, tzinfo=tz)
    end = dt.datetime.combine(appt.appointment_date, appt.end_time, tzinfo=tz)
    if end <= start:
        end = start + dt.timedelta(hours=1)
    return start, end


def sync_appointment_to_google(appt: Appointment) -> str | None:
    """
    Create or update a Google Calendar event. Deletes event if appointment is cancelled/no-show.
    Returns 'skipped' | 'deleted' | 'upserted' | error string.
    """
    from .models import Appointment

    provider = appt.provider
    if not (provider.google_refresh_token or "").strip():
        return "skipped"

    cal_id = (provider.google_calendar_id or "").strip() or "primary"

    if appt.status in (Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW):
        return _delete_google_event(appt, cal_id) or "deleted"

    service = _calendar_service_for_provider(provider)
    if not service:
        return "skipped"

    start, end = _appointment_datetimes(appt)
    patient = appt.patient
    svc_name = appt.booked_service.name if appt.booked_service else "Appointment"
    summary = f"{patient.first_name} {patient.last_name} — {svc_name}"
    description = (
        f"ChiroFlow appointment #{appt.id}\n"
        f"Patient phone: {patient.phone}\n"
        f"Status: {appt.get_status_display()}"
    )
    body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "reminders": {"useDefault": True},
    }

    try:
        if appt.google_calendar_event_id:
            service.events().update(
                calendarId=cal_id,
                eventId=appt.google_calendar_event_id,
                body=body,
            ).execute()
            return "upserted"
        created = service.events().insert(calendarId=cal_id, body=body).execute()
        eid = created.get("id")
        if eid:
            from .models import Appointment

            Appointment.objects.filter(pk=appt.pk).update(google_calendar_event_id=eid)
            appt.google_calendar_event_id = eid
        return "upserted"
    except Exception:
        logger.exception("Google Calendar sync failed for appointment %s", appt.pk)
        return "error"


def _delete_google_event(appt: Appointment, cal_id: str) -> str | None:
    from googleapiclient.errors import HttpError

    from .models import Appointment

    eid = (appt.google_calendar_event_id or "").strip()
    if not eid:
        return "deleted"
    provider = appt.provider
    service = _calendar_service_for_provider(provider)
    if not service:
        Appointment.objects.filter(pk=appt.pk).update(google_calendar_event_id="")
        appt.google_calendar_event_id = ""
        return "deleted"
    try:
        service.events().delete(calendarId=cal_id, eventId=eid).execute()
    except HttpError as exc:
        code = getattr(exc, "status_code", None) or getattr(getattr(exc, "resp", None), "status", None)
        if code != 404:
            logger.warning("Google Calendar delete failed appt=%s: %s", appt.pk, exc)
    except Exception as exc:
        logger.warning("Google Calendar delete failed appt=%s: %s", appt.pk, exc)
    Appointment.objects.filter(pk=appt.pk).update(google_calendar_event_id="")
    appt.google_calendar_event_id = ""
    return "deleted"


def delete_appointment_google_event_before_db_delete(appt: Appointment) -> None:
    """Synchronous delete when removing an appointment from the database."""
    from .models import Appointment

    if not (appt.google_calendar_event_id or "").strip():
        return
    if not (appt.provider.google_refresh_token or "").strip():
        return
    cal_id = (appt.provider.google_calendar_id or "").strip() or "primary"
    _delete_google_event(appt, cal_id)
