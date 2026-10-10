-- ADDITIVE migration tailored to ColdCallerCRM's CURRENT public.leads (id TEXT)
-- and existing public.follow_ups (lead_id TEXT). Does NOT recreate leads.
-- REVIEW / backup before running on production. Run once via Supabase SQL Editor.

create table if not exists public.portive_dnc (
    phone_e164 text primary key,
    lead_id text references public.leads(id) on delete set null,
    conversation_id text,
    reason text not null default 'requested_no_contact',
    created_at timestamptz not null default now()
);
create index if not exists portive_dnc_lead_idx on public.portive_dnc(lead_id);

alter table public.follow_ups add column if not exists source_conversation_id text;
alter table public.follow_ups add column if not exists contact_name text;
alter table public.follow_ups add column if not exists prospect_timezone text;
alter table public.follow_ups add column if not exists source text not null default 'manual';
create unique index if not exists follow_ups_lead_conversation_unique
    on public.follow_ups(lead_id, source_conversation_id);

create table if not exists public.portive_overview_emails (
    id uuid primary key default gen_random_uuid(),
    lead_id text references public.leads(id) on delete set null,
    conversation_id text not null,
    recipient_email text not null,
    recipient_name text,
    request_key text not null unique,
    status text not null default 'pending'
      check (status in ('pending','accepted','failed','uncertain')),
    provider_email_id text,
    last_error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists portive_overview_email_lead_idx on public.portive_overview_emails(lead_id);

create table if not exists public.portive_email_suppressions (
    email text primary key,
    reason text not null default 'unsubscribe_link',
    created_at timestamptz not null default now()
);

-- Only a backend service key (bypasses RLS) may access these tables.
alter table public.portive_dnc enable row level security;
alter table public.portive_overview_emails enable row level security;
alter table public.portive_email_suppressions enable row level security;
revoke all on public.portive_dnc from anon, authenticated;
revoke all on public.portive_overview_emails from anon, authenticated;
revoke all on public.portive_email_suppressions from anon, authenticated;
grant all on public.portive_dnc to service_role;
grant all on public.portive_overview_emails to service_role;
grant all on public.portive_email_suppressions to service_role;

-- One round-trip: suppress number and cancel the selected lead's pending callbacks.
create or replace function public.portive_record_dnc(
    p_lead_id text, p_conversation_id text, p_phone_e164 text, p_reason text
) returns jsonb
language plpgsql security definer set search_path=public as $$
declare v_cancelled integer := 0;
begin
    if not exists(select 1 from public.leads where id = p_lead_id) then
       return jsonb_build_object('suppressed',false,'code','lead_missing');
    end if;
    insert into public.portive_dnc(phone_e164,lead_id,conversation_id,reason)
        values(p_phone_e164,p_lead_id,p_conversation_id,p_reason)
        on conflict (phone_e164) do nothing;
    update public.follow_ups set status='canceled',updated_at=now()
       where lead_id=p_lead_id and status='open';
    get diagnostics v_cancelled = row_count;
    return jsonb_build_object('suppressed',true,'callbacks_canceled_for_lead',v_cancelled);
end $$;

-- One transaction: check suppression, record callback exactly once, mark lead for follow-up.
create or replace function public.portive_save_callback(
    p_lead_id text, p_phone_e164 text, p_conversation_id text,
    p_contact_name text, p_callback_at timestamptz, p_timezone text, p_note text
) returns jsonb
language plpgsql security definer set search_path=public as $$
declare v_id text; v_due timestamptz; v_existing boolean := false;
begin
    perform id from public.leads where id=p_lead_id for update;
    if not found then return jsonb_build_object('saved',false,'code','lead_missing'); end if;
    if exists(select 1 from public.portive_dnc where phone_e164=p_phone_e164) then
        return jsonb_build_object('saved',false,'code','phone_suppressed');
    end if;
    insert into public.follow_ups(
       lead_id,due_at,status,channel,note,source_conversation_id,contact_name,prospect_timezone,source
    ) values (
       p_lead_id,p_callback_at,'open','call',p_note,p_conversation_id,p_contact_name,p_timezone,'elevenlabs'
    )
    on conflict (lead_id,source_conversation_id) do nothing
    returning id,due_at into v_id,v_due;
    if v_id is null then
      v_existing := true;
      select id,due_at into v_id,v_due from public.follow_ups
       where lead_id=p_lead_id and source_conversation_id=p_conversation_id;
      if v_due is distinct from p_callback_at then
        return jsonb_build_object('saved',false,'code','callback_already_saved_different_time');
      end if;
    end if;
    update public.leads set derived_status='follow_up',updated_at=now() where id=p_lead_id;
    return jsonb_build_object('saved',true,'callback_id',v_id,
                              'callback_at_utc',to_char(v_due at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                              'already_existed',v_existing);
end $$;

revoke execute on function public.portive_record_dnc(text,text,text,text) from public,anon,authenticated;
revoke execute on function public.portive_save_callback(text,text,text,timestamptz,text,text,text) from public,anon,authenticated;
grant execute on function public.portive_record_dnc(text,text,text,text) to service_role;
grant execute on function public.portive_save_callback(text,text,text,timestamptz,text,text,text) to service_role;
