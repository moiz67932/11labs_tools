"""Synthetic webhook tests: no network, real leads, emails, or calls."""
import json
import pytest
from fastapi.testclient import TestClient

import app.webhooks as hooks
from app.main import app


@pytest.fixture
def service(monkeypatch):
    for name, value in {
        "PORTIVE_WEBHOOK_ENABLED": "true", "PORTIVE_INITIATION_SECRET": "init-secret",
        "ELEVENLABS_POSTCALL_WEBHOOK_SECRET": "post-secret", "PORTIVE_TOOL_API_KEY": "tool-secret",
        "PORTIVE_INBOUND_AGENT_ID": "agent_in", "PORTIVE_OUTBOUND_AGENT_ID": "agent_out",
        "PORTIVE_SALES_NUMBER": "+13103318914", "SUPABASE_URL": "https://test.invalid",
        "SUPABASE_SERVICE_ROLE_KEY": "sb_secret_test",
    }.items(): monkeypatch.setenv(name, value)
    state = {"leads": [{"id": "lead-1", "phone_number": "+15125550123", "business_name": "Test Spa",
                         "contact_name": "Jane Doe", "city": "Austin", "timezone": "America/Chicago"}],
             "dnc": set(), "contexts": {}, "events": {}, "fail": False, "quarantine": []}

    def fake_sb(method, endpoint, *, params=None, body=None, **kwargs):
        if state["fail"]: raise hooks.HTTPException(502, "test outage")
        if endpoint == "rpc/portive_find_leads_by_phone":
            return [{k: row.get(k) for k in ("id", "business_name", "contact_name", "city", "timezone")}
                    for row in state["leads"] if hooks._optional_phone(row["phone_number"]) == body["p_phone_e164"]]
        if endpoint == "portive_dnc" and method == "GET":
            return [{}] if params["phone_e164"].removeprefix("eq.") in state["dnc"] else []
        if endpoint == "portive_inbound_contexts" and method == "POST":
            state["contexts"].setdefault(body["conversation_id"], body); return []
        if endpoint == "portive_inbound_contexts" and method == "GET":
            found = state["contexts"].get(params["conversation_id"].removeprefix("eq.")); return [found] if found else []
        if endpoint == "portive_call_events" and method == "GET": return []
        if endpoint == "leads" and method == "GET":
            ident = params["id"].removeprefix("eq."); return [x for x in state["leads"] if x["id"] == ident]
        if endpoint in {"follow_ups", "portive_overview_emails"} and method == "GET": return []
        if endpoint == "portive_call_events" and method == "POST":
            key = (body["conversation_id"], body["event_type"]); state["events"].setdefault(key, body); return [body] if key in state["events"] else []
        if endpoint == "rpc/portive_record_unknown_inbound_dnc":
            context = state["contexts"].get(body["p_conversation_id"])
            if not context or context["agent_id"] != body["p_agent_id"]: return {"suppressed": False}
            state["dnc"].add(context["caller_e164"]); return {"suppressed": True}
        if endpoint == "portive_dnc_quarantine": state["quarantine"].append(body); return []
        raise AssertionError((method, endpoint, params, body))
    monkeypatch.setattr(hooks, "sb", fake_sb)
    monkeypatch.setattr(hooks, "is_dnc", lambda phone: phone in state["dnc"])
    yield TestClient(app), state


def initiation(c, **overrides):
    body = {"caller_id": "(512) 555-0123", "called_number": "+13103318914", "agent_id": "agent_in", "conversation_id": "conv-in"}
    body.update(overrides)
    return c.post("/webhooks/elevenlabs/initiation", headers={"X-Portive-Initiation-Key": "init-secret"}, json=body)


def test_initiation_known_unknown_ambiguous_and_security(service):
    c, state = service
    response = initiation(c)
    assert response.status_code == 200
    vars = response.json()["dynamic_variables"]
    assert vars["lead_id"] == "lead-1" and vars["timezone"] == "America/Chicago"
    assert "first_message" not in response.text
    assert initiation(c, caller_id="private", conversation_id="conv-private").json()["dynamic_variables"]["lead_id"] == ""
    state["leads"].append({**state["leads"][0], "id": "lead-2"})
    assert initiation(c, conversation_id="conv-duplicate").json()["dynamic_variables"]["caller_match_status"] == "ambiguous"
    assert initiation(c, agent_id="agent_demo").status_code == 403
    assert initiation(c, called_number="+14155550100").status_code == 403
    assert c.post("/webhooks/elevenlabs/initiation", json={}).status_code == 401


def test_initiation_dnc_and_lookup_failure_are_safe(service):
    c, state = service
    state["dnc"].add("+15125550123")
    vars = initiation(c).json()["dynamic_variables"]
    assert vars["caller_match_status"] == "suppressed" and not vars["lead_id"]
    state["dnc"].clear(); state["fail"] = True
    vars = initiation(c).json()["dynamic_variables"]
    assert vars["caller_match_status"] == "lookup_error" and not vars["lead_id"]


def event(kind="post_call_transcription", conversation="conv-post", agent="agent_out"):
    return {"type": kind, "event_timestamp": 1739537297, "data": {"agent_id": agent, "conversation_id": conversation,
      "agent_name": "Ava", "status": "done", "metadata": {"call_duration_secs": 22, "cost": 296},
      "transcript": [{"role": "user", "message": "synthetic", "time_in_call_secs": 1}],
      "analysis": {"transcript_summary": "Synthetic summary", "data_collection_results": {"call_outcome": "interested"}},
      "conversation_initiation_client_data": {"dynamic_variables": {"lead_id": "lead-1", "prospect_phone_number": "+15125550123"}}}}


def test_postcall_signature_persistence_idempotency_and_failures(service, monkeypatch):
    c, state = service
    monkeypatch.setattr(hooks, "_construct_event", lambda raw, sig: event())
    headers = {"ElevenLabs-Signature": "signed"}
    assert c.post("/webhooks/elevenlabs/post-call", headers=headers, content=b"{}").status_code == 200
    assert len(state["events"]) == 1
    assert c.post("/webhooks/elevenlabs/post-call", headers=headers, content=b"{}").status_code == 200
    assert len(state["events"]) == 1
    monkeypatch.setattr(hooks, "_construct_event", lambda raw, sig: event("call_initiation_failure", "conv-failure"))
    assert c.post("/webhooks/elevenlabs/post-call", headers=headers, content=b"{}").status_code == 200
    assert len(state["events"]) == 2
    monkeypatch.setattr(hooks, "_construct_event", lambda raw, sig: (_ for _ in ()).throw(ValueError()))
    assert c.post("/webhooks/elevenlabs/post-call", headers=headers, content=b"{}").status_code == 401
    assert len(state["events"]) == 2


def test_postcall_demo_audio_and_durable_failure(service, monkeypatch):
    c, state = service
    monkeypatch.setattr(hooks, "_construct_event", lambda raw, sig: event(agent="demo-agent"))
    assert c.post("/webhooks/elevenlabs/post-call", headers={"ElevenLabs-Signature": "s"}, content=b"{}").json()["status"] == "ignored_agent"
    monkeypatch.setattr(hooks, "_construct_event", lambda raw, sig: event("post_call_audio"))
    assert c.post("/webhooks/elevenlabs/post-call", headers={"ElevenLabs-Signature": "s"}, content=b"{}").json()["status"] == "ignored_audio"
    state["fail"] = True
    monkeypatch.setattr(hooks, "_construct_event", lambda raw, sig: event())
    assert c.post("/webhooks/elevenlabs/post-call", headers={"ElevenLabs-Signature": "s"}, content=b"{}").status_code == 502


def test_unknown_inbound_dnc_is_context_bound(service):
    c, state = service
    initiation(c, conversation_id="safe-context")
    result = c.post("/tools/add-to-dnc-inbound-unknown", headers={"X-Portive-Tool-Key": "tool-secret"}, json={"conversation_id": "safe-context"})
    assert result.json() == {"success": True, "suppressed": True}
    forged = c.post("/tools/add-to-dnc-inbound-unknown", headers={"X-Portive-Tool-Key": "tool-secret"}, json={"conversation_id": "forged"})
    assert forged.json()["success"] is False
