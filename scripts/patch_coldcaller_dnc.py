#!/usr/bin/env python3
"""Idempotent, fail-closed patch of the inspected ColdCallerCRM main call flow.

Usage: python scripts/patch_coldcaller_dnc.py /path/to/ColdCallerCRM --apply
Without --apply, prints planned work. Creates .portive-backup of the TS file.
For repo revisions with different source, refuses to guess edits.
"""
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]


def patch(repo:Path, apply=False):
    target=repo/"lib/telnyx/call-flow.ts"
    guard=repo/"lib/portive-dnc-guard.ts"
    if not target.exists(): raise SystemExit(f"Not found: {target}")
    text=target.read_text()
    import_line='import { assertNotSuppressed } from "@/lib/portive-dnc-guard";\n'
    marker_create='  const attempt = await prisma.callAttempt.create({'
    marker_dial='  const client = getTelnyxClient();'
    function_create='export async function createOutboundCallAttempt(leadId: string) {'
    function_dial='export async function initiateOutboundCall(callAttemptId: string) {'
    if function_create not in text or function_dial not in text or marker_create not in text or marker_dial not in text:
        raise SystemExit("Current call-flow.ts differs from inspected version; patch aborted")
    p_create=text.index(function_create)
    p_dial=text.index(function_dial)
    if p_create>=p_dial:raise SystemExit("Unexpected function order; patch aborted")
    part1=text[p_create:p_dial]
    part2=text[p_dial:]
    if 'await assertNotSuppressed(lead.phoneNumber);' not in part1:
        part1=part1.replace(marker_create,'  await assertNotSuppressed(lead.phoneNumber);\n\n'+marker_create,1)
    if 'await assertNotSuppressed(attempt.lead.phoneNumber);' not in part2:
        part2=part2.replace(marker_dial,'  await assertNotSuppressed(attempt.lead.phoneNumber);\n\n'+marker_dial,1)
    new=text[:p_create]+part1+part2
    if import_line not in new:new=import_line+new
    print(f"Target: {target}\nGuard: {guard}")
    print("Adds checks before call-attempt creation and Telnyx Call Control dial")
    if not apply:
        print("DRY RUN: nothing written. Add --apply to perform edits")
        return
    if new==text and guard.exists():
        print("Already patched; no modifications")
        return
    backup=target.with_name(target.name+".portive-backup")
    if not backup.exists():shutil.copy2(target,backup)
    target.write_text(new)
    guard.write_text((ROOT/"crm_patch/portive-dnc-guard.ts").read_text())
    print(f"Applied. Backup: {backup}")
    print("Run npm run lint and npx tsc --noEmit. Audit all other dial paths!")


if __name__=="__main__":
    if len(sys.argv)<2:raise SystemExit("Usage: python scripts/patch_coldcaller_dnc.py /path/to/ColdCallerCRM [--apply]")
    patch(Path(sys.argv[1]),"--apply" in sys.argv[2:])
