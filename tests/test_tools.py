import json
from datetime import datetime, timedelta, timezone
import pytest
import httpx
from fastapi.testclient import TestClient

import app.core as core
from app.main import app


@pytest.fixture
def service(monkeypatch):
    for key, val in {
        "SUPABASE_URL": "https://testproj.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "sb_secret_testonly",
        "PORTIVE_TOOL_API_KEY": "test-local-key",
        "RESEND_API_KEY": "re_test",
        "RESEND_FROM": "Portive <hello@portive.test>",
        "RESEND_REPLY_TO": "hello@portive.test",
        "BUSINESS_POSTAL_ADDRESS": "123 Test Street, Austin, Texas 78701",
        "PUBLIC_BASE_URL": "https://tools.portive.test",
        "UNSUBSCRIBE_SIGNING_SECRET": "test-signature-secret",
    }.items():
        monkeypatch.setenv(key,val)
    state={"dnc":set(), "callback":{}, "emails":{}, "provider_count":0,
           "calls":[],"emails_suppressed":set()}

    def handler(req: httpx.Request):
        state["calls"].append((req.method,req.url.path))
        p=req.url.path
        if p == "/rest/v1/leads" and req.method == "GET":
            leadid=req.url.params.get("id","").removeprefix("eq.")
            return httpx.Response(200,json=[{"id":leadid, "phone_number":"+15125550123",
                                              "business_name":"Test Med Spa", "contact_name":"Emily",
                                              "derived_status":"new"}] if leadid == "lead-1" else [])
        if p == "/rest/v1/portive_dnc" and req.method == "GET":
            phone=req.url.params.get("phone_e164","").removeprefix("eq.")
            return httpx.Response(200,json=[{"phone_e164":phone}] if phone in state["dnc"] else [])
        if p == "/rest/v1/portive_email_suppressions" and req.method == "GET":
            email=req.url.params.get("email","").removeprefix("eq.")
            return httpx.Response(200,json=[{"email":email}] if email in state["emails_suppressed"] else [])
        if p == "/rest/v1/portive_email_suppressions" and req.method == "POST":
            state["emails_suppressed"].add(json.loads(req.content)["email"])
            return httpx.Response(201)
        if p == "/rest/v1/rpc/portive_record_dnc":
            obj=json.loads(req.content)
            state["dnc"].add(obj["p_phone_e164"])
            return httpx.Response(200,json={"suppressed":True,"callbacks_canceled_for_lead":0})
        if p == "/rest/v1/rpc/portive_save_callback":
            obj=json.loads(req.content)
            if obj["p_phone_e164"] in state["dnc"]:
                return httpx.Response(200,json={"saved":False,"code":"phone_suppressed"})
            idx=(obj["p_lead_id"],obj["p_conversation_id"])
            existed=idx in state["callback"]
            if existed and state["callback"][idx]["p_callback_at"]!=obj["p_callback_at"]:
                return httpx.Response(200,json={"saved":False,"code":"callback_already_saved_different_time"})
            state["callback"][idx]=obj
            return httpx.Response(200,json={"saved":True,"callback_id":"test-callback-1",
                                            "callback_at_utc":obj["p_callback_at"],
                                            "already_existed":existed})
        if p == "/rest/v1/portive_overview_emails" and req.method == "POST":
            obj=json.loads(req.content)
            if obj["request_key"] in state["emails"]:
                return httpx.Response(201,json=[])
            row={**obj,"id":"email-log-1"}
            state["emails"][obj["request_key"]]=row
            return httpx.Response(201,json=[row])
        if p == "/rest/v1/portive_overview_emails" and req.method == "GET":
            key=req.url.params.get("request_key","").removeprefix("eq.")
            row=state["emails"].get(key)
            return httpx.Response(200,json=[row] if row else [])
        if p == "/rest/v1/portive_overview_emails" and req.method == "PATCH":
            obj=json.loads(req.content)
            for row in state["emails"].values():
                if row["id"]==req.url.params.get("id","").removeprefix("eq."):
                    row.update(obj)
            return httpx.Response(204)
        if p == "/emails" and req.url.host == "api.resend.com":
            state["provider_count"]+=1
            assert req.headers.get("Idempotency-Key","").startswith("portive-")
            return httpx.Response(200,json={"id":"resend-msg-1"})
        raise AssertionError(f"Unexpected backend request: {req.method} {req.url}")

    transport=httpx.MockTransport(handler)
    fake=httpx.Client(transport=transport)
    monkeypatch.setattr(core,"_client",fake)
    yield TestClient(app),state
    fake.close()


def body(**kwargs):
    return {**{"lead_id":"lead-1", "called_number":"+1 (512) 555-0123",
            "conversation_id":"conv-01"}, **kwargs}


def request(client,endpoint,b):
    return client.post(endpoint,headers={"X-Portive-Tool-Key":"test-local-key"},json=b)


def test_health_and_auth(service):
    c,s=service
    assert c.get("/health").json()["status"]=="ok"
    assert c.post("/tools/add-to-dnc",json=body()).status_code==401
    assert request(c,"/tools/add-to-dnc",body()).json()["success"]


def test_dnc_idempotent_and_phone_level(service):
    c,s=service
    one=request(c,"/tools/add-to-dnc",body())
    two=request(c,"/tools/add-to-dnc",body())
    assert one.status_code==200 and one.json()["suppressed"]
    assert two.json()["suppressed"]
    assert len(s["dnc"])==1
    assert s["dnc"]=={"+15125550123"}


def test_wrong_lead_or_phone_is_rejected(service):
    c,s=service
    r=request(c,"/tools/add-to-dnc",{"lead_id":"lead-1","called_number":"+13103318914", "conversation_id":"conv-22"})
    assert r.json()["code"]=="lead_phone_mismatch"
    assert not s["dnc"]


def test_callback_valid_and_idempotent(service):
    c,s=service
    dt=(datetime.now(timezone.utc)+timedelta(days=3)).astimezone(__import__('zoneinfo').ZoneInfo('America/Chicago'))
    payload=body(contact_name="Jessica",timezone="America/Chicago",
                 callback_at_local=dt.isoformat(),note="Owner agreed to receive a callback")
    one=request(c,"/tools/save-callback",payload).json()
    two=request(c,"/tools/save-callback",payload).json()
    assert one["saved"] and not one["already_existed"]
    assert two["saved"] and two["already_existed"]
    assert len(s["callback"])==1


def test_callback_invalid_dst_and_past(service):
    c,s=service
    known_bad=request(c,"/tools/save-callback",body(contact_name="Jessica",timezone="America/Chicago",
                  callback_at_local="2026-03-08T02:30:00-06:00"))
    assert known_bad.json()["code"]=="invalid_callback_time"
    past=request(c,"/tools/save-callback",body(contact_name="Jessica",timezone="America/Chicago",
                  callback_at_local="2020-01-01T14:00:00-06:00"))
    assert past.json()["code"]=="invalid_callback_time"


def test_email_once_and_unsubscribe(service):
    c,s=service
    email=body(recipient_email="emily@example.org",recipient_name="Emily")
    first=request(c,"/tools/send-portive-overview",email).json()
    second=request(c,"/tools/send-portive-overview",email).json()
    assert first["success"] and first["delivered_to_inbox"] is False
    assert second["success"] and second["already_sent"]
    assert s["provider_count"]==1
    token=core.unsubscribe_token("emily@example.org")
    assert c.get("/unsubscribe",params={"token":token}).status_code==200
    assert "Unsubscribed" in c.post("/unsubscribe",data={"token":token}).text
    assert "emily@example.org" in s["emails_suppressed"]
    next_call=request(c,"/tools/send-portive-overview",body(
        recipient_email="emily@example.org",recipient_name="Emily", conversation_id="conv-02"))
    assert next_call.json()["code"]=="contact_suppressed"
    assert s["provider_count"]==1


def test_phone_normalization_and_token_tamper(service):
    c,s=service
    assert core.normalize_phone("512 555 0123")=="+15125550123"
    assert core.normalize_phone("+92 320 503 1232")=="+923205031232"
    with pytest.raises(ValueError):core.normalize_phone("123")
    token=core.unsubscribe_token("emily@example.org")
    with pytest.raises(ValueError):core.verify_unsubscribe_token(token[:-1]+"f")
