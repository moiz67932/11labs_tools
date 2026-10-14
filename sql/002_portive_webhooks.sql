-- Portive sales webhook history. ADDITIVE ONLY: do not rerun or replace 001.
-- Review against the target Supabase project and take a backup before applying.
-- Assumes public.leads.id is TEXT, as in the supplied CRM schema.

create or replace function public.portive_normalize_phone(p_value text)
returns text language plpgsql immutable strict as $$
declare v_digits text;
begin
  v_digits := regexp_replace(btrim(p_value), '[^0-9]', '', 'g');
  if left(btrim(p_value), 1) = '+' then
    if v_digits ~ '^[1-9][0-9]{7,14}$' then return '+' || v_digits; end if;
  elsif v_digits ~ '^[0-9]{10}$' then
    return '+1' || v_digits;
  elsif v_digits ~ '^1[0-9]{10}$' then
    return '+' || v_digits;
  end if;
  return null;
end $$;

-- Keeps lookup indexed while preserving the existing legacy phone_number text.
create index if not exists leads_portive_normalized_phone_idx
  on public.leads (public.portive_normalize_phone(phone_number));

-- SECURITY DEFINER avoids granting direct lead access to browser roles. Its result
-- is intentionally bounded, and never chooses a duplicate for the application.
create or replace function public.portive_find_leads_by_phone(p_phone_e164 text)
returns table(id text, business_name text, contact_name text, city text, timezone text)
language sql stable security definer set search_path=public as $$
  select l.id, l.business_name, l.contact_name, l.city,
         coalesce(l.custom_fields_json->>'timezone', '') as timezone
  from public.leads l
  where public.portive_normalize_phone(l.phone_number) = p_phone_e164
  order by l.id
  limit 3
$$;

revoke all on function public.portive_find_leads_by_phone(text) from public, anon, authenticated;
grant execute on function public.portive_find_leads_by_phone(text) to service_role;

create table if not exists public.portive_inbound_contexts (
  conversation_id text primary key,
  caller_e164 text not null,
  agent_id text not null,
  called_e164 text not null,
  received_at timestamptz not null default now()
);
create index if not exists portive_inbound_contexts_caller_idx
  on public.portive_inbound_contexts (caller_e164, received_at desc);

create table if not exists public.portive_call_events (
  conversation_id text not null,
  event_type text not null,
  agent_id text not null,
  agent_name text null,
  branch_id text null,
  version_id text null,
  lead_id text null references public.leads(id) on delete set null,
  direction text null check (direction in ('inbound', 'outbound')),
  remote_phone_e164 text null,
  portive_phone_e164 text null,
  call_status text null,
  call_outcome text null,
  match_status text null,
  failure_reason text null,
  sip_status_code integer null,
  sip_error_reason text null,
  sip_call_id text null,
  duration_seconds integer null check (duration_seconds is null or duration_seconds >= 0),
  elevenlabs_credits integer null check (elevenlabs_credits is null or elevenlabs_credits >= 0),
  transcript_summary text null,
  transcript jsonb null,
  analysis jsonb null,
  metadata jsonb null,
  raw_payload jsonb not null,
  event_timestamp timestamptz null,
  received_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (conversation_id, event_type)
);
create index if not exists portive_call_events_lead_received_idx on public.portive_call_events (lead_id, received_at desc);
create index if not exists portive_call_events_remote_received_idx on public.portive_call_events (remote_phone_e164, received_at desc);
create index if not exists portive_call_events_agent_received_idx on public.portive_call_events (agent_id, received_at desc);
create index if not exists portive_call_events_outcome_received_idx on public.portive_call_events (call_outcome, received_at desc);

-- Explicit review queue for requests lacking a cryptographically-bound ANI.
create table if not exists public.portive_dnc_quarantine (
  id uuid primary key default gen_random_uuid(),
  conversation_id text not null,
  reason text not null,
  created_at timestamptz not null default now(),
  resolved_at timestamptz null,
  resolution_note text null
);
create unique index if not exists portive_dnc_quarantine_conversation_open_idx
  on public.portive_dnc_quarantine(conversation_id) where resolved_at is null;

-- Suppress only an ANI recorded by the authenticated initiation endpoint. It never
-- changes a previous lead association and it never invents a lead ID.
create or replace function public.portive_record_unknown_inbound_dnc(
  p_conversation_id text, p_agent_id text, p_reason text
) returns jsonb language plpgsql security definer set search_path=public as $$
declare v_phone text;
begin
  select caller_e164 into v_phone from public.portive_inbound_contexts
    where conversation_id=p_conversation_id and agent_id=p_agent_id;
  if v_phone is null then
    insert into public.portive_dnc_quarantine(conversation_id, reason)
      values(p_conversation_id, 'unverified_inbound_opt_out')
      on conflict (conversation_id) where resolved_at is null do nothing;
    return jsonb_build_object('suppressed', false, 'code', 'context_unverified');
  end if;
  insert into public.portive_dnc(phone_e164, lead_id, conversation_id, reason)
    values(v_phone, null, p_conversation_id, coalesce(nullif(p_reason,''), 'requested_no_contact'))
    on conflict (phone_e164) do nothing;
  return jsonb_build_object('suppressed', true);
end $$;

alter table public.portive_inbound_contexts enable row level security;
alter table public.portive_call_events enable row level security;
alter table public.portive_dnc_quarantine enable row level security;
revoke all on public.portive_inbound_contexts from anon, authenticated;
revoke all on public.portive_call_events from anon, authenticated;
revoke all on public.portive_dnc_quarantine from anon, authenticated;
grant all on public.portive_inbound_contexts to service_role;
grant all on public.portive_call_events to service_role;
grant all on public.portive_dnc_quarantine to service_role;
revoke execute on function public.portive_record_unknown_inbound_dnc(text,text,text) from public, anon, authenticated;
grant execute on function public.portive_record_unknown_inbound_dnc(text,text,text) to service_role;

-- Retention is operational policy: review/delete transcripts and raw payloads under
-- your documented retention policy. Do not schedule automatic deletion here.
