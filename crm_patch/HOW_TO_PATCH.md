# Guard the EXISTING ColdCallerCRM dialer before any phone call

In your `ColdCallerCRM` repo, copy `portive-dnc-guard.ts` into `lib/portive-dnc-guard.ts`.

Edit `lib/telnyx/call-flow.ts`:

1. Add import at file top:

```ts
import { assertNotSuppressed } from "@/lib/portive-dnc-guard";
```

2. Inside `createOutboundCallAttempt`, AFTER validating `lead` exists and BEFORE
   creating the attempt, add:

```ts
await assertNotSuppressed(lead.phoneNumber);
```

3. Inside `initiateOutboundCall`, AFTER validating `attempt` exists and BEFORE
   `getTelnyxClient().calls.dial(...)`, add:

```ts
await assertNotSuppressed(attempt.lead.phoneNumber);
```

4. Check EVERY additional/manual/WebRTC/dial/batch initiation route for bypasses.
   A guard in this one file does not by itself protect separate ElevenLabs batches,
   other services, or any direct client-originated dialing path.

5. Run `npm run lint`, `npx tsc --noEmit`, and your calling-flow tests.

6. Validate: choose a TEST lead, suppress its number with the FastAPI tool,
   then attempt to dial it from the CRM. Expected outcome: **blocked**.
   Repeat with a SECOND lead/list using the same number; expected blocked.

Do NOT add a special `derived_status='dnc'` value without updating the
CRM status enum/UI; suppression is an independent phone-level table.
