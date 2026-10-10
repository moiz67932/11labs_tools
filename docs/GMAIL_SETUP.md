# Gmail setup on Fedora and Vercel

This backend sends requested Portive overviews from your own Gmail account.
A purchased sender domain is unnecessary. Any valid personal or business address
can receive an overview; only the sending account participates in OAuth.
Gmail policies, account sending limits, and recipient suppression still apply.
This change does not configure Google Calendar or change ElevenLabs scheduling.

## One-time local authorization

1. Open [Google Cloud Console](https://console.cloud.google.com/), select or
   create a project, and enable **Gmail API** under APIs & Services > Library.
2. Open Google Auth Platform. Configure Branding (app name, support/contact
   email), Audience (External for personal Gmail), and Data Access. Add only
   `https://www.googleapis.com/auth/gmail.send`. While in Testing, add the
   sending Gmail account as a test user. Do not request read, modify, profile,
   or Calendar scopes.
3. Under Clients, create a **Desktop app** OAuth client. Download the client
   JSON outside this repository, e.g. into your private Downloads folder.
4. On Fedora, in this project directory:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python3 -m pip install -r requirements-oauth.txt
   python3 scripts/setup_gmail_oauth.py --client-secrets ~/Downloads/client_secret.json
   ```

5. Enter the exact Gmail address you will authorize. A local browser opens;
   choose that same account and authorize send-only access. The script uses
   offline access, explicit consent, and an ephemeral loopback callback on
   `127.0.0.1`. Finish within three minutes. No public OAuth callback is needed.
6. The script creates `.env.gmail` with permissions `0600`, refuses to overwrite
   an existing file, and never prints credential values. Open it privately in
   your editor. Copy its five settings into your ignored `.env` for local use
   and into Vercel as described below. Remove the private generated file after
   securely storing its values. Do not upload client JSON or token files.

The send-only scope cannot read the account profile. `GMAIL_SENDER_EMAIL` must
be the account selected during consent; do not configure a different sender or
an unverified alias. `GMAIL_REPLY_TO` defaults to that sender if empty and may be
a different valid reply address. Both settings accept bare addresses.

## Token expiry and production requirements

Google issues refresh tokens expiring after **seven days** for External apps
in **Testing** when requesting `gmail.send`. Reauthorize locally when necessary;
changing Vercel access-token files will not help. For continuing production use,
review Google's publishing and verification requirements. `gmail.send` is a
**sensitive** scope. Public use may require OAuth verification; any applicable
personal-use exceptions must be evaluated under Google's rules. If verification
requires branding, privacy-policy pages or domain ownership, meet those
requirements; this implementation does not bypass them. Moving to Production
is not a promise that tokens never expire. Revocation, account/password changes,
inactivity, and Google policies can still invalidate authorization.

Official references: [OAuth token expiry](https://developers.google.com/identity/protocols/oauth2#expiration),
[Gmail scopes and verification](https://developers.google.com/workspace/gmail/api/auth/scopes),
[Desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app),
[OAuth verification FAQ](https://support.google.com/cloud/answer/9110914).

## Vercel configuration

Keep the existing `index.py` FastAPI entry point and URLs. No database migration,
worker, filesystem token cache, or new infrastructure is required. Vercel installs
`requirements.txt`; `requirements-oauth.txt` is for local authorization only.

In Project Settings > Environment Variables, configure the intended deployment
environments with:

| Variable | Value |
| --- | --- |
| `GOOGLE_CLIENT_ID` | Desktop client ID from the private generated file |
| `GOOGLE_CLIENT_SECRET` | Matching client secret |
| `GOOGLE_REFRESH_TOKEN` | Offline refresh token from local authorization |
| `GMAIL_SENDER_EMAIL` | Exact account that authorized OAuth |
| `GMAIL_REPLY_TO` | Reply address; optional, defaults to sender |
| `SUPABASE_URL` | Existing project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Existing server-only database key |
| `PORTIVE_TOOL_API_KEY` | Existing ElevenLabs secret header value |
| `BUSINESS_POSTAL_ADDRESS` | Real sender mailing address |
| `PUBLIC_BASE_URL` | Stable HTTPS URL of this FastAPI deployment |
| `UNSUBSCRIBE_SIGNING_SECRET` | Existing signing secret; retain for old links |

Redeploy after changing environment values. Remove obsolete `RESEND_API_KEY`,
`RESEND_FROM`, and `RESEND_REPLY_TO` from local/Vercel settings after switching.
No code consumes them anymore. Never use frontend/public environment prefixes.
The runtime refreshes access tokens in memory from environment credentials and
makes exactly one Gmail send call for a newly reserved database request.

## Ava behavior and error recovery

Keep the same webhook URL, schema and `X-Portive-Tool-Key`. Ava must confirm the
address and invoke the tool **only after an explicit request**. This unchanged
schema relies on Ava to enforce consent; the backend cannot infer spoken consent.
Success means Gmail accepted and returned a message ID, never inbox delivery or
reading. The ID is saved in `portive_overview_emails.provider_email_id`.

| Code | Operator action |
| --- | --- |
| `gmail_configuration_missing` / `gmail_configuration_invalid` | Set the four required credentials and valid sender/reply addresses, redeploy |
| `email_configuration_missing_or_invalid` | Set real postal address, HTTPS public URL and unsubscribe signing secret |
| `invalid_recipient_email` | Confirm a valid address; no email attempted |
| `gmail_oauth_invalid` | Reauthorize locally; replace refresh token and redeploy |
| `gmail_oauth_unavailable` | Check OAuth connectivity; no Gmail send attempted |
| `gmail_permission_denied` | Check Gmail API enablement, consent scope and account policies |
| `gmail_rate_limited` | Check Gmail account/API limits; no automatic resend |
| `gmail_send_rejected` | Check account configuration and recipient; no automatic resend |
| `contact_suppressed` | Respect suppression; do not resend |
| `email_already_attempted` | Inspect existing audit record and Gmail Sent; do not change request context to force retry |
| `email_result_uncertain` | Inspect Gmail Sent and audit record manually; never automatically resend |
| `email_accepted_audit_unconfirmed` | Gmail accepted; reconcile audit using returned Gmail ID; do not resend |
| `email_database_unavailable` | Database action unconfirmed; inspect reservation before any follow-up |

Pending, failed, and uncertain reservations all block later sends with the same
`(lead_id, conversation_id, normalized email)`. Gmail has no Resend-style
idempotency key: a timeout, 5xx or malformed success response may still mean the
message was sent. Even if audit writes fail, the original reservation remains.
If Gmail accepted but the audit update failed, manually reconcile that record
with the returned message ID. Do not delete reservations or create new
conversation IDs to bypass protection. The send-only token cannot inspect Sent;
use the Gmail UI for reconciliation. Existing accepted legacy records are also
left untouched and block resends; their IDs may belong to the previous provider.

## Tests and an optional real send

```bash
python3 -m pip install pytest
python3 -m pytest -q
python3 -m compileall -q app scripts tests
```

Automated tests mock both Google OAuth and Gmail/Supabase; no email is sent.
For one real email, use a controlled CRM lead matching its called phone and an
address whose recipient explicitly requested the overview. This command reads
local `.env`, uses the existing tool implementation, and records a normal audit:

```bash
python3 scripts/test_gmail_send.py \
  --recipient ADDRESS_YOU_EXPLICITLY_PROVIDE \
  --lead-id CONTROLLED_CRM_LEAD_ID \
  --called-number +15125550123 \
  --conversation-id controlled-gmail-test-001 \
  --confirmed-request
```

Reuse the same conversation ID for that attempt. Do not run it after an uncertain
outcome until you have reconciled Gmail and Supabase. This smoke test sends a
real overview, with postal address and signed unsubscribe link, without placing
a call or modifying Calendar. Test the deployed endpoint separately with a
controlled ElevenLabs call when credentials and deployment are ready.
