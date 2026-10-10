# Portive — Integrated ElevenLabs Live Tools (ColdCallerCRM schema)

Three low-overhead FastAPI endpoints for Ava, connected to your **existing**
`dental-agent` Supabase project / ColdCallerCRM tables:

- `POST /tools/add-to-dnc` -> `portive_dnc`, cancel open callback records for this lead.
- `POST /tools/save-callback` -> EXISTING `follow_ups`, and `leads.derived_status='follow_up'`.
- `POST /tools/send-portive-overview` -> Gmail API + audit table + working unsubscribe web form.

**Not included:** automatic callback dialing, full ElevenLabs post-call webhook,
LangGraph workflow, verified legal eligibility checks, or automated deployment.
Those are separate planned modules. Do NOT confuse this with the med-spa
inbound booking/dental clinic scheduling system; these are outbound Portive
sales tools only.

## Why this matches your real schema

From your Supabase table screenshot + CRM main branch:
- `public.leads.id TEXT`, `phone_number TEXT`, `business_name TEXT`, `contact_name TEXT`,
  `derived_status TEXT`, `lead_list_id TEXT`; not UUID-typed IDs.
- `public.follow_ups.id TEXT`, `lead_id TEXT`, `due_at timestamptz`, `status`,
  `channel`, `note`, existing FK to leads.
- The CRM's current follow-up creation is implemented in
  `app/api/leads/[id]/followups/route.ts`, with login/cookie auth not suitable for
  an ElevenLabs service-to-service call.
- This package does not create a second callback table or modify old data.

## Installation

```bash
unzip portive_fastapi_crm_integrated.zip
cd portive_fastapi_crm_integrated
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

**BEFORE TESTING:** In Supabase SQL Editor, review and run
`sql/001_portive_live_tools.sql` against the correct project. Back up database
first. Migration is additive, but it adds columns/indexes/functions to production.

### Environment

Set `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (server-only service role JWT or
new `sb_secret_` key), `PORTIVE_TOOL_API_KEY` (random 40-byte string), and email
settings in `.env`. For DNC and callbacks, Gmail credentials are not required.

Generate TWO different secrets:
```bash
python3 -c 'import secrets;print(secrets.token_urlsafe(40))'
```

Authorize your own Gmail account locally using [Gmail setup](docs/GMAIL_SETUP.md).
No purchased sender domain is needed. Configure
`PUBLIC_BASE_URL` to the eventual HTTPS address of THIS FastAPI server;
unsubscribe routes work on the same domain. Supply your real postal address.

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
curl -fsS http://127.0.0.1:8000/health
```

Server runs locally on port 8000. Expose with publicly reachable HTTPS at your
Hetzner host or persistent backend; do not expose `.env`, Supabase secret or
API tokens. Use a process supervisor/reverse proxy with HTTPS.

## Configure ElevenLabs

See `elevenlabs/TOOLS_SETUP.md`. Use the secret header `X-Portive-Tool-Key`.
Body data from ElevenLabs system vars must be declared Dynamic, not extracted by
LLM. A business logic failure returns `success=false` even with HTTP 200.

Sample request for DNC testing (replace IDs/phone with your controlled test lead):
```bash
curl -sS http://127.0.0.1:8000/tools/add-to-dnc \
  -H "X-Portive-Tool-Key: $PORTIVE_TOOL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"lead_id":"REAL_TEST_LEAD_ID","called_number":"+15125550123","conversation_id":"controlled-test-001","reason":"requested_no_contact"}'
```
**Do not run that example against a real lead unless they requested suppression.**
Run with a dedicated test lead; or inspect tests with fake backends first.
For this curl invocation, export the tool token into your shell first
(the FastAPI `.env` loader does not automatically export shell variables).

### Vercel deployment

Keep `index.py` and all existing API URLs. Set server environment credentials
from [Gmail setup](docs/GMAIL_SETUP.md) in Vercel Project Settings, then redeploy.
Local authorization dependencies and token files are not needed by Vercel.

### Container deployment (optional)

```bash
docker compose up -d --build
curl -fsS http://127.0.0.1:8000/health
```

The Compose port binds only to localhost. Use Caddy or another reverse proxy
with HTTPS (see `Caddyfile.example`) to expose the tool endpoint to ElevenLabs.
Set `PUBLIC_BASE_URL` to your final HTTPS host. Do NOT put the backend directly
on an unauthenticated public HTTP port.

### Optional idempotent CRM patch helper

```bash
python scripts/patch_coldcaller_dnc.py /path/to/ColdCallerCRM
python scripts/patch_coldcaller_dnc.py /path/to/ColdCallerCRM --apply
```

The helper operates only when the inspected source markers are present, stores
`call-flow.ts.portive-backup`, and never commits/pushes changes for you. Inspect
its diff and test first; it does not claim to cover every possible dial path.

### Tests

```bash
pytest -q
python3 -m compileall -q app scripts tests
```

## CRITICAL: enforce DNC BEFORE ALL dialing

- `crm_patch/HOW_TO_PATCH.md`: TypeScript guard for existing manual Telnyx calling.
- `scripts/screen_batch_csv.py`: prefilter a new ElevenLabs batch CSV before
  uploading, excluding non-new/mismatched/suppressed rows. Run this immediately
  before EVERY batch. A stale CSV can violate DNC even if a tool records DNC.
- Later automated Python dialer must independently check DNC and applicable
  consent/calling-hour requirements before submitting an outbound call.

Example:
```bash
python scripts/screen_batch_csv.py prospects.csv safe_batch.csv
```

## SQL verification

```sql
select phone_e164,lead_id,created_at from public.portive_dnc order by created_at desc;
select lead_id,due_at,status,contact_name,prospect_timezone,source
  from public.follow_ups where source='elevenlabs' order by created_at desc;
select lead_id,recipient_email,status,provider_email_id
  from public.portive_overview_emails order by created_at desc;
select * from public.portive_email_suppressions order by created_at desc;
```

## Correctness and latency notes

- HTTPX uses persistent connection pooling; Supabase SQL RPCs make DNC and
  callback write+status updates one database round-trip.
- A benchmark on your actual Hetzner/Vercel/Supabase regions is required.
- DNC is stored by *normalized called phone*, independent of lead ID. This
  prevents bypassing suppression through other lists once the dialer checks it.
- Callback creation is idempotent on `(lead_id,source_conversation_id)`.
- Email request is reserved in Supabase on `(lead_id,conversation_id,email)`.
  Gmail sends are never automatically retried. **Provider acceptance is NOT delivery.**
- If an email send times out, it becomes `uncertain`; do not blindly retry and
  risk a duplicate. Inspect Gmail Sent and the Supabase audit record before a manual retry.
- SQL functions are restricted to `service_role`, and all new tables have RLS.
- `GET /unsubscribe` only renders a confirmation page; clicking submit POSTs
  to suppress that email address. GET never silently unsubscribes on link scans.

## Production safety checklist

- [ ] Correct Supabase project backup + migration applied
- [ ] Service secret rotated if previously exposed
- [ ] HTTPS URL deployed; `.env` never committed
- [ ] DNC tool tested against **test number**, incl. duplicate call
- [ ] Callback scheduled for test lead and appears in CRM Follow-ups
- [ ] Wrong lead/phone refused; past and incorrect DST callback refused
- [ ] `assertNotSuppressed` blocks existing CRM Telnyx manual calls
- [ ] Every ElevenLabs batch uses the fresh screened CSV
- [ ] Gmail OAuth credentials, authorized sender, valid address/unsubscribe configured
- [ ] Email sends once; unsubscribe blocks future overview sends
- [ ] Compliant automated outbound calling consent/disclosure verified separately
