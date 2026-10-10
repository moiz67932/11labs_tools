#!/usr/bin/env python3
"""Explicit single-send smoke test through the existing endpoint and audit flow."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recipient', required=True)
    parser.add_argument('--lead-id', required=True)
    parser.add_argument('--called-number', required=True)
    parser.add_argument('--conversation-id', required=True, help='Reuse this ID for the same attempt; never change it to retry an uncertain send.')
    parser.add_argument('--confirmed-request', action='store_true', required=True, help='Recipient explicitly requested this email.')
    args = parser.parse_args()
    from app.main import OverviewRequest, send_portive_overview
    from app.core import env
    try:
        request = OverviewRequest(lead_id=args.lead_id, called_number=args.called_number,
                                  conversation_id=args.conversation_id, recipient_email=args.recipient)
        result = send_portive_overview(request, x_portive_tool_key=env('PORTIVE_TOOL_API_KEY'))
    except Exception:
        parser.exit(1, 'Test failed; review configuration and CRM context. No credentials displayed.\n')
    # No credentials or recipient data in terminal output.
    print('Gmail accepted; audit saved.' if result.get('success') else 'Failed: ' + result.get('code', 'unconfirmed'))
    if not result.get('success'):
        sys.exit(1)


if __name__ == '__main__':
    main()
