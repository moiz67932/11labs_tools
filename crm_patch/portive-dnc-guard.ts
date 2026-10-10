// COPY TO: lib/portive-dnc-guard.ts in the existing ColdCallerCRM repository.
// IMPORTANT: Call assertNotSuppressed() on ALL server-side outbound call paths.
import { parsePhoneNumberFromString } from "libphonenumber-js";
import { getSupabaseAdmin } from "@/lib/supabase-admin";

export function toE164ForDnc(rawPhone: string): string {
  const parsed = parsePhoneNumberFromString(rawPhone, "US");
  if (!parsed?.isPossible()) {
    throw new Error("Invalid destination number; outbound call blocked");
  }
  return parsed.number;
}

export async function assertNotSuppressed(rawPhone: string): Promise<void> {
  const phone = toE164ForDnc(rawPhone);
  const { data, error } = await getSupabaseAdmin()
    .from("portive_dnc")
    .select("phone_e164")
    .eq("phone_e164", phone)
    .limit(1);

  // NEVER fail open: if the database can't be checked, don't initiate the call.
  if (error) throw new Error("DNC status could not be verified; outbound call blocked");
  if (data?.length) throw new Error("DNC: outbound call blocked for suppressed number");
}
