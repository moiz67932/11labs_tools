"""Gmail send-only OAuth transport. No token files or automatic send retries."""
from __future__ import annotations

import base64
from email.message import EmailMessage
from email.policy import SMTP
from types import SimpleNamespace

import httpx
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
from pydantic import EmailStr, TypeAdapter, ValidationError

from .core import env, http_client

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


class GmailError(Exception):
    def __init__(self, code: str, *, uncertain: bool = False):
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


def gmail_config() -> dict:
    names = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN", "GMAIL_SENDER_EMAIL")
    config = {name: env(name, required=False) for name in names}
    if not all(config.values()):
        raise GmailError("gmail_configuration_missing")
    config["GMAIL_REPLY_TO"] = env("GMAIL_REPLY_TO", required=False) or config["GMAIL_SENDER_EMAIL"]
    try:
        for name in ("GMAIL_SENDER_EMAIL", "GMAIL_REPLY_TO"):
            config[name] = str(TypeAdapter(EmailStr).validate_python(config[name]))
    except ValidationError:
        raise GmailError("gmail_configuration_invalid") from None
    return config


def _oauth_request(url, method="GET", body=None, headers=None, timeout=None, **kwargs):
    """Adapt the existing HTTPX pool to google-auth's Request protocol."""
    try:
        response = http_client().request(method, url, content=body, headers=headers, timeout=6.0)
    except httpx.HTTPError:
        raise TransportError("OAuth transport failed") from None
    return SimpleNamespace(status=response.status_code, data=response.content, headers=response.headers)


def send_gmail(*, config: dict, recipient: str, content: dict) -> str:
    message = EmailMessage(policy=SMTP)
    message["From"] = f"Portive <{config['GMAIL_SENDER_EMAIL']}>"
    message["To"] = recipient
    message["Reply-To"] = config["GMAIL_REPLY_TO"]
    message["Subject"] = content["subject"]
    message["List-Unsubscribe"] = f"<{content['unsubscribe_url']}>"
    message.set_content(content["text"])
    message.add_alternative(content["html"], subtype="html")
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    credentials = Credentials(
        token=None, refresh_token=config["GOOGLE_REFRESH_TOKEN"],
        token_uri=TOKEN_URL, client_id=config["GOOGLE_CLIENT_ID"],
        client_secret=config["GOOGLE_CLIENT_SECRET"], scopes=[GMAIL_SCOPE],
    )
    try:
        # Each invocation refreshes in memory. Refresh retries never send email.
        credentials.refresh(_oauth_request)
    except RefreshError as exc:
        raise GmailError("gmail_oauth_unavailable" if exc.retryable else "gmail_oauth_invalid") from None
    except (TransportError, ValueError):
        raise GmailError("gmail_oauth_unavailable") from None
    if not credentials.token:
        raise GmailError("gmail_oauth_invalid")
    if credentials.granted_scopes is not None and GMAIL_SCOPE not in credentials.granted_scopes:
        raise GmailError("gmail_permission_denied")
    try:
        response = http_client().post(
            SEND_URL, json={"raw": raw},
            headers={"Authorization": f"Bearer {credentials.token}"}, timeout=8.0,
        )
        if response.status_code >= 500:
            raise GmailError("email_result_uncertain", uncertain=True)
        if not response.is_success:
            try:
                error = response.json().get("error", {})
                reasons = {item.get("reason") for item in error.get("errors", [])}
            except (ValueError, AttributeError, TypeError):
                reasons = set()
            if response.status_code == 429 or reasons & {
                "rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded", "quotaExceeded"
            }:
                raise GmailError("gmail_rate_limited")
            if response.status_code == 401:
                raise GmailError("gmail_oauth_invalid")
            if response.status_code == 403:
                raise GmailError("gmail_permission_denied")
            raise GmailError("gmail_send_rejected")
        data = response.json()
        provider_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise GmailError("email_result_uncertain", uncertain=True)
        return provider_id
    except (httpx.HTTPError, ValueError):
        raise GmailError("email_result_uncertain", uncertain=True) from None
