"""Authenticated, additive ElevenLabs sales-webhook endpoints.

These endpoints intentionally do not invoke a dialer, Gmail, calendar, callback
creation, or the existing demo-agent code.  A webhook is an audit/input channel,
not proof that an LLM-requested CRM action succeeded.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hmac
import json
import logging
import os
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .core import env, is_dnc, normalize_phone, require_tool_key, sb

router = APIRouter()
log = logging.getLogger("portive.webhooks")
MAX_INITIATION_BYTES = 32_768
MAX_POSTCALL_BYTES = 1_500_000
INITIATION_TIMEOUT = 1.75
_GENERIC_CONTACT = {"medical spa", "med spa", "spa", "owner", "manager", "unknown", "n/a"}


def _enabled() -> bool:
    return os.getenv("PORTIVE_WEBHOOK_ENABLED", "true").lower() in {"1", "true", "yes"}


def _masked(phone: str | None) -> str:
    if not phone:
        return "unavailable"
    digits = re.sub(r"\D", "", phone)
    return "***" + digits[-4:] if digits else "invalid"


def _agents() -> tuple[str, str]:
    inbound = env("PORTIVE_INBOUND_AGENT_ID")
    outbound = env("PORTIVE_OUTBOUND_AGENT_ID")
    return inbound, outbound


def _sales_number() -> str:
    return normalize_phone(env("PORTIVE_SALES_NUMBER"))


def _optional_phone(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or value.lower().strip() in {"private", "anonymous", "withheld", "unknown"}:
        return None
    try:
        return normalize_phone(value)
    except ValueError:
        return None


def _short_text(value: Any, limit: int = 480) -> str:
    """Remove controls/prompt-like content from text returned to an agent."""
    if not isinstance(value, str):
        return ""
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    value = re.sub(r"(?i)(ignore (all |previous )?instructions|system prompt|developer message|jailbreak)", "", value)
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _safe_owner(value: Any) -> str:
    name = _short_text(value, 80)
    if not name or name.lower() in _GENERIC_CONTACT or not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,78}", name):
        return ""
    return name


def _safe_timezone(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 80:
        return ""
    try:
        ZoneInfo(value)
        return value
    except ZoneInfoNotFoundError:
        return ""


def _base_variables(phone: str | None = None, *, status: str = "unknown", error: str = "") -> dict[str, str]:
    result = {"business_name": "", "owner_name": "", "city": "", "timezone": "", "lead_id": "",
              "prospect_phone_number": phone or "", "previous_context": "", "caller_match_status": status,
              "prior_call_outcome": ""}
    if error:
        result["context_error"] = error
    return result


def _lookup_rows(phone: str) -> list[dict]:
    # SQL function normalizes/indexes the legacy text phone field server-side.
    return sb("POST", "rpc/portive_find_leads_by_phone", body={"p_phone_e164": phone}, timeout=INITIATION_TIMEOUT) or []


def _prior_context(lead_id: str) -> str:
    rows = sb("GET", "portive_call_events", params={"lead_id": f"eq.{lead_id}",
              "select": "direction,call_outcome,call_status,event_timestamp,received_at", "order": "received_at.desc", "limit": "3"}, timeout=INITIATION_TIMEOUT) or []
    if not rows:
        return ""
    latest = rows[0]
    outcome = _short_text(latest.get("call_outcome"), 48).replace("_", " ")
    direction = latest.get("direction") if latest.get("direction") in {"inbound", "outbound"} else "prior"
    if outcome in {"voicemail", "no answer", "busy", "technical failure"}:
        return f"Previous {direction} attempt: {outcome}; no human conversation confirmed."
    if outcome in {"callback saved", "overview sent", "demo booked", "interested", "not interested", "wrong number"}:
        return f"Previous verified outcome: {outcome}."
    return "Previous verified call record exists; confirm details with the caller."


def _save_inbound_context(conversation_id: str, caller: str | None, agent_id: str, called: str) -> None:
    if not conversation_id or not caller:
        return
    sb("POST", "portive_inbound_contexts", params={"on_conflict": "conversation_id"}, body={
        "conversation_id": conversation_id, "caller_e164": caller, "agent_id": agent_id, "called_e164": called,
    }, prefer="resolution=ignore-duplicates,return=minimal", timeout=INITIATION_TIMEOUT)


def _initiation_response(payload: dict[str, Any]) -> dict[str, Any]:
    caller = _optional_phone(payload.get("caller_id"))
    conversation_id = str(payload.get("conversation_id") or "")[:128]
    if not caller:
        return {"type": "conversation_initiation_client_data", "dynamic_variables": _base_variables(status="unavailable")}
    try:
        rows, suppressed = _parallel_lookup(caller)
    except HTTPException:
        log.warning("Initiation datastore unavailable conversation=%s caller=%s", conversation_id, _masked(caller))
        return {"type": "conversation_initiation_client_data", "dynamic_variables": _base_variables(caller, status="lookup_error", error="temporary_lookup_unavailable")}
    if suppressed:
        variables = _base_variables(caller, status="suppressed")
        variables["previous_context"] = "This caller is on the no-contact list. Do not solicit or schedule marketing follow-up."
        return {"type": "conversation_initiation_client_data", "dynamic_variables": variables}
    if len(rows) != 1:
        return {"type": "conversation_initiation_client_data", "dynamic_variables": _base_variables(caller, status="ambiguous" if len(rows) > 1 else "unknown")}
    lead = rows[0]
    variables = _base_variables(caller, status="matched")
    variables.update({"business_name": _short_text(lead.get("business_name"), 120),
                      "owner_name": _safe_owner(lead.get("contact_name")),
                      "city": _short_text(lead.get("city"), 80),
                      "timezone": _safe_timezone(lead.get("timezone")),
                      "lead_id": str(lead.get("id") or "")[:128]})
    variables["previous_context"] = _prior_context(variables["lead_id"])
    if variables["previous_context"]:
        variables["prior_call_outcome"] = "verified_history"
    return {"type": "conversation_initiation_client_data", "dynamic_variables": variables}


def _parallel_lookup(phone: str) -> tuple[list[dict], bool]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(_lookup_rows, phone)
        b = pool.submit(is_dnc, phone)
        return a.result(), b.result()


@router.post("/webhooks/elevenlabs/initiation")
async def elevenlabs_initiation(request: Request, x_portive_initiation_key: str | None = Header(default=None)):
    secret = env("PORTIVE_INITIATION_SECRET")
    if not x_portive_initiation_key or not hmac.compare_digest(x_portive_initiation_key, secret):
        raise HTTPException(401, "Unauthorized initiation webhook")
    if not _enabled():
        raise HTTPException(503, "Webhook intake disabled")
    if request.headers.get("content-length") and int(request.headers["content-length"]) > MAX_INITIATION_BYTES:
        raise HTTPException(413, "Payload too large")
    raw = await request.body()
    if len(raw) > MAX_INITIATION_BYTES:
        raise HTTPException(413, "Payload too large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(422, "Expected JSON object")
    agent_id = payload.get("agent_id")
    called = _optional_phone(payload.get("called_number"))
    inbound, _ = _agents()
    if not isinstance(agent_id, str) or not agent_id or agent_id != inbound:
        raise HTTPException(403, "Unauthorized agent")
    if called != _sales_number():
        raise HTTPException(403, "Unauthorized destination")
    # Persist a minimal signed-context binding before a later unknown-caller DNC
    # tool can use it.  A DB failure still allows a safe, no-identity conversation.
    caller = _optional_phone(payload.get("caller_id"))
    conversation_id = payload.get("conversation_id")
    if isinstance(conversation_id, str) and caller:
        try:
            await run_in_threadpool(_save_inbound_context, conversation_id[:128], caller, agent_id, called)
        except HTTPException:
            log.warning("Initiation context audit unavailable conversation=%s", conversation_id[:128])
    response = await run_in_threadpool(_initiation_response, payload)
    log.info("Initiation conversation=%s agent=%s caller=%s status=%s", str(conversation_id or "")[:128], agent_id, _masked(caller), response["dynamic_variables"]["caller_match_status"])
    return response


def _construct_event(raw: bytes, signature: str | None) -> dict[str, Any]:
    if not signature:
        raise ValueError("signature missing")
    secret = env("ELEVENLABS_POSTCALL_WEBHOOK_SECRET")
    try:
        from elevenlabs.client import ElevenLabs
        event = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY") or None).webhooks.construct_event(
            rawBody=raw.decode("utf-8"), sig_header=signature, secret=secret)
    except Exception as exc:  # SDK raises its own BadRequestError across versions.
        raise ValueError("signature invalid") from exc
    if not isinstance(event, dict):
        raise ValueError("event malformed")
    return event


def _timestamp(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _normalized_dynamic(data: dict) -> tuple[str | None, str | None]:
    dynamic = ((data.get("conversation_initiation_client_data") or {}).get("dynamic_variables") or {})
    if not isinstance(dynamic, dict):
        return None, None
    lead_id = dynamic.get("lead_id") if isinstance(dynamic.get("lead_id"), str) else None
    return (lead_id[:128] if lead_id else None), _optional_phone(dynamic.get("prospect_phone_number"))


def _context_for_conversation(conversation_id: str) -> dict | None:
    rows = sb("GET", "portive_inbound_contexts", params={"conversation_id": f"eq.{conversation_id}", "limit": "1"}, timeout=6.0) or []
    return rows[0] if rows else None


def _verified_lead(lead_id: str | None, phone: str | None) -> str | None:
    if not lead_id or not phone:
        return None
    rows = sb("GET", "leads", params={"id": f"eq.{lead_id}", "select": "id,phone_number", "limit": "1"}, timeout=6.0) or []
    if rows and _optional_phone(rows[0].get("phone_number")) == phone:
        return lead_id
    return None


def _failure_phone(data: dict, direction: str) -> tuple[str | None, str | None, dict]:
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    body = metadata.get("body") if isinstance(metadata.get("body"), dict) else {}
    source = body.get("from_number", body.get("From", body.get("Caller")))
    target = body.get("to_number", body.get("To", body.get("Called")))
    source_phone, target_phone = _optional_phone(source), _optional_phone(target)
    sales = _sales_number()
    if direction == "outbound":
        return target_phone if source_phone == sales or target_phone != sales else None, sales if source_phone == sales else source_phone, body
    return source_phone if target_phone == sales else None, sales if target_phone == sales else target_phone, body


def _outcome(data: dict, event_type: str, failure_reason: str | None) -> str:
    if event_type == "call_initiation_failure":
        return {"busy": "busy", "no-answer": "no_answer"}.get((failure_reason or "").lower(), "technical_failure")
    analysis = data.get("analysis") if isinstance(data.get("analysis"), dict) else {}
    collected = analysis.get("data_collection_results") if isinstance(analysis.get("data_collection_results"), dict) else {}
    raw = str(collected.get("call_outcome", "")).lower().replace("-", "_").replace(" ", "_")
    # Booking, callback, overview, and DNC are persistence claims, not a model
    # analysis claim.  They are promoted separately only after DB verification.
    allowed = {"interested", "not_interested", "voicemail", "no_answer", "busy", "ivr_only", "wrong_number", "technical_failure"}
    return raw if raw in allowed else "unknown"


def _verified_outcome(lead_id: str | None, phone: str | None, conversation_id: str, candidate: str) -> str:
    """Apply durable CRM facts before an analysis-derived disposition."""
    if phone and is_dnc(phone):
        return "dnc"
    if not lead_id:
        return candidate
    callbacks = sb("GET", "follow_ups", params={"lead_id": f"eq.{lead_id}",
        "source_conversation_id": f"eq.{conversation_id}", "select": "id", "limit": "1"}, timeout=6.0) or []
    if callbacks:
        return "callback_saved"
    emails = sb("GET", "portive_overview_emails", params={"lead_id": f"eq.{lead_id}",
        "conversation_id": f"eq.{conversation_id}", "status": "eq.accepted", "select": "id", "limit": "1"}, timeout=6.0) or []
    if emails:
        return "overview_sent"
    # Calendar proof is intentionally not guessed: no calendar-event audit exists
    # in this service, so an analysis saying "demo booked" remains unknown.
    return candidate


def _event_row(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    event_type, conversation_id, agent_id = event.get("type"), data.get("conversation_id"), data.get("agent_id")
    if not all(isinstance(x, str) and x for x in (event_type, conversation_id, agent_id)):
        raise ValueError("missing event identity")
    inbound, outbound = _agents()
    direction = "inbound" if agent_id == inbound else "outbound" if agent_id == outbound else None
    if not direction:
        raise PermissionError("non-portive agent")
    lead_id, dynamic_phone = _normalized_dynamic(data)
    context = _context_for_conversation(conversation_id) if direction == "inbound" else None
    if context:
        remote, portive = _optional_phone(context.get("caller_e164")), _optional_phone(context.get("called_e164"))
    elif event_type == "call_initiation_failure":
        remote, portive, _ = _failure_phone(data, direction)
    else:
        remote, portive = dynamic_phone, _sales_number() if dynamic_phone else None
    verified_id = _verified_lead(lead_id, remote) if remote else None
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    analysis = data.get("analysis") if isinstance(data.get("analysis"), dict) else {}
    transcript = data.get("transcript") if isinstance(data.get("transcript"), list) else None
    failure = _short_text(data.get("failure_reason"), 100) or None
    failure_body = metadata.get("body") if isinstance(metadata.get("body"), dict) else {}
    sip_code = failure_body.get("sip_status_code")
    try: sip_code = int(sip_code) if sip_code is not None else None
    except (TypeError, ValueError): sip_code = None
    raw_payload = {"type": event_type, "event_timestamp": event.get("event_timestamp"), "data": data}
    if event_type == "post_call_audio": raw_payload["data"] = {"conversation_id": conversation_id, "agent_id": agent_id}
    return {"conversation_id": conversation_id[:128], "event_type": event_type[:80], "agent_id": agent_id[:128],
        "agent_name": _short_text(data.get("agent_name"), 200) or None, "branch_id": _short_text(data.get("branch_id"), 128) or None,
        "version_id": _short_text(data.get("version_id"), 128) or None, "lead_id": verified_id, "direction": direction,
        "remote_phone_e164": remote, "portive_phone_e164": portive, "call_status": _short_text(data.get("status"), 80) or None,
        "call_outcome": _verified_outcome(verified_id, remote, conversation_id,
            _outcome(data, event_type, failure)), "match_status": "matched" if verified_id else ("unknown" if remote else "unavailable"),
        "failure_reason": failure, "sip_status_code": sip_code, "sip_error_reason": _short_text(failure_body.get("error_reason"), 500) or None,
        "sip_call_id": _short_text(failure_body.get("call_sid", failure_body.get("CallSid")), 160) or None,
        "duration_seconds": metadata.get("call_duration_secs") if isinstance(metadata.get("call_duration_secs"), int) else None,
        "elevenlabs_credits": metadata.get("cost") if isinstance(metadata.get("cost"), int) else None,
        "transcript_summary": _short_text(analysis.get("transcript_summary"), 4000) or None, "transcript": transcript,
        "analysis": analysis or None, "metadata": metadata or None, "raw_payload": raw_payload,
        "event_timestamp": _timestamp(event.get("event_timestamp"))}


def _persist_event(row: dict) -> None:
    # Ignore duplicates without replacing a good row with sparse retry data.
    result = sb("POST", "portive_call_events", params={"on_conflict": "conversation_id,event_type"}, body=row,
                prefer="resolution=ignore-duplicates,return=representation", timeout=8.0)
    # 201 [] is the expected duplicate response under Prefer ignore-duplicates.
    if result is None:
        raise HTTPException(502, "Event persistence unconfirmed")


@router.post("/webhooks/elevenlabs/post-call")
async def elevenlabs_post_call(request: Request):
    if request.headers.get("content-length") and int(request.headers["content-length"]) > MAX_POSTCALL_BYTES:
        raise HTTPException(413, "Payload too large")
    raw = await request.body()
    if len(raw) > MAX_POSTCALL_BYTES:
        raise HTTPException(413, "Payload too large")
    try:
        event = _construct_event(raw, request.headers.get("elevenlabs-signature"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(401, "Invalid webhook signature")
    if not _enabled():
        raise HTTPException(503, "Webhook intake disabled")
    if event.get("type") == "post_call_audio":
        return {"status": "ignored_audio"}
    if event.get("type") not in {"post_call_transcription", "call_initiation_failure"}:
        return {"status": "ignored_event"}
    try:
        row = await run_in_threadpool(_event_row, event)
    except PermissionError:
        return {"status": "ignored_agent"}
    except (ValueError, HTTPException) as exc:
        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(422, "Malformed event") from exc
    await run_in_threadpool(_persist_event, row)
    log.info("Post-call persisted conversation=%s type=%s agent=%s remote=%s", row["conversation_id"], row["event_type"], row["agent_id"], _masked(row.get("remote_phone_e164")))
    return {"status": "received"}


class UnknownInboundDncRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(default="requested_no_contact", max_length=80)


@router.post("/tools/add-to-dnc-inbound-unknown")
async def add_unknown_inbound_dnc(body: UnknownInboundDncRequest, x_portive_tool_key: str | None = Header(default=None)):
    """Suppress only the ANI previously captured by authenticated initiation."""
    require_tool_key(x_portive_tool_key)
    inbound, _ = _agents()
    try:
        result = await run_in_threadpool(sb, "POST", "rpc/portive_record_unknown_inbound_dnc", body={
            "p_conversation_id": body.conversation_id, "p_agent_id": inbound, "p_reason": body.reason,
        })
    except HTTPException:
        # Persist a no-phone review marker when possible; never claim suppression.
        try:
            await run_in_threadpool(sb, "POST", "portive_dnc_quarantine", body={"conversation_id": body.conversation_id, "reason": "unverified_inbound_opt_out"}, prefer="return=minimal")
        except HTTPException:
            pass
        return {"success": False, "code": "manual_review_required"}
    if result and result.get("suppressed"):
        return {"success": True, "suppressed": True}
    return {"success": False, "code": "manual_review_required"}
