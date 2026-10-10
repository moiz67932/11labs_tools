# Attach these THREE tools to Ava (Agent → Tools → Add Webhook Tool)

Auth: add a custom secret header named `X-Portive-Tool-Key` whose value is
exactly the backend env var `PORTIVE_TOOL_API_KEY`. Use ElevenLabs Secret,
NOT a value pasted into the system prompt. Method POST, JSON body.

All three tools share fixed (dynamic variable) body inputs:

- `lead_id` = `{{lead_id}}` from your batch CSV; mark Dynamic variable, NOT LLM.
- `called_number` = `{{system__called_number}}` (SIP destination); Dynamic variable.
- `conversation_id` = `{{system__conversation_id}}`; Dynamic variable.

## 1. add_to_dnc
URL: `https://YOUR_HOST/tools/add-to-dnc`
Description: "Use IMMEDIATELY on any clear request not to call again / take off list.
Never promise suppression until success=true and suppressed=true. Do not ask for
contact details; end the call after recording or flag for urgent manual suppression."
Body: shared inputs + constant `reason`: `requested_no_contact`.
Expected success: `{"success":true,"suppressed":true}`.

## 2. save_callback
URL: `https://YOUR_HOST/tools/save-callback`
Description: "Use ONLY after an actual callback request or agreed follow-up.
Confirm a precise local date/time and timezone; use ISO 8601 including offset.
Do not invent an appointment or claim the later call is automatically placed."
Body: shared inputs + LLM parameters:
- `contact_name`: target person, e.g. Jessica
- `callback_at_local`: e.g. `2026-10-13T14:00:00-05:00` (US Texas October)
- `timezone`: IANA timezone `America/Chicago`, ideally from batch variable
- `note`: brief context; e.g. `Emily said owner Jessica can talk Tuesday at 2 PM.`
Expected success: `{"success":true,"saved":true,"callback_at_utc":"..."}`.
If given only "Tuesday afternoon", ask for a specific time before using the tool.

## 3. send_portive_overview
URL: `https://YOUR_HOST/tools/send-portive-overview`
Description: "Use ONLY when a person explicitly asks for overview information by email.
Confirm the recipient personal or business email out loud, then call the tool once.
Never claim inbox delivery; provider acceptance is not proof of delivery."
Body: shared inputs + LLM parameters:
- `recipient_email`: confirmed address
- `recipient_name`: person receiving the overview, if known (optional)
Expected success: `{"success":true,"accepted_by_provider":true,"provider":"gmail","provider_email_id":"...","already_sent":false,"delivered_to_inbox":false}`.

For `email_result_uncertain`, `email_already_attempted`, or
`email_accepted_audit_unconfirmed`, do not invoke again with a new conversation
ID or claim delivery. Flag manual Gmail/Supabase reconciliation. See
[server Gmail setup and error codes](../docs/GMAIL_SETUP.md).

## Minimal system-prompt addition

```markdown
# CRM tool actions
If the person requests no further calls, immediately call `add_to_dnc`,
then end the conversation. If it fails, acknowledge and flag urgent manual removal.
If the person agrees to a precise callback, confirm local date/time/timezone
and call `save_callback`; only say it was saved after success=true.
If the person explicitly asks for Portive information by email, confirm the
email and call `send_portive_overview`; describe success as submitted for sending,
not delivered. Never invent a successful action.
```

## Voice-call preview caveat

Text-only ElevenLabs previews may not populate `system__called_number` for a
real business; use a controlled SIP test for end-to-end validation. Calendar
booking is unaffected by these new endpoints.

## Error behavior

A business-logic error responds `{"success":false,"code":"..."}`. Ava must
NOT tell a prospect an action was completed unless the boolean confirms it.
An authentication error is HTTP 401; DNC/callback database errors are HTTP 502.
Overview database/configuration failures return `success=false` with a compact code;
an invalid overview address returns HTTP 422 and `invalid_recipient_email`.
