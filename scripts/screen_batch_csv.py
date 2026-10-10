#!/usr/bin/env python3
"""Preflight an ElevenLabs batch CSV against ColdCallerCRM and Portive DNC.

Usage: python scripts/screen_batch_csv.py input.csv allowed.csv

Only lead IDs present in CRM, status 'new', phone matching that lead, and
not suppressed are included. THIS IS NOT A TCPA consent audit.
"""
from __future__ import annotations
import csv
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core import sb, normalize_phone, is_dnc


def main(input_file: str, output_file: str):
    inp, out = Path(input_file), Path(output_file)
    with inp.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames or []
        if not {"phone_number", "lead_id"}.issubset(columns):
            raise SystemExit("CSV must include exact columns: phone_number, lead_id")
        rows = list(reader)
    kept = []
    removed = []
    for row in rows:
        lead_id = (row.get("lead_id") or "").strip()
        try:
            phone = normalize_phone(row["phone_number"])
            data = sb("GET", "leads", params={"id":f"eq.{lead_id}",
                                                "select":"id,phone_number,derived_status", "limit":"1"}) if lead_id else []
            if not data:
                raise ValueError("missing_crm_lead")
            lead = data[0]
            if normalize_phone(lead["phone_number"]) != phone:
                raise ValueError("lead_phone_mismatch")
            if lead["derived_status"] != "new":
                raise ValueError("not_new_lead")
            if is_dnc(phone):
                raise ValueError("dnc_suppressed")
            # Normalize output for ElevenLabs; preserve all other dynamic variables.
            row["phone_number"] = phone
            kept.append(row)
        except ValueError as exc:
            removed.append((lead_id or "<missing>", str(exc)))
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(kept)
    print(f"Input {len(rows)} | allowed {len(kept)} | excluded {len(removed)}")
    for lead_id, reason in removed:
        print(f"  EXCLUDED {lead_id}: {reason}")
    print(f"Allowed CSV: {out}")
    print("Manual TCPA/state-law eligibility and calling-hour checks are STILL REQUIRED.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python scripts/screen_batch_csv.py INPUT.csv SAFE_OUTPUT.csv")
    main(sys.argv[1],sys.argv[2])
