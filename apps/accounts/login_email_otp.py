"""Email verification code after successful password check (staff / doctor login)."""

from __future__ import annotations

import logging
import os
import secrets
from typing import TYPE_CHECKING

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.core.mail import send_mail

if TYPE_CHECKING:
    from .models import User

logger = logging.getLogger(__name__)

_CHALLENGE_SALT = "chiroflow.login.challenge"
_CACHE_PREFIX = "login_otp:"
_CODE_TTL = 600  # 10 minutes


def login_email_verification_enabled() -> bool:
    raw = os.getenv("LOGIN_EMAIL_VERIFICATION", "true").strip().lower()
    return raw in ("1", "true", "yes")


def should_send_login_otp(user: "User") -> bool:
    if not login_email_verification_enabled():
        return False
    # Clinic owner/admin signs in with password only (no email code).
    from .models import User as UserModel

    if getattr(user, "role", None) == UserModel.Roles.OWNER_ADMIN:
        return False
    email = (user.email or "").strip()
    if not email:
        return False
    return True


def mask_email(email: str) -> str:
    email = (email or "").strip()
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}***@{domain}"


def create_login_challenge(user: "User") -> tuple[str, str]:
    """
    Store a 6-digit code and return (verification_token, plaintext_code).
    verification_token is signed; code is emailed to the user.
    """
    code = f"{secrets.randbelow(1_000_000):06d}"
    cache.set(f"{_CACHE_PREFIX}{user.pk}", code, timeout=_CODE_TTL)
    token = signing.dumps({"uid": user.pk}, salt=_CHALLENGE_SALT)
    return token, code


def send_login_code_email(*, user: "User", code: str) -> None:
    subject = "Your sign-in verification code"
    body = (
        f"Hello {user.full_name or user.username},\n\n"
        f"Your verification code is: {code}\n\n"
        "It expires in 10 minutes. If you did not try to sign in, ignore this email.\n"
    )
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or "noreply@localhost"
    send_mail(subject, body, from_email, [user.email.strip()], fail_silently=False)


def verify_login_challenge(verification_token: str, code: str) -> "User | None":
    from .models import User

    try:
        data = signing.loads(verification_token, salt=_CHALLENGE_SALT, max_age=_CODE_TTL + 60)
    except signing.BadSignature:
        return None
    uid = data.get("uid")
    if not isinstance(uid, int):
        return None
    cached = cache.get(f"{_CACHE_PREFIX}{uid}")
    if not cached or (code or "").strip() != str(cached).strip():
        return None
    cache.delete(f"{_CACHE_PREFIX}{uid}")
    try:
        return User.objects.get(pk=uid, is_active=True)
    except User.DoesNotExist:
        return None


def clear_login_challenge(user_id: int) -> None:
    cache.delete(f"{_CACHE_PREFIX}{user_id}")
