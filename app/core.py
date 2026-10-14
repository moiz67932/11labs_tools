"""Shared validation, Supabase REST access and email utilities.

No supabase-py dependency: HTTPX keeps a pooled HTTP connection and supports both
legacy service-role JWTs and new sb_secret_ API keys. Never expose these keys.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import html
import json
import logging
import os
import re
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from dotenv import load_dotenv
from fastapi import HTTPException

load_dotenv()
logger = logging.getLogger("portive")
_client: httpx.Client | None = None


def http_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=httpx.Timeout(6.0), follow_redirects=False)
    return _client


def env(name: str, *, required: bool = True) -> str:
    value = os.getenv(name, "").strip()
    if not value and required:
        raise HTTPException(status_code=503, detail=f"Server configuration missing: {name}")
    return value


def require_tool_key(value: str | None) -> None:
    secret = env("PORTIVE_TOOL_API_KEY")
    if not value or not hmac.compare_digest(value, secret):
        raise HTTPException(status_code=401, detail="Unauthorized tool request")


def normalize_phone(value: str) -> str:
    """Normalize US CRM numbers and explicit international +E.164 numbers.

    Requires '+' for international destinations other than 10/11-digit NANP.
    This validates E.164 *shape*, not carrier assignment or call eligibility.
    """
    raw = (value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        result = "+" + digits
    elif len(digits) == 10:
        result = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        result = "+" + digits
    else:
        raise ValueError("Phone must be +E.164, or a 10/11-digit North American number")
    if not re.fullmatch(r"\+[1-9]\d{7,14}", result):
        raise ValueError("Invalid E.164 phone number")
    return result


def _sb_headers(*, prefer: str | None = None) -> dict[str, str]:
    key = env("SUPABASE_SERVICE_ROLE_KEY")
    headers = {"apikey": key, "Accept": "application/json", "Content-Type": "application/json"}
    # New Supabase sb_secret_* keys are authenticated via the apikey header.
    # Old service_role JWT keys can also be used as Bearer credentials.
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    if prefer:
        headers["Prefer"] = prefer
    return headers


def sb(method: str, endpoint: str, *, params: dict | None = None,
       body: dict | None = None, prefer: str | None = None,
       timeout: float | None = None):
    """Make a server-side Supabase REST request.

    ``timeout`` is deliberately opt-in: the established tool routes retain their
    six-second behaviour, while call-start lookups can use a much smaller bound.
    """
    url = env("SUPABASE_URL").rstrip("/") + "/rest/v1/" + endpoint.lstrip("/")
    try:
        response = http_client().request(method, url, params=params, json=body,
                                         headers=_sb_headers(prefer=prefer), timeout=timeout)
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        logger.error("Supabase operation failed: %s %s [%s]", method, endpoint, type(exc).__name__)
        raise HTTPException(502, detail="Database request failed; action not confirmed") from exc


def lead_for_call(lead_id: str, called_number: str) -> tuple[dict | None, str]:
    phone = normalize_phone(called_number)
    rows = sb("GET", "leads", params={"id": f"eq.{lead_id}",
                                       "select": "id,phone_number,business_name,contact_name", "limit": "1"})
    if not rows:
        return None, phone
    lead = rows[0]
    try:
        stored_phone = normalize_phone(lead["phone_number"])
    except (ValueError, TypeError):
        return None, phone
    if stored_phone != phone:
        return None, phone
    return lead, phone


def is_dnc(phone_e164: str) -> bool:
    rows = sb("GET", "portive_dnc", params={"phone_e164": f"eq.{phone_e164}",
                                              "select": "phone_e164", "limit": "1"})
    return bool(rows)


def is_email_suppressed(email: str) -> bool:
    rows = sb("GET", "portive_email_suppressions", params={"email": f"eq.{email}",
                                                             "select": "email", "limit": "1"})
    return bool(rows)


def parse_callback(value: str, zone: str) -> datetime:
    try:
        local = datetime.fromisoformat(value.replace("Z", "+00:00"))
        tz = ZoneInfo(zone)
        if local.utcoffset() is None:
            raise ValueError("Timezone offset required")
        in_zone = local.astimezone(tz)
        # Reject offset mistakes and daylight-saving transitions with invalid wall time.
        if (in_zone.date(), in_zone.hour, in_zone.minute, in_zone.utcoffset()) != (local.date(), local.hour, local.minute, local.utcoffset()):
            raise ValueError("Local time/UTC offset does not match business timezone")
        now = datetime.now(timezone.utc)
        utc = local.astimezone(timezone.utc)
        if not now + timedelta(seconds=30) < utc < now + timedelta(days=366):
            raise ValueError("Callback must be in the future and within one year")
        return utc
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError(f"Invalid callback datetime/timezone: {exc}") from exc


def idempotency_key(*parts: str) -> str:
    return "portive-" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def unsubscribe_token(address: str) -> str:
    secret = env("UNSUBSCRIBE_SIGNING_SECRET")
    email = address.strip().lower()
    encoded = base64.urlsafe_b64encode(email.encode("utf-8")).decode().rstrip("=")
    signature = hmac.new(secret.encode(), email.encode(), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def verify_unsubscribe_token(token: str) -> str:
    try:
        encoded, signature = token.split(".", 1)
        email = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
            raise ValueError("Invalid email")
        expected = unsubscribe_token(email)
        if not hmac.compare_digest(token, expected):
            raise ValueError("Invalid signature")
        return email
    except Exception as exc:
        # Token is untrusted; avoid returning its contents.
        raise ValueError("Invalid unsubscribe token") from exc


def overview_email(*, recipient_name: str, business_name: str, recipient_email: str) -> dict:
    public_base_url = env("PUBLIC_BASE_URL").rstrip("/")
    if not public_base_url.startswith("https://"):
        raise HTTPException(503, detail="PUBLIC_BASE_URL must be HTTPS")
    company_address = env("BUSINESS_POSTAL_ADDRESS")
    unsubscribe_url = public_base_url + "/unsubscribe?token=" + quote(unsubscribe_token(recipient_email))
    first_name = recipient_name.strip().split(" ")[0] if recipient_name.strip() else "there"
    name = html.escape(first_name)
    company = html.escape(business_name or "your spa")
    footer = html.escape(company_address)
    safe_link = html.escape(unsubscribe_url, quote=True)
    subject = "Portive | Receptionist & messaging overview"
    body = f"""<p>Hi {name},</p>
<p>Thanks for requesting information about Portive for {company}.</p>
<p>Portive's AI receptionist system helps med spas respond to incoming and missed calls, after-hours enquiries,
Instagram DMs and comments, WhatsApp, and SMS. It can also help guide appointment-booking
conversations around your existing workflow. Specific integrations are confirmed during setup.</p>
<p>Moiz can give you an optional 5–10 minute demonstration and answer questions about your setup.</p>
<p>Best,<br>Portive Team</p>
<hr><p style='font-size:12px'>{footer}<br>
<a href='{safe_link}'>Unsubscribe from marketing email</a></p>"""
    text = f"""Hi {first_name},

Thanks for requesting information about Portive for {business_name or 'your spa'}.

Portive's AI receptionist system helps med spas respond to incoming and missed
calls, after-hours enquiries, Instagram DMs and comments, WhatsApp, and SMS.
It can also help guide appointment-booking conversations around your existing
workflow. Specific integrations are confirmed during setup.

Moiz can give you an optional 5–10 minute demonstration and answer questions
about your setup.

Best,
Portive Team

{company_address}
Unsubscribe from marketing email: {unsubscribe_url}
"""
    return {"subject": subject, "text": text, "html": body, "unsubscribe_url": unsubscribe_url}
