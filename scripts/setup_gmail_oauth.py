#!/usr/bin/env python3
"""One-time, local Fedora OAuth setup. Never run in Vercel requests."""
import argparse
import logging
import os
from pathlib import Path

INSTRUCTIONS = """
1. In https://console.cloud.google.com/ select/create your project, then
   APIs & Services > Library > Gmail API > Enable.
2. In Google Auth Platform configure Branding (app name and support email),
   Audience (External for a personal Gmail account), and Data Access. Add ONLY
   https://www.googleapis.com/auth/gmail.send. In Testing add your own Gmail
   account as a test user. Recipients do not need to be OAuth test users.
3. Under Clients create an OAuth client with application type Desktop app.
   Download its JSON outside this repository (for example ~/Downloads/).
4. Install local dependencies: python3 -m pip install -r requirements-oauth.txt
5. Run this script with --client-secrets /path/to/client_secret.json.
   A browser opens locally; select the Gmail account that will send mail.
   Enter that SAME account's address when prompted. gmail.send cannot read
   your profile, so the sender address must be supplied by you accurately.

External/Testing refresh tokens for gmail.send expire after seven days.
For ongoing use, review Google's production publishing and verification rules.
gmail.send is a sensitive scope; verification may be required for production,
particularly for a public app. Production does not guarantee perpetual tokens;
revocation and other expiry conditions still apply. Do not bypass Google warnings
or security requirements. See docs/GMAIL_SETUP.md for official references.

Credentials are written to a NEW private .env.gmail file, never printed.
Copy its GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN,
GMAIL_SENDER_EMAIL and GMAIL_REPLY_TO into Vercel Project Settings >
Environment Variables for the intended deployment environments, then redeploy.
Also retain all existing Supabase, tool-key, postal-address, public-URL and
unsubscribe-secret variables. No purchased sender domain is needed.
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-secrets', type=Path)
    parser.add_argument('--output', type=Path, default=Path('.env.gmail'))
    args = parser.parse_args()
    print(INSTRUCTIONS)
    if args.client_secrets is None:
        return
    # The library logs loopback request URLs, which contain authorization codes.
    logging.getLogger("google_auth_oauthlib.flow").disabled = True
    from google_auth_oauthlib.flow import InstalledAppFlow
    from pydantic import EmailStr, TypeAdapter

    if args.output.exists():
        parser.error('Output already exists; choose a new private output file.')
    try:
        sender = str(TypeAdapter(EmailStr).validate_python(input('Gmail account you will authorize: ').strip()))
        flow = InstalledAppFlow.from_client_secrets_file(
            str(args.client_secrets), scopes=['https://www.googleapis.com/auth/gmail.send'])
        credentials = flow.run_local_server(
            host='127.0.0.1', port=0, open_browser=True, access_type='offline',
            prompt='consent select_account', authorization_prompt_message=None,
            success_message='Authorization complete. You can close this tab.', timeout_seconds=180)
        if not credentials.refresh_token:
            parser.error('No refresh token issued. Review account consent, then authorize again.')
        values = {
            'GOOGLE_CLIENT_ID': credentials.client_id,
            'GOOGLE_CLIENT_SECRET': credentials.client_secret,
            'GOOGLE_REFRESH_TOKEN': credentials.refresh_token,
            'GMAIL_SENDER_EMAIL': sender,
            'GMAIL_REPLY_TO': sender,
        }
        if any(not value or '\n' in value or '\r' in value for value in values.values()):
            parser.error('Invalid credential format; no credentials saved.')
        # Exclusive creation and owner-only permissions from the first write.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(''.join(f'{key}={value}\n' for key, value in values.items()))
    except Exception:
        parser.exit(1, 'OAuth setup failed; review local browser consent and client configuration. No secrets displayed.\n')
    print('Private credentials saved. Copy the five values into Vercel securely, then redeploy.')


if __name__ == '__main__':
    main()
