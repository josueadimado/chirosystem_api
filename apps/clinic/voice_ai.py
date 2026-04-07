"""
Turn caller speech into structured booking fields.

Local parsers handle name, service, provider, and common date/time patterns
instantly (no API call). OpenAI is only used as a fallback for ambiguous
date/time expressions like "the week after next" that regex can't handle.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings

from .models import Service

logger = logging.getLogger(__name__)

# ─── Catalog ───────────────────────────────────────────────────────────

def _booking_catalog_json() -> dict[str, Any]:
    """Same shape as /api/v1/booking-options/ (minimal fields for the model)."""
    bookable = Service.objects.filter(is_active=True, show_in_public_booking=True).order_by("name")
    services = []
    providers_by_service: dict[int, list[dict]] = {}
    for svc in bookable:
        services.append(
            {
                "id": svc.id,
                "name": svc.name,
                "duration_minutes": svc.duration_minutes,
                "price": str(svc.price),
                "service_type": svc.service_type,
                "allow_provider_choice": svc.service_type == "massage",
            }
        )
        providers_by_service[svc.id] = [
            {"id": p.id, "provider_name": str(p)}
            for p in svc.providers.filter(active=True)
        ]
    return {"services": services, "providers_by_service": providers_by_service}


# ─── Name extraction (no AI) ──────────────────────────────────────────

_NAME_PREFIXES = [
    "hi my name is", "hello my name is", "hey my name is",
    "good morning my name is", "good afternoon my name is",
    "yes my name is", "yeah my name is", "so my name is",
    "um my name is", "uh my name is", "my name is",
    "hi this is", "hello this is", "hey this is", "yes this is",
    "hi i'm", "hello i'm", "hey i'm", "good morning i'm",
    "i'm calling my name is", "i'm calling this is",
    "this is", "i'm", "i am", "it's",
    "hi it's", "hi i am", "hello i am",
    "the name is", "name is", "you can call me",
    "hi", "hello", "hey", "yes", "yeah", "um", "uh", "so",
    "good morning", "good afternoon", "good evening",
]

_NAME_SUFFIXES = [
    "please", "thank you", "thanks", "i'd like to book",
    "i want to book", "booking", "appointment", "i need an appointment",
    "calling to book", "calling to make", "calling for",
    "i'm calling to", "i'm calling for",
]

def extract_name_from_speech(speech: str) -> tuple[str, str]:
    """
    Pull first + last name from raw speech. Very forgiving — if there's
    any recognizable word left after stripping filler, it's treated as a name.
    """
    text = speech.strip()
    text = re.sub(r"[.,!?;:]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    lower = text.lower()
    for prefix in _NAME_PREFIXES:
        if lower.startswith(prefix):
            text = text[len(prefix):].strip()
            text = text.lstrip(",").lstrip("-").strip()
            lower = text.lower()
            break

    for suffix in _NAME_SUFFIXES:
        if lower.endswith(suffix):
            text = text[: -len(suffix)].strip().rstrip(",").rstrip("-").strip()
            lower = text.lower()

    text = re.sub(r"\b(um|uh|like|so|well|and|the)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()

    parts = [p for p in text.split() if len(p) > 0]
    if len(parts) >= 2:
        return parts[0].title(), " ".join(parts[1:]).title()
    if parts:
        return parts[0].title(), ""
    return "", ""


# ─── Service matching (no AI) ─────────────────────────────────────────

def match_service_from_speech(speech: str, services: list[dict]) -> dict | None:
    """
    Fuzzy-match a service from the caller's speech against known service names.
    Tries exact, contains, keyword overlap — all instantly, no API call.
    """
    s_lower = speech.lower().strip()
    s_lower = re.sub(r"^(i('d| would) like( (a|an|the))?\s*|i want( (a|an|the))?\s*|"
                     r"(can i|could i) (get|have|book)( (a|an|the))?\s*|"
                     r"(let('s| us) (do|go with)( (a|an|the))?\s*)|"
                     r"(the|a|an)\s+)", "", s_lower).strip()
    s_lower = re.sub(r"\s*(please|thanks|thank you)$", "", s_lower).strip()

    for svc in services:
        if svc["name"].lower() == s_lower:
            return svc

    for svc in services:
        if svc["name"].lower() in s_lower or s_lower in svc["name"].lower():
            return svc

    s_words = set(s_lower.split())
    best, best_score = None, 0
    for svc in services:
        name_words = set(svc["name"].lower().split())
        sig_overlap = len({w for w in (s_words & name_words) if len(w) > 2})
        if sig_overlap > best_score:
            best_score = sig_overlap
            best = svc

    if best and best_score >= 1:
        return best

    type_map = {"chiropractic": "chiropractic", "chiro": "chiropractic",
                "massage": "massage", "massages": "massage"}
    for word, stype in type_map.items():
        if word in s_lower:
            typed = [svc for svc in services if svc.get("service_type") == stype]
            if len(typed) == 1:
                return typed[0]
            if typed:
                return typed[0]

    return None


def match_services_from_speech(speech: str, services: list[dict]) -> list[dict]:
    """
    Detect one or more services from a single utterance.
    Handles phrases like "chiropractic and massage", "both", "I want a
    massage and also chiropractic", etc.
    Returns a deduplicated list of matched services (1 or more).
    """
    s_lower = speech.lower().strip()
    s_lower = re.sub(r"^(i('d| would) like( (a|an|the))?\s*|i want( (a|an|the))?\s*|"
                     r"(can i|could i) (get|have|book)( (a|an|the))?\s*|"
                     r"(let('s| us) (do|go with)( (a|an|the))?\s*)|"
                     r"(the|a|an)\s+)", "", s_lower).strip()
    s_lower = re.sub(r"\s*(please|thanks|thank you)$", "", s_lower).strip()

    both_keywords = {"both", "two", "all", "everything"}
    wants_both = any(kw in s_lower.split() for kw in both_keywords)

    type_map = {
        "chiropractic": "chiropractic", "chiro": "chiropractic",
        "massage": "massage", "massages": "massage",
    }

    detected_types: set[str] = set()
    for word, stype in type_map.items():
        if word in s_lower:
            detected_types.add(stype)

    if wants_both and not detected_types:
        detected_types = {"chiropractic", "massage"}

    if len(detected_types) >= 2:
        matched: list[dict] = []
        seen_ids: set[int] = set()
        for stype in detected_types:
            for svc in services:
                if svc.get("service_type") == stype and svc["id"] not in seen_ids:
                    matched.append(svc)
                    seen_ids.add(svc["id"])
                    break
        if matched:
            return matched

    name_matches: list[dict] = []
    seen_ids_2: set[int] = set()
    for svc in services:
        name_l = svc["name"].lower()
        if name_l in s_lower and svc["id"] not in seen_ids_2:
            name_matches.append(svc)
            seen_ids_2.add(svc["id"])
    if name_matches:
        return name_matches

    single = match_service_from_speech(speech, services)
    return [single] if single else []


# ─── Provider matching (no AI) ────────────────────────────────────────

def match_provider_from_speech(speech: str, providers: list[dict]) -> dict | None:
    """Match a provider name from raw speech — tries first name, last name, full name."""
    s_lower = speech.lower().strip()
    s_lower = re.sub(r"^(i('d| would) like\s*|i want\s*|"
                     r"(can i|could i) (see|get|have)\s*|"
                     r"(let('s| us) (go with|do)\s*))", "", s_lower).strip()
    s_lower = re.sub(r"\s*(please|thanks|thank you)$", "", s_lower).strip()

    for p in providers:
        if p["provider_name"].lower() == s_lower:
            return p

    for p in providers:
        if p["provider_name"].lower() in s_lower or s_lower in p["provider_name"].lower():
            return p

    for p in providers:
        name_parts = p["provider_name"].lower().split()
        if any(part in s_lower for part in name_parts if len(part) > 2):
            return p

    return None


# ─── Date / time parsing (local first, AI fallback) ───────────────────

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "weds": 2,
    "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
}

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}

_WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "twenty one": 21, "twenty two": 22, "twenty three": 23,
    "twenty four": 24, "twenty five": 25, "twenty six": 26,
    "twenty seven": 27, "twenty eight": 28, "twenty nine": 29,
    "thirty": 30, "thirty one": 31,
}

_MINUTE_WORDS = {
    "o'clock": 0, "oclock": 0, "o clock": 0,
    "oh five": 5, "oh-five": 5,
    "fifteen": 15, "thirty": 30, "forty five": 45, "forty-five": 45,
    "forty": 40, "fifty": 50, "twenty": 20,
}


def _normalize_speech(speech: str) -> str:
    """Lower-case and clean up speech for parsing."""
    s = speech.lower().strip()
    s = re.sub(r"[.,!?]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _next_weekday(today: date, weekday_num: int) -> date:
    """Return the next occurrence of the given weekday (0=Mon)."""
    days_ahead = weekday_num - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return today + timedelta(days=days_ahead)


def _replace_word_numbers(text: str) -> str:
    """Replace written-out numbers with digits in text for easier regex."""
    for word, num in sorted(_WORD_NUMS.items(), key=lambda x: -len(x[0])):
        text = text.replace(word, str(num))
    return text


def _parse_date_from_speech(speech: str, today: date) -> date | None:
    """Try to extract a date from natural speech. Returns None if can't parse."""
    s = _normalize_speech(speech)
    s = _replace_word_numbers(s)

    if "tomorrow" in s:
        return today + timedelta(days=1)
    if re.search(r"\btoday\b", s):
        return today

    for name, num in _WEEKDAYS.items():
        if re.search(rf"\bnext\s+{name}\b", s):
            d = _next_weekday(today, num)
            if d <= today:
                d += timedelta(days=7)
            return d
        if re.search(rf"\bthis\s+{name}\b", s):
            d = _next_weekday(today, num)
            return d

    for mname, mnum in sorted(_MONTHS.items(), key=lambda x: -len(x[0])):
        m = re.search(rf"\b{mname}\s+(\d{{1,2}})\b", s)
        if m:
            day = int(m.group(1))
            try:
                d = date(today.year, mnum, day)
                if d < today:
                    d = date(today.year + 1, mnum, day)
                return d
            except ValueError:
                continue

    m = re.search(r"\bthe\s+(\d{1,2})\s*(?:st|nd|rd|th)?\b", s)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            try:
                d = date(today.year, today.month, day)
                if d < today:
                    next_month = today.month + 1
                    next_year = today.year
                    if next_month > 12:
                        next_month = 1
                        next_year += 1
                    d = date(next_year, next_month, day)
                return d
            except ValueError:
                pass

    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", s)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            pass

    for name, num in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", s):
            return _next_weekday(today, num)

    return None


def _parse_time_from_speech(speech: str):
    """Try to extract a time from natural speech. Returns a time object or None."""
    s = _normalize_speech(speech)

    for word, mins in sorted(_MINUTE_WORDS.items(), key=lambda x: -len(x[0])):
        s = s.replace(word, f":{mins:02d}" if mins else ":00")

    s = _replace_word_numbers(s)

    s = re.sub(r"\ba\s*\.?\s*m\s*\.?\b", "am", s)
    s = re.sub(r"\bp\s*\.?\s*m\s*\.?\b", "pm", s)

    m = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)", s)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
        if ap == "pm" and h != 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return datetime.strptime(f"{h}:{mi:02d}", "%H:%M").time()

    m = re.search(r"(\d{1,2})\s*(am|pm)", s)
    if m:
        h, ap = int(m.group(1)), m.group(2)
        if ap == "pm" and h != 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if 0 <= h <= 23:
            return datetime.strptime(f"{h}:00", "%H:%M").time()

    m = re.search(r"\bat\s+(\d{1,2}):(\d{2})\b", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 7 <= h <= 12 and 0 <= mi <= 59:
            return datetime.strptime(f"{h}:{mi:02d}", "%H:%M").time()

    m = re.search(r"\bat\s+(\d{1,2})\b", s)
    if m:
        h = int(m.group(1))
        if 7 <= h <= 12:
            return datetime.strptime(f"{h}:00", "%H:%M").time()

    return None


def parse_datetime_from_speech(speech: str, today: date) -> tuple[str, str]:
    """
    Try to parse date and time from speech using local regex.
    Returns (date_iso, time_12h) — either or both may be empty if unparseable.
    """
    d = _parse_date_from_speech(speech, today)
    t = _parse_time_from_speech(speech)

    date_str = d.isoformat() if d else ""
    time_str = t.strftime("%I:%M %p") if t else ""

    logger.info("Local datetime parse: speech=%r → date=%s time=%s", speech[:120], date_str, time_str)
    return date_str, time_str


# ─── OpenAI fallback (only for tricky date/time) ──────────────────────

def openai_parse_datetime(speech: str, today_iso: str) -> tuple[str, str]:
    """
    Call OpenAI to parse a date/time that the local parser couldn't handle.
    Returns (date_iso, time_12h) — either or both may be empty.
    """
    key = getattr(settings, "OPENAI_API_KEY", "") or ""
    if not key.strip():
        return "", ""

    logger.info("OpenAI datetime fallback: speech=%r", speech[:200])

    instruction = (
        f"Today is {today_iso}. The caller said a date and/or time for a clinic appointment. "
        "Parse it and return JSON: "
        '{"appointment_date": "YYYY-MM-DD", "start_time": "H:MM AM"}. '
        "Handle phrases like 'tomorrow', 'next Tuesday', 'the 15th', 'Monday at 9', etc. "
        "If you can only determine the date or only the time, still return what you can "
        "and set the other to empty string."
    )

    body = json.dumps(
        {
            "model": getattr(settings, "OPENAI_VOICE_MODEL", "gpt-4o-mini"),
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": speech},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
            "max_tokens": 60,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        content = raw["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        logger.info("OpenAI datetime result: %r", parsed)
        return (
            (parsed.get("appointment_date") or "").strip(),
            (parsed.get("start_time") or "").strip(),
        )
    except Exception as e:
        logger.warning("OpenAI datetime fallback failed: %s", e)
        return "", ""


def openai_extract_field(transcript: str, *, field: str, instruction: str) -> dict[str, Any] | None:
    """General-purpose OpenAI call — kept for backward compatibility but rarely used now."""
    key = getattr(settings, "OPENAI_API_KEY", "") or ""
    if not key.strip():
        return None

    body = json.dumps(
        {
            "model": getattr(settings, "OPENAI_VOICE_MODEL", "gpt-4o-mini"),
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": transcript},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        content = raw["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        logger.warning("OpenAI extract_field (%s) failed: %s", field, e)
        return None


# ─── Time parser used at booking confirmation ─────────────────────────

def _parse_time_12h(s: str):
    """Parse a 12-hour time string like '2:30 PM' into a time object."""
    s = (s or "").strip()
    for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)$", s.lower())
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
        if ap == "pm" and h != 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        return datetime.strptime(f"{h}:{mi:02d}", "%H:%M").time()
    return None


# ─── Legacy helpers (kept for backward compat) ────────────────────────

def openai_parse_booking_intent(*, transcript: str, today_iso: str, catalog: dict[str, Any]) -> dict[str, Any] | None:
    """Call OpenAI; return parsed JSON dict or None on failure."""
    key = getattr(settings, "OPENAI_API_KEY", "") or ""
    if not key.strip():
        return None

    system = (
        "You help parse phone booking requests for a chiropractic clinic. "
        "Return ONLY valid JSON with keys: "
        "first_name (string), last_name (string), "
        "service_id (integer or null), service_name_hint (string or null), "
        "provider_id (integer or null), provider_name_hint (string or null), "
        "appointment_date (YYYY-MM-DD), start_time (12-hour string like \"2:30 PM\" or \"9:00 AM\"), "
        "notes (string, optional). "
        f"Today in the clinic calendar is {today_iso}. "
        "Interpret phrases like \"next Tuesday\" relative to that date. "
        "If something critical is missing, still return best-effort JSON with nulls."
    )
    user = json.dumps({"caller_said": transcript, "catalog": catalog}, default=str)

    body = json.dumps(
        {
            "model": getattr(settings, "OPENAI_VOICE_MODEL", "gpt-4o-mini"),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        logger.exception("OpenAI voice booking request failed: %s", e)
        return None

    try:
        content = raw["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError, TypeError) as e:
        logger.warning("OpenAI voice booking bad response: %s", e)
        return None


def _match_service(services: list[dict], service_id: int | None, name_hint: str | None) -> dict | None:
    if service_id is not None:
        for s in services:
            if s["id"] == int(service_id):
                return s
    if name_hint and str(name_hint).strip():
        hint = str(name_hint).lower().strip()
        for s in services:
            n = s["name"].lower()
            if hint in n or n in hint:
                return s
        for s in services:
            n = s["name"].lower()
            if any(word in n for word in hint.split() if len(word) > 3):
                return s
    return None


def _match_provider(
    providers: list[dict],
    provider_id: int | None,
    name_hint: str | None,
) -> int | None:
    if provider_id is not None:
        for p in providers:
            if p["id"] == int(provider_id):
                return int(provider_id)
    if name_hint and str(name_hint).strip():
        h = str(name_hint).lower()
        for p in providers:
            if h in p["provider_name"].lower():
                return int(p["id"])
    return None


def intent_to_booking_payload(
    intent: dict[str, Any],
    *,
    caller_e164: str,
    catalog: dict[str, Any],
) -> tuple[dict | None, str | None]:
    """
    Build PublicBookingSerializer payload from model output + catalog.
    Returns (payload, None) or (None, human-readable error).
    """
    services = catalog.get("services") or []
    if not services:
        return None, "No bookable services are configured."

    fn = (intent.get("first_name") or "").strip()
    ln = (intent.get("last_name") or "").strip()
    if not fn or not ln:
        return None, "missing_name"

    svc = _match_service(
        services,
        intent.get("service_id"),
        intent.get("service_name_hint"),
    )
    if not svc:
        return None, "missing_service"

    prov_id = None
    pbs = catalog.get("providers_by_service") or {}
    plist = pbs.get(svc["id"]) or []
    if svc.get("allow_provider_choice"):
        prov_id = _match_provider(plist, intent.get("provider_id"), intent.get("provider_name_hint"))
        if not prov_id and len(plist) == 1:
            prov_id = plist[0]["id"]
        if not prov_id:
            return None, "missing_provider"
    else:
        if plist:
            prov_id = plist[0]["id"]

    date_raw = intent.get("appointment_date") or ""
    try:
        appt_date = date.fromisoformat(str(date_raw)[:10])
    except ValueError:
        return None, "bad_date"

    t = _parse_time_12h(str(intent.get("start_time") or ""))
    if not t:
        return None, "bad_time"

    phone = caller_e164
    if phone.startswith("tel:"):
        phone = phone[4:]

    payload = {
        "first_name": fn[:100],
        "last_name": ln[:100],
        "phone": phone,
        "email": "",
        "service_id": svc["id"],
        "service_duration_minutes": int(svc["duration_minutes"]),
        "service_price": Decimal(str(svc["price"])),
        "appointment_date": appt_date,
        "start_time": t,
    }
    if prov_id is not None:
        payload["provider_id"] = int(prov_id)

    return payload, None
