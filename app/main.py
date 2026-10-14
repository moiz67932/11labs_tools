"""ElevenLabs -> FastAPI -> existing ColdCallerCRM Supabase database.

Run: uvicorn app.main:app --host 0.0.0.0 --port 8000
Apply sql/001_portive_live_tools.sql before making tool calls.
"""
from __future__ import annotations

from datetime import datetime
import html
import logging
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from pydantic import BaseModel, EmailStr, Field

from .core import (
    idempotency_key, is_dnc, is_email_suppressed,
    lead_for_call, overview_email, parse_callback, require_tool_key, sb,
    verify_unsubscribe_token,
)

from .gmail import GmailError, gmail_config, send_gmail
from .webhooks import router as webhook_router

log = logging.getLogger("portive")
app = FastAPI(title="Portive ElevenLabs Live Tools", version="1.0.0", docs_url="/docs")
app.include_router(webhook_router)


@app.exception_handler(RequestValidationError)
async def overview_validation_error(request, exc):
    if request.url.path == "/tools/send-portive-overview":
        invalid_email = any(error["loc"] == ("body", "recipient_email") for error in exc.errors())
        return JSONResponse(status_code=422, content={"success": False,
                            "code": "invalid_recipient_email" if invalid_email else "invalid_overview_request"})
    return await request_validation_exception_handler(request, exc)


class CallContext(BaseModel):
    lead_id: str = Field(min_length=1, max_length=128)
    called_number: str = Field(min_length=8, max_length=30)
    conversation_id: str = Field(min_length=1, max_length=128)


class DncRequest(CallContext):
    reason: Literal["requested_no_contact", "no_more_calls", "other_opt_out"] = "requested_no_contact"


class CallbackRequest(CallContext):
    contact_name: str = Field(min_length=1, max_length=120)
    callback_at_local: str = Field(min_length=20, max_length=40, description="ISO 8601 local time with numeric UTC offset")
    timezone: str = Field(min_length=3, max_length=80, description="IANA timezone, e.g. America/Chicago")
    note: str = Field(default="Callback requested by prospect", max_length=700)


class OverviewRequest(CallContext):
    recipient_email: EmailStr
    recipient_name: str = Field(default="", max_length=120)


@app.get("/health")
def health():
    return {"status": "ok", "service": "portive-live-tools"}


def context_or_failure(body: CallContext):
    try:
        lead, phone = lead_for_call(body.lead_id, body.called_number)
    except ValueError:
        return None, None, {"success": False, "code": "invalid_phone", "message": "Called phone is invalid"}
    if lead is None:
        return None, phone, {"success": False, "code": "lead_phone_mismatch", "message": "Lead not found or called number does not match CRM lead"}
    return lead, phone, None


@app.post("/tools/add-to-dnc")
def add_to_dnc(body: DncRequest, x_portive_tool_key: str | None = Header(default=None)):
    require_tool_key(x_portive_tool_key)
    lead, phone, error = context_or_failure(body)
    if error:
        return error
    # Atomic DB function inserts a number-level suppression and cancels current lead's open callbacks.
    result = sb("POST", "rpc/portive_record_dnc", body={
        "p_lead_id": body.lead_id, "p_conversation_id": body.conversation_id,
        "p_phone_e164": phone, "p_reason": body.reason,
    })
    if not result or not result.get("suppressed"):
        return {"success": False, "code": "dnc_unconfirmed"}
    return {"success": True, "suppressed": True, "phone_e164": phone,
            "callbacks_canceled_for_lead": result.get("callbacks_canceled_for_lead", 0)}


@app.post("/tools/save-callback")
def save_callback(body: CallbackRequest, x_portive_tool_key: str | None = Header(default=None)):
    require_tool_key(x_portive_tool_key)
    lead, phone, error = context_or_failure(body)
    if error:
        return error
    try:
        utc = parse_callback(body.callback_at_local, body.timezone)
    except ValueError as exc:
        return {"success": False, "code": "invalid_callback_time", "message": str(exc)}
    if is_dnc(phone):
        return {"success": False, "code": "phone_suppressed", "message": "No callback permitted"}
    result = sb("POST", "rpc/portive_save_callback", body={
        "p_lead_id": body.lead_id,
        "p_phone_e164": phone,
        "p_conversation_id": body.conversation_id,
        "p_contact_name": body.contact_name,
        "p_callback_at": utc.isoformat(),
        "p_timezone": body.timezone,
        "p_note": body.note,
    })
    if result and result.get("saved"):
        return {"success": True, "saved": True, "callback_id": result.get("callback_id"),
                "callback_at_utc": result.get("callback_at_utc"),
                "timezone": body.timezone, "already_existed": result.get("already_existed", False)}
    return {"success": False, "code": (result or {}).get("code", "callback_not_saved")}


@app.post("/tools/send-portive-overview")
def send_portive_overview(body: OverviewRequest, x_portive_tool_key: str | None = Header(default=None)):
    require_tool_key(x_portive_tool_key)
    try:
        return _send_overview(body)
    except HTTPException:
        return {"success": False, "code": "email_database_unavailable",
                "message": "Database action unconfirmed; preserve request context and do not resend"}


def _send_overview(body: OverviewRequest):
    lead, phone, error = context_or_failure(body)
    if error:
        return error
    email = str(body.recipient_email).strip().lower()
    if is_dnc(phone) or is_email_suppressed(email):
        return {"success": False, "code": "contact_suppressed", "message": "Email must not be sent"}

    try:
        config = gmail_config()
        content = overview_email(recipient_name=body.recipient_name,
                                 business_name=lead.get("business_name") or "your spa",
                                 recipient_email=email)
    except GmailError as exc:
        return {"success": False, "code": exc.code}
    except HTTPException:
        return {"success": False, "code": "email_configuration_missing_or_invalid"}
    key = idempotency_key(body.lead_id, body.conversation_id, email)
    record = {
        "lead_id": body.lead_id, "conversation_id": body.conversation_id,
        "recipient_email": email, "recipient_name": body.recipient_name,
        "request_key": key, "status": "pending",
    }
    # Return existing accepted request instead of resending; concurrent calls cannot both insert.
    created = sb("POST", "portive_overview_emails", params={"on_conflict": "request_key"},
                 body=record, prefer="resolution=ignore-duplicates,return=representation")
    if not created:
        existing = sb("GET", "portive_overview_emails", params={"request_key": f"eq.{key}",
                                                              "select": "id,status,provider_email_id", "limit": "1"})
        if existing and existing[0]["status"] == "accepted" and existing[0].get("provider_email_id"):
            return {"success": True, "accepted_by_provider": True, "already_sent": True,
                    "provider_email_id": existing[0].get("provider_email_id"),
                    "provider": "gmail", "delivered_to_inbox": False}
        return {"success": False, "code": "email_already_attempted", "message": "Already pending/uncertain/failed; inspect Gmail Sent and audit record; do not resend"}
    record_id = created[0]["id"]
    try:
        provider_id = send_gmail(config=config, recipient=email, content=content)
    except GmailError as exc:
        try:
            sb("PATCH", "portive_overview_emails", params={"id": f"eq.{record_id}"},
               body={"status": "uncertain" if exc.uncertain else "failed", "last_error": exc.code})
        except HTTPException:
            # The pending reservation still blocks retries even if this update fails.
            log.warning("Overview failure audit update failed")
        result = {"success": False, "code": exc.code}
        if exc.uncertain:
            result["message"] = "Inspect Gmail Sent and audit record; do not resend or claim sent"
        return result

    try:
        updated = sb("PATCH", "portive_overview_emails", params={"id": f"eq.{record_id}"},
                     body={"status": "accepted", "provider_email_id": provider_id, "last_error": None},
                     prefer="return=representation")
        if not updated or updated[0].get("status") != "accepted" or updated[0].get("provider_email_id") != provider_id:
            raise HTTPException(502, "Email audit update unconfirmed")
    except HTTPException:
        log.warning("Gmail accepted overview; audit update unconfirmed")
        return {"success": False, "code": "email_accepted_audit_unconfirmed",
                "accepted_by_provider": True, "provider": "gmail", "provider_email_id": provider_id,
                "delivered_to_inbox": False,
                "message": "Reconcile audit with this Gmail ID; do not resend"}
    return {"success": True, "accepted_by_provider": True, "provider": "gmail",
            "provider_email_id": provider_id, "already_sent": False, "delivered_to_inbox": False}


@app.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_page(token: str = Query(min_length=40, max_length=512)):
    try:
        verify_unsubscribe_token(token)
    except ValueError:
        raise HTTPException(400, "Invalid link")
    safe_token = html.escape(token, quote=True)
    return HTMLResponse(f"""<!doctype html><html lang='en'><meta charset='UTF-8'>
<title>Unsubscribe | Portive</title><body style='font:16px sans-serif;max-width:550px;margin:60px auto'>
<h1>Unsubscribe from Portive emails</h1><p>Click below to stop Portive marketing emails to this address.</p>
<form action='/unsubscribe' method='post'><input type='hidden' name='token' value='{safe_token}'>
<button type='submit'>Confirm unsubscribe</button></form></body></html>""")


@app.post("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_execute(token: str = Form(...)):
    try:
        email = verify_unsubscribe_token(token)
    except ValueError:
        raise HTTPException(400, "Invalid link")
    sb("POST", "portive_email_suppressions", params={"on_conflict": "email"},
       body={"email": email, "reason": "unsubscribe_link"},
       prefer="resolution=ignore-duplicates,return=minimal")
    return HTMLResponse("<h1>Unsubscribed</h1><p>You will no longer receive Portive marketing emails at this address.</p>")
