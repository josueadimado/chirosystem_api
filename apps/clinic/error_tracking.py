"""Capture application errors for the admin error tracker."""

from __future__ import annotations

import hashlib
import json
import logging
import traceback
from typing import Any

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.utils import timezone
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)

ERROR_TRACKER_HEADER = "HTTP_X_ERROR_TRACKER_TOKEN"
ERROR_TRACKER_SIGNER_SALT = "chiroflow-error-tracker"

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "token",
        "access",
        "refresh",
        "authorization",
        "card",
        "card_number",
        "cvv",
        "ssn",
        "social_security",
        "square_card",
        "source_id",
        "payment_nonce",
    }
)


def _env_error_tracker_password() -> str:
    import os

    # Read at call time so container env vars are picked up after redeploy.
    raw = os.getenv("ERROR_TRACKER_PASSWORD")
    if raw is not None and str(raw).strip():
        return str(raw).strip()
    return (getattr(settings, "ERROR_TRACKER_PASSWORD", "") or "").strip()


def _db_error_tracker_password_hash() -> str:
    try:
        from apps.clinic.models import ClinicSettings

        solo = ClinicSettings.get_cached()
        return (solo.error_tracker_password_hash or "").strip()
    except Exception:
        logger.exception("error tracker: could not read ClinicSettings password hash")
        return ""


def error_tracker_password_source() -> str:
    """How the error tracker password is configured: env, database, or none."""
    if _env_error_tracker_password():
        return "env"
    if _db_error_tracker_password_hash():
        return "database"
    return "none"


def error_tracker_password_configured() -> bool:
    """True when env password or a saved owner-chosen password exists."""
    return error_tracker_password_source() != "none"


def error_tracker_password_ok(password: str) -> bool:
    import hmac

    from django.contrib.auth.hashers import check_password

    supplied = str(password or "").strip()
    if not supplied:
        return False
    env_password = _env_error_tracker_password()
    if env_password:
        return hmac.compare_digest(supplied, env_password.strip())
    stored_hash = _db_error_tracker_password_hash()
    if stored_hash:
        return check_password(supplied, stored_hash)
    return False


def set_error_tracker_password_in_db(password: str) -> None:
    """Owner setup: store a hashed password in ClinicSettings (used when env var is unset)."""
    from django.contrib.auth.hashers import make_password

    from apps.clinic.models import ClinicSettings

    solo = ClinicSettings.get_solo()
    solo.error_tracker_password_hash = make_password(password)
    solo.save(update_fields=["error_tracker_password_hash", "updated_at"])


def issue_error_tracker_token(user_id: int) -> str:
    signer = TimestampSigner(salt=ERROR_TRACKER_SIGNER_SALT)
    return signer.sign(f"etr:{user_id}")


def verify_error_tracker_token(token: str, user_id: int) -> bool:
    if not token or not user_id:
        return False
    signer = TimestampSigner(salt=ERROR_TRACKER_SIGNER_SALT)
    max_age = int(getattr(settings, "ERROR_TRACKER_TOKEN_MAX_AGE", 8 * 3600))
    try:
        value = signer.unsign(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return False
    return value == f"etr:{user_id}"


def error_tracker_token_from_request(request) -> str:
    meta = getattr(request, "META", {}) or {}
    return (meta.get(ERROR_TRACKER_HEADER) or request.headers.get("X-Error-Tracker-Token") or "").strip()


def request_has_error_tracker_access(request) -> bool:
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "role", None) != "owner_admin" and not getattr(user, "is_superuser", False):
        return False
    if not error_tracker_password_configured():
        return False
    token = error_tracker_token_from_request(request)
    return verify_error_tracker_token(token, user.pk)


def _redact_value(key: str, value: Any) -> Any:
    key_l = key.lower()
    if any(s in key_l for s in _SENSITIVE_KEYS):
        return "[redacted]"
    if key_l in ("phone", "patient_phone") and isinstance(value, str) and value.strip():
        digits = "".join(c for c in value if c.isdigit())
        if len(digits) >= 4:
            return f"***{digits[-4:]}"
        return "[redacted]"
    if key_l == "email" and isinstance(value, str) and "@" in value:
        local, _, domain = value.partition("@")
        if local:
            return f"{local[0]}***@{domain}"
        return "[redacted]"
    return value


def sanitize_mapping(data: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "[truncated]"
    if isinstance(data, dict):
        return {str(k): sanitize_mapping(v, depth=depth + 1) for k, v in data.items()}
    if isinstance(data, list):
        return [sanitize_mapping(item, depth=depth + 1) for item in data[:50]]
    if isinstance(data, (str, int, float, bool)) or data is None:
        return data
    return str(data)


def sanitize_request_payload(request) -> str:
    try:
        raw = getattr(request, "data", None)
        if raw is None:
            body = getattr(request, "body", b"") or b""
            if not body:
                return ""
            try:
                raw = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                text = body.decode("utf-8", errors="replace")
                return text[:4000] + ("…" if len(text) > 4000 else "")
        if hasattr(raw, "dict"):
            raw = raw.dict()
        cleaned = sanitize_mapping(raw)
        if isinstance(cleaned, dict):
            cleaned = {k: _redact_value(k, v) for k, v in cleaned.items()}
        text = json.dumps(cleaned, default=str)
        return text[:8000] + ("…" if len(text) > 8000 else "")
    except Exception:
        return ""


def _user_snapshot(request) -> tuple[int | None, str, str]:
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return None, "", ""
    display = (getattr(user, "full_name", "") or getattr(user, "username", "") or "").strip()
    role = str(getattr(user, "role", "") or "")
    return user.pk, role, display


def build_error_fingerprint(
    *,
    source: str,
    exception_type: str,
    message: str,
    path: str,
) -> str:
    base = f"{source}|{exception_type}|{message[:500]}|{path}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:40]


def _fingerprint_filter(fingerprint: str):
    from apps.clinic.models import SystemErrorLog

    if fingerprint:
        return SystemErrorLog.objects.filter(fingerprint=fingerprint)
    return SystemErrorLog.objects.none()


def fingerprint_occurrence_stats(row) -> dict:
    """Count and date range for all rows sharing this error fingerprint."""
    from django.db.models import Count, Min, Max

    from apps.clinic.models import SystemErrorLog

    if not row.fingerprint:
        return {
            "count": 1,
            "first_at": row.created_at,
            "last_at": row.created_at,
        }
    agg = SystemErrorLog.objects.filter(fingerprint=row.fingerprint).aggregate(
        count=Count("pk"),
        first_at=Min("created_at"),
        last_at=Max("created_at"),
    )
    return {
        "count": agg["count"] or 1,
        "first_at": agg["first_at"] or row.created_at,
        "last_at": agg["last_at"] or row.created_at,
    }


def fingerprint_occurrence_history(row, *, limit: int = 15) -> list[dict]:
    """Recent occurrences for the same fingerprint (newest first)."""
    if not row.fingerprint:
        return [
            {
                "id": row.pk,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "path": row.path,
                "http_method": row.http_method,
                "user_display": row.user_display,
                "auto_reopened": bool((row.extra or {}).get("auto_reopened")),
            }
        ]
    rows = (
        _fingerprint_filter(row.fingerprint)
        .order_by("-created_at")
        .values("id", "created_at", "path", "http_method", "user_display", "extra")[:limit]
    )
    out = []
    for item in rows:
        extra = item.get("extra") or {}
        out.append(
            {
                "id": item["id"],
                "created_at": item["created_at"].isoformat() if item["created_at"] else None,
                "path": item["path"],
                "http_method": item["http_method"],
                "user_display": item["user_display"],
                "auto_reopened": bool(extra.get("auto_reopened")),
            }
        )
    return out


def build_error_log_groups_queryset(*, resolved: str = "", source: str = "", search: str = ""):
    """
    One row per error fingerprint (latest occurrence), with occurrence_count annotated.
    Rows without a fingerprint are listed individually.
    """
    from django.db.models import Case, CharField, Count, F, Min, OuterRef, Q, Subquery, Value, When, Window
    from django.db.models.functions import Cast, Coalesce, NullIf, RowNumber

    from apps.clinic.models import SystemErrorLog

    base = SystemErrorLog.objects.all()
    if source:
        base = base.filter(source=source)
    if search:
        base = base.filter(
            Q(message__icontains=search)
            | Q(path__icontains=search)
            | Q(exception_type__icontains=search)
        )

    base = base.annotate(
        group_key=Coalesce(
            NullIf(F("fingerprint"), Value("")),
            Cast(F("pk"), CharField()),
        )
    )

    ranked = base.annotate(
        row_num=Window(
            expression=RowNumber(),
            partition_by=[F("group_key")],
            order_by=F("created_at").desc(),
        )
    ).filter(row_num=1)

    if resolved == "true":
        ranked = ranked.filter(resolved_at__isnull=False)
    elif resolved == "false":
        ranked = ranked.filter(resolved_at__isnull=True)

    count_subq = (
        SystemErrorLog.objects.filter(fingerprint=OuterRef("fingerprint"))
        .values("fingerprint")
        .annotate(c=Count("pk"))
        .values("c")[:1]
    )
    first_subq = (
        SystemErrorLog.objects.filter(fingerprint=OuterRef("fingerprint"))
        .values("fingerprint")
        .annotate(first_at=Min("created_at"))
        .values("first_at")[:1]
    )

    return ranked.annotate(
        occurrence_count=Case(
            When(fingerprint="", then=Value(1)),
            default=Subquery(count_subq),
        ),
        first_occurrence_at=Case(
            When(fingerprint="", then=F("created_at")),
            default=Subquery(first_subq),
        ),
    ).order_by("-created_at")


def count_open_error_groups() -> int:
    return build_error_log_groups_queryset(resolved="false").count()


def _maybe_auto_reopen_resolved_fingerprint(row) -> bool:
    """
    If this fingerprint was marked resolved and the same error happens again,
    reopen the whole group so it shows up as open in the tracker.
    """
    from apps.clinic.models import SystemErrorLog

    if not row.fingerprint:
        return False

    prior = (
        SystemErrorLog.objects.filter(fingerprint=row.fingerprint)
        .exclude(pk=row.pk)
        .order_by("-created_at")
        .first()
    )
    if not prior or prior.resolved_at is None:
        return False

    SystemErrorLog.objects.filter(fingerprint=row.fingerprint).update(
        resolved_at=None,
        resolved_by_id=None,
        resolution_notes="",
    )

    extra = dict(row.extra or {})
    extra["auto_reopened"] = True
    extra["auto_reopened_at"] = timezone.now().isoformat()
    row.extra = extra
    row.save(update_fields=["extra", "updated_at"])
    return True


def resolve_error_log_group(row, *, resolved_by_id: int, resolution_notes: str = "") -> None:
    """Mark every occurrence of this fingerprint as resolved."""
    notes = (resolution_notes or "").strip()
    qs = _fingerprint_filter(row.fingerprint) if row.fingerprint else SystemErrorLog.objects.filter(pk=row.pk)
    qs.update(
        resolved_at=timezone.now(),
        resolved_by_id=resolved_by_id,
        resolution_notes=notes,
    )


def reopen_error_log_group(row) -> None:
    """Reopen every occurrence of this fingerprint."""
    qs = _fingerprint_filter(row.fingerprint) if row.fingerprint else SystemErrorLog.objects.filter(pk=row.pk)
    qs.update(
        resolved_at=None,
        resolved_by_id=None,
        resolution_notes="",
    )


def capture_application_error(
    *,
    exc: BaseException | None = None,
    message: str = "",
    request=None,
    source: str = "api",
    level: str = "error",
    status_code: int | None = None,
    extra: dict | None = None,
    fingerprint_message: str | None = None,
    exception_type: str | None = None,
) -> int | None:
    """Persist one error row. Returns pk or None if logging failed."""
    from apps.clinic.models import SystemErrorLog

    tb_text = ""
    exc_type = (exception_type or "").strip()
    if exc is not None:
        exc_type = type(exc).__name__
        tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if not message:
            message = str(exc) or exc_type
    message = (message or "Unknown error")[:8000]
    tb_text = tb_text[:50000]

    http_method = ""
    path = ""
    query_string = ""
    request_body = ""
    user_id = None
    user_role = ""
    user_display = ""
    if request is not None:
        http_method = (getattr(request, "method", "") or "")[:10]
        path = (getattr(request, "path", "") or "")[:500]
        query_string = (getattr(request, "META", {}).get("QUERY_STRING", "") or "")[:1000]
        user_id, user_role, user_display = _user_snapshot(request)
        if http_method in ("POST", "PUT", "PATCH"):
            request_body = sanitize_request_payload(request)

    fingerprint = build_error_fingerprint(
        source=source,
        exception_type=exc_type,
        message=(fingerprint_message or message)[:500],
        path=path,
    )

    try:
        row = SystemErrorLog.objects.create(
            level=level,
            source=source,
            message=message,
            exception_type=exc_type[:200],
            traceback_text=tb_text,
            http_method=http_method,
            path=path,
            query_string=query_string,
            status_code=status_code,
            user_id=user_id,
            user_role=user_role[:30],
            user_display=user_display[:200],
            request_body=request_body,
            extra=extra or {},
            fingerprint=fingerprint,
        )
        try:
            _maybe_auto_reopen_resolved_fingerprint(row)
        except Exception:
            logger.exception("error tracker: auto-reopen failed for fingerprint %s", fingerprint)
        return row.pk
    except Exception:
        logger.exception("Could not save SystemErrorLog row")
        return None


def capture_payment_failure(
    *,
    request,
    operation: str,
    detail: str,
    error_code: str | None = None,
    invoice_id: int | None = None,
    patient_id: int | None = None,
    status_code: int = 400,
) -> int | None:
    """
    Log a handled payment failure (Square decline, nothing due, etc.) for Admin → Errors.

    Unlike crashes, these are expected business outcomes — logged as warnings so staff can
    review patterns without treating them as server outages.
    """
    extra: dict = {"category": "payment", "operation": operation}
    if invoice_id is not None:
        extra["invoice_id"] = invoice_id
    if patient_id is not None:
        extra["patient_id"] = patient_id
    if error_code:
        extra["error_code"] = (error_code or "")[:500]
    return capture_application_error(
        message=(detail or "Payment failed.")[:8000],
        request=request,
        source="api",
        level="warning",
        status_code=status_code,
        extra=extra,
        exception_type=f"Payment:{operation}"[:200],
        fingerprint_message=(error_code or detail or operation)[:500],
    )


def drf_exception_handler(exc, context):
    """Log API exceptions, then return DRF's normal response."""
    response = exception_handler(exc, context)
    request = context.get("request")
    try:
        if response is None:
            capture_application_error(exc=exc, request=request, source="api", status_code=500)
        elif response.status_code >= 500:
            capture_application_error(
                exc=exc,
                request=request,
                source="api",
                status_code=response.status_code,
            )
    except Exception:
        logger.exception("error tracker: failed during DRF exception handling")
    return response


class ErrorCaptureMiddleware:
    """Catch unhandled exceptions outside DRF (webhooks, voice, health checks)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        except Exception as exc:
            try:
                capture_application_error(exc=exc, request=request, source="middleware", status_code=500)
            except Exception:
                logger.exception("error tracker: middleware capture failed")
            raise
