import json
import logging

from django.utils.timezone import now

logger = logging.getLogger("hipaa.audit")

# URL fragments that touch Protected Health Information (PHI)
PHI_PATHS = [
    "/patients/",
    "/appointments/",
    "/billing/",
    "/records/",
    "/history/",
    "/messages/",
    "/kiosk/",
    "/doctor/",
    "/admin/",
]


class HIPAAAuditMiddleware:
    """
    Logs every request that touches PHI.
    Required for HIPAA §164.312(b) — Audit Controls.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        if any(p in request.path for p in PHI_PATHS):
            try:
                user = getattr(request, "user", None)
                logger.info(
                    json.dumps(
                        {
                            "timestamp": now().isoformat(),
                            "event": "phi_access",
                            "user_id": user.id if user and user.is_authenticated else "anonymous",
                            "user_email": str(user.email) if user and user.is_authenticated else "anonymous",
                            "method": request.method,
                            "path": request.path,
                            "ip": (
                                request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
                                or request.META.get("REMOTE_ADDR", "unknown")
                            ),
                            "status_code": response.status_code,
                            "user_agent": request.META.get("HTTP_USER_AGENT", ""),
                        }
                    )
                )
            except Exception:
                # Never let audit logging crash the actual request
                logger.error("HIPAA audit logging failed", exc_info=True)

        return response
