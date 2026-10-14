# CODEX MASTER TASK — PORTIVE ELEVENLABS WEBHOOKS & CRM OUTCOME PIPELINE

You are the senior backend engineer responsible for implementing **all of Part 2: Webhooks and CRM call-history integration** for Portive. **Implement working code, SQL migrations, tests, configuration documentation, and a deploy/runbook. Do not stop at an architectural proposal.** Work surgically in the current repository; do not push/deploy or run destructive SQL without approval. At the end give exact commands and dashboard actions I must perform.

## A. Product and non-negotiable scope

Portive is my sales/AI-receptionist agency for US med spas. Two separate ElevenLabs sales agents exist:

1. **Ava | Portive Outbound**: calls permitted leads via ElevenLabs Batch Calling over Telnyx SIP; uses CSV dynamic variables and does five-stage sales discovery.
2. **Ava | Portive Inbound Agent**: answers callbacks to my existing Telnyx-owned Portive sales number `+13103318914` with a configured First Message of `Hello.`; identifies returning clinics, continues natural missed-call/callback-recovery discovery and may book demos.

There is ALSO a completely distinct med-spa **demo receptionist agent**, which MUST NOT be confused with these sales agents or ingest into Portive sales call history. The `ColdCallerCRM` codebase already contains some ElevenLabs **demo** initiation/post-call handlers: preserve them; do not repurpose their business logic. Implement the NEW Portive sales webhook endpoints in the existing Python FastAPI application hosted at `https://11labs-tools.vercel.app`.

Existing LIVE actions already tested successfully: `POST /tools/add-to-dnc`, `/tools/save-callback`, `/tools/send-portive-overview`; also calendar availability and creation tools inside ElevenLabs. Preserve these exact routes, payloads, authentication, Gmail OAuth, unsubscribe behavior, SQL functions and outward-facing semantics. **Do not refactor, migrate, delete, or break them.**

Deliver now:
- Secure ElevenLabs **conversation-initiation webhook** for inbound caller recognition.
- Secure ElevenLabs **post-call transcription** and **call-initiation-failure** webhook intake for both Portive sales agents.
- Additive Supabase event/history schema, indexed matching and idempotent processing.
- Deterministic disposition and safe CRM outcome handling, including DNC supremacy, validated tool records, and safe unknown-caller opt-outs.
- Reliable tests, docs, Vercel deploy checklist, ElevenLabs UI configuration instructions, and validation SQL.

**Out of scope for this task:** automatic outbound prospect dialing, callback executor/cron, mass campaign scheduler, bulk lead import, website scraping, LangGraph, new LLM agents, telephony trunk rewiring, UI redesign and med-spa demo-agent changes. Build data structures extensible for these later. NEVER actually call a lead or send unrequested emails in tests. Respect AI-voice marketing consent, number eligibility, local hours, privacy, recording notices, and DNC; webhooks are not legal-consent verification.

## B. Repo reconnaissance BEFORE coding

1. Inspect git status/branch and all relevant files in the local `11labs_tools` repo, including `index.py`, `app/main.py`, `app/core.py`, `app/gmail.py`, `requirements.txt`, `sql/001_portive_live_tools.sql`, `.env.example`, `tests`, `README.md` and deployment config.
2. `index.py` currently imports `app` from `app.main`. Keep that Vercel entry point. `app/core.py` already implements `sb(method, endpoint, ...)`, `normalize_phone(...)`, `lead_for_call(...)`, `is_dnc(...)`, and uses `httpx`. Reuse these carefully; a pre-call webhook should use a separately bounded fast timeout rather than blocking for the default six seconds unnecessarily.
3. Read and understand existing production DNC and callback SQL behavior. The old checked-in initial SQL migration has had **signature/GRANT problems** in earlier deployments; production functions were separately repaired. **DO NOT blindly rerun or overwrite that old migration.** Create a NEW additive migration with a new filename. Verify signatures against actual production schema before touching functions.
4. If available, inspect linked `moiz67932/ColdCallerCRM` only to understand schema/current CRM behavior. It contains pre-existing ElevenLabs **demo-specific** hooks (`lib/elevenlabs/conversation-initiation.ts` and `lib/elevenlabs/post-call-webhook.ts`). Do not alter/duplicate their purposes. Work within `11labs_tools` for Portive sales webhooks.
5. Before implementing, read official current ElevenLabs documentation for initiation webhook, post-call webhook/HMAC signature, dynamic variables, SIP metadata and SDK. Do not use invented request field paths or methods; support documented shapes. Verify installed SDK method signatures and tests.
6. Report any discovered schema divergences before running migrations, but continue with safe code and tests; do not block merely because optional secrets or real API credentials are unavailable locally.

Official docs to verify:
- https://elevenlabs.io/docs/eleven-agents/customization/personalization
- https://elevenlabs.io/docs/eleven-agents/customization/personalization/dynamic-variables
- https://elevenlabs.io/docs/eleven-agents/workflows/post-call-webhooks
- https://elevenlabs.io/docs/eleven-api/resources/webhooks
- https://elevenlabs.io/docs/eleven-agents/phone-numbers/sip-trunking

## C. AUTHORITATIVE lead table schema (supplied by owner)

```sql
create table public.leads (
  id text not null default (gen_random_uuid ())::text,
  lead_list_id text not null,
  business_name text null,
  contact_name text null,
  phone_number text not null,
  city text null,
  state text null,
  niche text null,
  website text null,
  notes text null,
  custom_fields_json jsonb null,
  derived_status text not null default 'new'::text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  location text null,
  constraint leads_pkey primary key (id),
  constraint leads_lead_list_id_phone_number_key unique (lead_list_id, phone_number),
  constraint leads_lead_list_id_fkey foreign key (lead_list_id)
    references public.lead_lists(id) on delete cascade
);
create index if not exists leads_lead_list_created_at_idx on public.leads (lead_list_id, created_at);
create index if not exists leads_derived_status_idx on public.leads (derived_status);
create index if not exists leads_location_created_at_idx on public.leads (location, created_at desc);
create trigger leads_touch_updated_at before update on public.leads
  for each row execute function touch_updated_at();
```

**Hard implications:**
- `leads.id`, `lead_list_id`, `follow_ups.lead_id` are **TEXT**, even if an ID resembles a UUID. Do not assume UUID-typed IDs.
- `lead_list_id` is required with a foreign key; **NEVER create a fabricated placeholder lead** for an unknown inbound caller.
- Uniqueness is per `(lead_list_id, phone_number)`, NOT global phone uniqueness. A phone number can belong to multiple lists or appear in differently formatted variants. Never arbitrarily pick `limit=1` across the entire database.
- `phone_number` is not guaranteed globally E.164-normalized. Use robust normalized matching and reject ambiguous multi-lead matches.
- `contact_name` is unverified; often a label like "Medical spa", not the actual owner name. `business_name`, `city`, etc. may be stale.
- There is **no `timezone` column**. Determine timezone only from a trusted existing record/source or a defensible uniquely identified city/state mapping; otherwise return an empty string and ask the caller. Do NOT default every prospect to `America/Chicago` or `Asia/Karachi`. `location` is an opaque text label, not a timezone.
- `derived_status` is arbitrary text used by ColdCallerCRM; the attached 100-row sample includes `new`, `contacted`, `closed_lost`, `bad_number`, `test`, and `demo_requested`. **Do not bulk overwrite derived_status with an invented `voicemail`/`booked`/`follow_up` vocabulary without inspecting CRM conventions.** Prefer dedicated Portive outcome tables; conservatively project status into `leads` only when tested and semantically correct.
- All call history must link by actual `leads.id` when verified, not by just a displayed business name.

An attached `Supabase Snippet Untitled query(1).csv` is a **sample export of 100 rows** with precisely these 15 headers: `id,lead_list_id,business_name,contact_name,phone_number,city,state,niche,website,notes,custom_fields_json,derived_status,created_at,updated_at,location`. Use it ONLY locally for schema/edge-case inspection, never dial its numbers, never ship it into commits/test fixtures, never print personal lead data or expose it publicly. Do not assume that sample covers the whole DB. The sample has 100 unique phone numbers, but full production can still have duplicates across lists.

Known tables from current backend/CRM: `public.leads`, `public.lead_lists`, existing `public.call_attempts`, `public.follow_ups`, `public.portive_dnc` (number-level suppression), `public.portive_overview_emails` (email audit), `public.portive_email_suppressions`, and existing demo/legacy voice tables. Validate columns/signatures before use; avoid interfering with demo-agent tables.

## D. Exact endpoint names and source layout

Create these TWO FastAPI endpoints in the SAME existing deployed app:

- `POST /webhooks/elevenlabs/initiation`
- `POST /webhooks/elevenlabs/post-call`

Preferred new code: `app/webhooks.py` (APIRouter), plus small clearly separated helpers such as `app/caller_lookup.py`, `app/postcall.py`, `app/webhook_security.py` only if justified. Register router via `app.include_router(...)` in `app/main.py`. The deployed base URL is `https://11labs-tools.vercel.app`.

Do not create another Vercel app, backend, project, Supabase database, or webhook secret system. Do not overwrite existing routes. Avoid circular imports, excessive dependencies and synchronous network calls inside unbounded async event loops.

## E. INBOUND conversation-initiation webhook — exact behavior

The ElevenLabs inbound webhook is invoked **before** the sales agent begins its inbound SIP conversation. Use the documented JSON POST body, ordinarily containing `caller_id`, `called_number`, `agent_id`, `conversation_id` (possibly additional documented fields). Do not confuse agent demo callbacks, outbound calls and inbound calls; enforce allowlist of **inbound Portive sales agent ID** and destination `PORTIVE_SALES_NUMBER` (`+13103318914`). Never trust an LLM-provided phone when the verified webhook provides the SIP caller ID.

### Authentication and request validation

- Configure a dedicated `X-Portive-Initiation-Key` secret header from ElevenLabs' initiation webhook settings; compare server-side to `PORTIVE_INITIATION_SECRET` in constant time. Fail closed with 401/403 when missing/incorrect. Separate this from existing `X-Portive-Tool-Key` and the HMAC post-call secret.
- Validate payload type/size, nonblank agent ID, called number, caller_id if provided, supported channel. Reject unauthorized agent ID or incorrect sales destination; never include sensitive CRM data in such responses.
- Do not treat a malformed phone as a verified match. If incoming caller ID is withheld/unavailable, use an unknown-caller safe context rather than erroring and dropping an otherwise valid call (unless security validation fails).
- Never log secrets, full phone numbers, entire POST bodies, patient data or extensive business notes. Logs may include masked phone, conversation_id, agent_id, match status and latency.

### Known caller lookup

- Normalize `caller_id` with a tested US E.164 normalizer, preserving support for current international conventions only when explicit.
- Search eligible `leads` by **normalized** phone. Use a real Postgres expression index or an additive normalized field/function/index backed by SQL if the existing exact equality is insufficient. Avoid an unindexed full-table scan and avoid per-call loading of all leads.
- Inspect `portive_dnc` for caller phone; return no solicitation context for suppressed numbers, respect the call if it's an incoming inquiry but do not pitch and never schedule further marketing follow-up.
- Handle duplicates. If zero matches → `unknown`; exactly one unambiguous match → `matched`; if two+ records disagree/are ambiguous → `ambiguous`, no selected lead ID. Duplicate rows across lists can be safely resolved only if explicit recorded authoritative campaign/conversation binding proves the one intended lead; do not take most recent by default.
- For known match, optionally load last 1–3 **verified** interactions from the NEW Portive event table and existing validated CRM actions. Distinguish `outbound voicemail`, `outbound no answer`, `outbound answered human`, `inbound`, `SIP failure`. Never invent a prior dialogue when there was only voicemail or a dial attempt.
- Keep returned `previous_context` factual and SHORT (e.g., under 500 characters), stripping sensitive information and prompt-injection-like instructions from stored transcript fragments. Give at most verified outcome, last contact time, requested next step and DNC status. Do not expose other leads' data.
- `owner_name` should be empty unless verified; don't treat `contact_name="Medical spa"` as a person's name. For unknown/ambiguous callers do not claim to know which clinic called.

### EXACT response contract

Return `HTTP 200`, `application/json`, with at least:

```json
{
  "type": "conversation_initiation_client_data",
  "dynamic_variables": {
    "business_name": "Example Med Spa",
    "owner_name": "",
    "city": "Austin",
    "timezone": "America/Chicago",
    "lead_id": "EXISTING_LEADS_ID_TEXT",
    "prospect_phone_number": "+15125550123",
    "previous_context": "Previous outbound attempt reached voicemail; no human conversation confirmed."
  }
}
```

This is an **illustrative** response. Send actual data only when confidently verified. For unknown/ambiguous callers, return ALL prompt-required custom variables with safe empty strings, except that the actual verified incoming caller number may be provided in `prospect_phone_number` for a strictly approved caller-level DNC path. `lead_id` MUST be empty if unverified. Prefer extra `caller_match_status` / `prior_call_outcome` variables only if the agent config accepts them; document and update placeholders if necessary.

**Never return** an overridden `first_message`, `conversation_config_override`, voice, model, full prompt or branch unless separately required and approved. Inbound agent's fixed first message MUST stay `Hello.`; the pre-call hook exists for **data only**. Preserve caller identity safety, required truthful identification/disclosures and the agent's conversation style. `system__` variables are reserved; don't try to populate/override them.

### Performance and failure handling

- Target minimal extra call-start latency (ideally under 1 second warm path, bounded around 2 seconds of external lookup; measure and report, do not claim guarantees). Do NOT perform multiple sequential six-second Supabase calls in initiation.
- Database timeout: provide a safe limited fallback context for transient DB failure *after the request has been authenticated and destination validated*. Include diagnostic `context_error`/match state if agent has the variable; do not falsely imply a matched caller.
- Missing/invalid credentials or wrong agent/destination: FAIL CLOSED, not HTTP 200.
- Provide synthetic fixture tests for matching, unknown, ambiguous, withheld numbers, invalid numbers, data-store timeout, wrong destination, DNC, prompt injection in previous context, timezone ambiguity and slow lookup.

## F. Post-call webhook — verify first, store durably

Endpoint: `POST /webhooks/elevenlabs/post-call`

Incoming ElevenLabs post-call event types to support:
- `post_call_transcription` (full transcript and analysis)
- `call_initiation_failure` (including SIP failure metadata)

`post_call_audio` is out of scope / DISABLED initially. If received, authenticate first and ignore without storing base64 audio. If other event types arrive, authenticate, record minimal safe event if justified and acknowledge without mutating sales leads.

### HMAC requirement (non-negotiable)

- Read the **RAW** request bytes first; obtain `ElevenLabs-Signature` header; verify using the *current supported* ElevenLabs Python SDK (`ElevenLabs(...).webhooks.construct_event(rawBody=..., sig_header=..., secret=...)`) or independently verified identical HMAC/time-window algorithm with tests.
- SDK method returns a parsed **dict**, not necessarily an attribute object. Account for SDK exceptions and absent headers. Use a dedicated Vercel secret `ELEVENLABS_POSTCALL_WEBHOOK_SECRET`; fail closed if missing/misconfigured, never accept unsigned webhooks even in production if env absent. Never print or return the secret.
- Reject invalid or stale timestamps, incorrect HMAC, malformed JSON, oversized payloads; cap request size carefully, while allowing legitimate transcript payloads. Don't parse any payload as trusted before signature verification.
- For workspace-wide post-call webhooks, accept/process ONLY allowlisted inbound and outbound PORTIVE SALES `agent_id` strings. Ignore all demo receptionist agent events, with no CRM mutation.
- Handler must write durably BEFORE HTTP 200. On persistence/database outage return non-2xx so ElevenLabs retry logic can redeliver. Avoid non-durable `BackgroundTasks` or fire-and-forget work on serverless Vercel.
- Make delivery idempotent for retries and concurrency. Include `POST` signature verification and replay safety. Never allow replays to regenerate emails, calendar bookings, DNC writes or callback reservations.

### Correct extraction

For transcription events, extract defensively from the actual schema:
- `data.conversation_id`, `data.agent_id`, `data.agent_name`, `data.branch_id`, `data.version_id`, `data.status`
- `event_timestamp` (Unix seconds → timestamptz UTC)
- `data.metadata.call_duration_secs`, `data.metadata.cost` (credits, NOT dollars), `metadata.termination_reason`, start time, telephony metadata (if present)
- `data.transcript` array (role, message, time_in_call_secs, tool_calls, tool_results etc.)
- `data.analysis.transcript_summary`, `data.analysis.call_successful`, `data.analysis.data_collection_results`
- `data.conversation_initiation_client_data.dynamic_variables` for `lead_id`, `prospect_phone_number`, business/timezone when actually present.
- `data.user_id` / SIP phone fields / metadata only when the documented source provides them; never blindly assume all call legs have the same caller and called direction.

For SIP initiation failure events, extract:
- `data.failure_reason` (`busy`, `no-answer`, `unknown`, ...)
- `data.metadata.type` and `data.metadata.body` where SIP provider may include `from_number`, `to_number`, `sip_status_code`, `error_reason`, `call_sid`, `sip_status`, `twirp_code`.
- If missing, leave nullable. `SIP 408/503/504` must not automatically be labeled voicemail. Preserve original technical reason and classify conservatively.
- Correct external-party number: outbound remote is destination; inbound remote is caller. Validate against `PORTIVE_SALES_NUMBER`, agent ID, documented metadata and verified lead binding. Never mistake the Portive caller ID for the prospect and never attach an ambiguous number to a random lead.
- For calls initiated from ElevenLabs CSV, dynamic variables may be available in initiation client data; use `lead_id` only after validating it matches `public.leads.phone_number`, or a securely persisted call binding. Never let malicious transcript text supply lead ID or overwrite verified structured data.

### Durable/idempotent data layer

Create a NEW additive migration in `sql/002_portive_webhooks.sql` (or next free migration name) for `public.portive_call_events` with suitable columns; proposed:

```sql
conversation_id text not null,
event_type text not null,
agent_id text not null,
agent_name text null,
branch_id text null,
version_id text null,
lead_id text null references public.leads(id) on delete set null,
direction text null check (direction in ('inbound','outbound')),
remote_phone_e164 text null,
portive_phone_e164 text null,
call_status text null,
call_outcome text null,
match_status text null,
failure_reason text null,
sip_status_code integer null,
sip_error_reason text null,
sip_call_id text null,
duration_seconds integer null,
elevenlabs_credits integer null,
transcript_summary text null,
transcript jsonb null,
analysis jsonb null,
metadata jsonb null,
raw_payload jsonb not null,
event_timestamp timestamptz null,
received_at timestamptz not null default now(),
updated_at timestamptz not null default now(),
primary key(conversation_id,event_type)
```

Implement exact valid SQL, appropriate indexes `(lead_id, received_at DESC)`, `(remote_phone_e164, received_at DESC)`, `(agent_id, received_at DESC)` and status/outcome if needed. Add RLS, restricted grants to service_role and deny anon/authenticated. Consider data-minimization and cleanup job/docs; transcripts can contain personal data. Do not ingest audio by default.

**Idempotency details:**
- Use conflict-safe `upsert`/insert handling on `(conversation_id,event_type)` but do not overwrite valuable previously populated verified fields with NULL, blank, or a later malformed retry.
- If duplicate with identical payload, return success without duplicate side effects.
- If duplicate with different event content, log masked discrepancy and resolve according to deterministic policy without replacing validated DNC, bookings or callbacks.
- Don't use a single `conversation_id` PK because a call could yield more than one event type.
- Link call to lead only on **verified** unique match, ideally by authoritative `lead_id` + matching normalized phone; otherwise store event unlinked for review. Unknown callers are valid call records with `lead_id = NULL`.
- Avoid conflicting with existing `call_attempts` table or med-spa demo `elevenlabs_conversations` structures; optionally maintain a read-only linkage if approved, not automatic overwrites.

## G. Safe CRM outcomes, analysis, DNC supremacy

Build a deterministic extractor/normalizer, not another free-running LLM. If ElevenLabs Analysis has data collection fields (`call_outcome`, `decision_maker_name`, `pain_point`, `interest_level`, `objection_reason`, `next_action`), parse and store them only if present/valid. Don't require that ElevenLabs analysis was configured already; gracefully support empty analysis and flag `needs_review` where appropriate.

Proposed internal dispositions:
`demo_booked`, `callback_saved`, `overview_sent`, `interested`, `not_interested`, `dnc`, `voicemail`, `no_answer`, `busy`, `ivr_only`, `wrong_number`, `technical_failure`, `unknown`.

Rules:
1. **Verified existing DNC** (`portive_dnc` by phone) ALWAYS outranks every other status; never silently unset or downgrade suppression or schedule future marketing.
2. **Verified action wins**: a demo booked is verified by actual Google Calendar event metadata/tool result where accessible; callback saved by an actual `follow_ups` row or success payload (reconcile with DB); email sent only when `portive_overview_emails.status='accepted'`; DNC by `portive_dnc`. Tool utterances / model analysis alone are NOT proof of persistence.
3. Distinguish `no-answer` / `busy` / SIP error from `voicemail` and from completed conversations; an initiated voicemail call is a completed call (post-call transcription) not necessarily an initiation failure.
4. Never apply a new retry/callback booking automatically from unstructured transcript sentiment. Don't manufacture consent, callback agreements, fees or times.
5. A returned inbound call does NOT retroactively prove interestedness or a sale; remain conservative.
6. Preserve pre-existing non-sales CRM state and derive all new call results in dedicated Portive tables. If updating `leads.derived_status` at all, strictly follow existing CRM supported values, test transitions and never overwrite `closed_lost`, `bad_number`, explicit DNC, or human-edited higher-priority states blindly.
7. Source of truth for DNC is persisted database suppression, not transcript; implement an *alert / quarantine* for uncertain opt-outs so outbound scheduling cannot re-dial before manual review. Don't claim a manual notification without a working persistent mechanism.
8. Idempotent webhook processing must not create an outbound call, calendar event, email or callback on delivery/retry.

## H. Unknown inbound callers: real DNC without fake lead IDs

Existing `POST /tools/add-to-dnc` expects `lead_id`, `called_number` and `conversation_id`, and checks lead-to-number ownership. It CANNOT suppress unmatched caller numbers via an invented ID. Implement a separate secure endpoint, for example `POST /tools/add-to-dnc-inbound-unknown` (or better name consistent with existing tools) with explicit authorization and verified caller context; only use it when the inbound caller requests opt-out and no lead ID is available.

**Security design is required**, not a naive LLM-provided arbitrary phone number:
- Bind target to inbound `system__caller_id` and verified conversation/agent context. The tool config must map the system caller variable directly, not ask the model to invent a number. Prefer verifying `conversation_id`/inbound context by signed initiation audit (save minimal `(conversation_id, caller_e164, agent_id, called_number, received_at)` on authenticated initiation) and/or ElevenLabs API server-side. If authoritative context cannot be verified, use a durable explicit quarantine and operator notification path without allowing untrusted arbitrary number suppressions.
- Treat caller-ID spoofing as possible; avoid disclosure of CRM information and never mutate a matched unrelated lead solely based on ANI.
- `portive_dnc` schema allows `lead_id = NULL`; use safely parameterized atomic UPSERT keyed by `phone_e164` and reason, with idempotency. Never unsuppress an existing record. Do not overwrite a prior lead association with an unverified one.
- Ensure inbound agent can invoke it through an ElevenLabs authenticated Webhook Tool. Provide **exact setup instructions** for `Name`, Description, URL, `X-Portive-Tool-Key`, Body Parameters (data types, requiredness, Dynamic Variable vs LLM extraction), and handling of missing caller ID. Do not claim the tool is active until configured and tested.
- Known caller DNC continues using existing tested tool. If caller is anonymous/unverifiable, never falsely promise suppression. Provide safe manual-review fallback.
- Ensure DNC applies to outbound batch-screening and future scheduler (document existing screening command or inspect existing behavior); a live opt-out is not useful if the next CSV batch ignores the DNC table.

## I. Existing tool configuration and incoming-number mapping

The **existing** routes must continue to receive:
- `lead_id`: actual matched string from Supabase (never generated by model)
- `called_number`: prospect's normalized actual phone (despite legacy field name)
- `conversation_id`: actual ElevenLabs conversation ID.

On inbound SIP, `system__caller_id` is the **clinic's incoming phone**; `system__called_number` is **Portive +13103318914**. Thus do NOT map `called_number = system__called_number` in inbound tools. Correct value must resolve from `prospect_phone_number` derived from verified incoming caller context, or direct system caller mapping with verification.

For unknown/ambiguous calls, prohibit existing known-lead callback/email/DNC tools without a valid lead association; do not invent IDs. Unknown inbound caller can ask general questions, be politely redirected, or use the dedicated safe no-contact route.

Do NOT modify inbound agent's conversation first message: it is configured in ElevenLabs as `Hello.`. The webhook must not set an override for it. Preserve the established five-stage missed-call callback-recovery conversation and Portive disclosure/identity obligations.

## J. Data protection, operational robustness and billing concerns

- Validate all incoming bodies with Pydantic/manual schema as appropriate, min/max lengths, expected number formats and allowlisted agents; reject unknown auth.
- Keep FastAPI HTTP response short; no sensitive raw objects echoed.
- Use HTTPS, secrets in Vercel environment settings, no hardcoded credentials, no checked-in `.env`.
- Keep logs structured and redacted; signature failures 401, unauthorized agents safe handled; avoid leaking transcript content into Vercel logs.
- Bound Supabase queries and network timeout; connection pooling is okay. Use `httpx` in a way that doesn't block FastAPI event loop for too long; choose threadpool or `async` as appropriate.
- Evaluate PostgREST `Prefer` options, on_conflict upsert and null merging correctly. Beware `sb()` returning None for empty 204 responses; tests must simulate it.
- Keep DB transactionally consistent across event write, linkage and outcome; use SQL RPC for multi-table atomic transitions only if truly necessary.
- Distinguish ElevenLabs `metadata.cost` **credits** from dollar costs. Don't multiply currency blindly. Avoid making cost claims without actual usage verification.
- Minimize transcripts, provide retention plan and access controls. Don't store base64 audio in call-event JSON even if accidentally delivered.
- Document `ElevenLabs-Signature` HMAC verification, delivery retries, deduplication and webhook auto-disable monitoring.
- No unbounded retries from app, no infinite loops, no auto-replay of paid tools.
- Respect caller ID may be missing, spoofed, different from originally dialed, or a central business number.
- Respect caller timezone from verified data only; `Asia/Karachi` on developer dashboard is not the spa's local timezone.

## K. Test suite — mandatory synthetic fixtures

Add isolated mocks/in-memory tests, do not connect to production in unit tests. Retain and run existing tests. Cover AT LEAST:

**Initiation:**
1. Correct secret, correct inbound agent & sales destination, unique lead: response with all variables.
2. Unknown caller: placeholders, no fabricated `lead_id`, call not blocked.
3. Duplicate same phone across two `lead_list_id` values: ambiguous, no arbitrary lead ID.
4. Formatting variants: `+1`, punctuation/whitespace, 10-digit; normalization consistent.
5. Wrong agent ID (including demo agent) rejected/no data.
6. Wrong destination number rejected/no data.
7. Missing/wrong header 401.
8. Caller ID private/unavailable/invalid: safe fallback.
9. Caller present in DNC: suppression context and no solicitation.
10. Existing only-voicemail outreach: no invented prior human discussion.
11. Actual prior human conversation: brief verified context without private over-sharing.
12. Slow Supabase and transient 502: short fallback, not multi-second hang.
13. No timezone source and generic contact label: no invented person/timezone.
14. No override of inbound `Hello.`.

**Post-call:**
15. Valid signed `post_call_transcription` persists transcript, summary, analysis, agent/version, credits.
16. Valid signed `call_initiation_failure` persists SIP 408/486 etc. precisely and no fake voicemail.
17. Missing/bad/stale `ElevenLabs-Signature` rejected without DB writes.
18. Unknown/unauthorized agent/demo agent safely ignored without Portive lead mutation.
19. Repeated same event delivery: exactly one record, no side effects.
20. Different event types for same conversation: separate rows.
21. Durable Supabase failure: non-200 (retryable), no fake success.
22. Missing/non-E164 recipient number: event stored unlinked.
23. Outbound caller/called vs inbound caller/called mapping, never suppress +13103318914 accidentally.
24. Mismatched lead ID/phone from payload: do not link to unrelated lead.
25. DNC already exists: status cannot be downgraded.
26. Hallucinated booking in transcript but no calendar DB proof: not marked booked.
27. Callback recorded by tool: verified outcome, no duplicate callback on webhook replay.
28. Unrelated message/audio webhook authenticated and handled safely.
29. Malformed/huge payload rejected safely, secrets never logged.

**DNC:**
30. Unknown caller with cryptographically/signed context can opt out without fake lead.
31. Arbitrary forged caller phone cannot suppress someone else.
32. Idempotent repeated suppression.
33. Existing known-lead DNC route works unchanged.
34. Unverifiable no-contact request is persistently quarantined/alerts for manual review if configured; never falsely confirms.

**Regression:**
35. All existing `tests/test_tools.py` / Gmail / unsubscribe / SQL-dependent tests pass.
36. Existing production routes and their response contracts unaffected.

Provide fixtures modeled on OFFICIAL ElevenLabs webhook examples and synthetic med-spa test leads. Don't insert or email real scraped leads.

## L. Files and deliverables

At minimum produce:

1. Changed/appended FastAPI modules and router registered in `app/main.py`.
2. An **additive** numbered SQL migration for new event table, normalized number lookup and minimal signed call-context table if needed for secure unmatched DNC.
3. Updated `requirements.txt` with supported pinned/ranged `elevenlabs` SDK version and any required test dependencies.
4. Updated `.env.example` (no real secrets), `README.md` and detailed `docs/ELEVENLABS_PORTIVE_WEBHOOKS.md`.
5. Synthetic payload fixtures and focused pytest tests, plus no-regression tests.
6. Optional masked local test script for unsigned initiation (secret header) and SDK-signed local post-call fixture (do not paste real production secret into test data).
7. A concise change summary + list of changed files + local commands + SQL migration instructions + exact dashboard clickpath + known risks.

## M. Required environment variables

Use/document (exact names unless repo has a consistent alternative):

```
SUPABASE_URL=...
SUPABASE_SERVICE_ROLE_KEY=...
PORTIVE_TOOL_API_KEY=...           # existing; do not rotate/change casually
PUBLIC_BASE_URL=https://11labs-tools.vercel.app
PORTIVE_INITIATION_SECRET=...     # NEW distinct random header secret
ELEVENLABS_POSTCALL_WEBHOOK_SECRET=...  # NEW ElevenLabs-generated HMAC secret
ELEVENLABS_API_KEY=...            # only if SDK/server API needs it; never expose
PORTIVE_INBOUND_AGENT_ID=agent_...
PORTIVE_OUTBOUND_AGENT_ID=agent_...
PORTIVE_SALES_NUMBER=+13103318914
```

If needed define `PORTIVE_WEBHOOK_ENABLED` feature flag(s), separate from tool endpoints, to allow safe staged rollout without deployment rebuild; default safe. Never use a secret dynamic variable in spoken prompt.

## N. Manual configuration runbook you must write for me

**1. Supabase:** backup → review/run ONLY new additive SQL → validate table/indexes/grants/RLS → verify old DNC & callback RPCs still exist.

**2. Vercel:** project `11labs-tools`, correct production branch and settings → add environment variables with correct scopes → redeploy → `/health` still 200 and old live tools behave unchanged → new endpoints reachable and authenticated (unauthenticated must fail, this is expected).

**3. ElevenLabs initiation:** workspace ElevenAgents settings / initiation webhook config → exact `https://11labs-tools.vercel.app/webhooks/elevenlabs/initiation` URL → attach `X-Portive-Initiation-Key` secret header → enable `Fetch initiation client data from a webhook` under **inbound sales Ava Security** only; no prompt/voice/first-message override allowed; the First Message is exactly `Hello.`. Since agent preview does NOT run initiation webhook, test through real consented inbound SIP phone call.

**4. ElevenLabs post-call:** Developers/Webhooks + ElevenAgents workspace/agent settings → exact `https://11labs-tools.vercel.app/webhooks/elevenlabs/post-call` URL → HMAC secret `ELEVENLABS_POSTCALL_WEBHOOK_SECRET` in Vercel → enable transcription and call-initiation failure types, **disable audio**, **enable retries**; if webhook applies workspace-wide, app filter MUST exclude demo receptionist. Document dashboard wording variances and verify actual UI.

**5. Inbound phone:** My number `+13103318914` has already been shown assigned to `Ava | Portive Inbound Agent` on `Main`. Do NOT change SIP trunk or existing outbound route in this coding task. Confirm manually that Telnyx inbound points to ElevenLabs and make controlled test call.

**6. Live controlled tests:** one known test clinic number and one unknown number I own; one harmless known-person callback, one tool-confirmed callback, one explicit unknown caller opt-out, one DNC from known test lead, one outbound controlled call to ensure no regression; verify Vercel HTTP statuses and table contents. Never actually dial a real spa in tests.

**7. Test SQL examples:** supply SQL to inspect latest 20 Portive call events, filter by lead/phone, inspect recent SIP failures, inspect ambiguous matches/unknowns and suppressed callers, and verify duplicate event counts. Include a query for webhook health/failure rate if possible.

**8. Rollback:** disable webhook use in ElevenLabs, keep original routes functional, no destructive dropping of event history; safe code rollback documented. Warn about deployment ordering (schema first, app second, enable webhook last).

## O. Acceptance criteria — done means ALL of this is true

- Inbound request with valid header for an actual known unique caller returns correct custom dynamic variables **without** overriding `Hello.`.
- Unknown caller still reaches inbound Ava without fabricated CRM identity.
- Two leads sharing a telephone number are not arbitrarily linked.
- Unknown-caller DNC can be durably and safely persisted without fabricated lead ID, with protected identity/context.
- Post-call HMAC verified before parse/DB; invalid signature rejected.
- Inbound and outbound Portive events stored, demo agent events excluded.
- SIP 408/486/busy/no-answer reason preserved; voicemail is not confused with failed initiation.
- Webhook retries are idempotent; no duplicate emails, callbacks, DNC or calendar actions.
- Existing DNC and `save_callback` SQL functions and all live tools still work unchanged.
- Event storage never picks the wrong prospect number or business from ambiguous data.
- DNC remains highest-priority and never undone by lower-confidence analysis.
- All tests pass, `python -m compileall` passes, code meets repo conventions.
- Documentation identifies every manual secret, setting and step I need after reviewing the diff.

## P. Work style and final response

**Actually make the code edits in the workspace** and run local tests; don't just list instructions. Read before writing, use coherent modules, minimum disruption, clear types, no credentials in logs. Prioritize correctness, idempotency, call-start latency and fail-closed auth over clever abstractions. If one detail of the ElevenLabs API or database cannot be verified, consult current official docs or code/tests and call out the exact remaining unknown—do not hallucinate a payload or claim a production test passed.

At the end return:
1. `IMPLEMENTED / TESTED / STILL REQUIRES MY ACTION` clearly separated.
2. All modified file paths and why.
3. SQL migration filename and exact application order.
4. Exact Vercel secrets, ElevenLabs webhook URLs, agent tool mapping and UI settings.
5. Commands run and tests passed/failed.
6. Copy/paste verification SQL + controlled test commands.
7. Remaining operational gaps (e.g., separate outbound scheduler/callback executor are NOT done by these webhooks).

**Do NOT git commit, push, deploy, change Telnyx number assignment, send email, place telephone calls, or run SQL against production without explicit approval. Begin by inspecting the repository and then IMPLEMENT all approved local changes.**
