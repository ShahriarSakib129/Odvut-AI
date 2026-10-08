-- ============================================================================
--  INFO GROUP AI BOT - PostgreSQL / Supabase schema
-- ----------------------------------------------------------------------------
--  How to run:
--    1) Supabase dashboard -> SQL Editor -> paste this whole file -> Run
--       (or)  python scripts/apply_schema.py --url "$DATABASE_URL"
--    2) The script is idempotent: running it twice is safe.
--
--  Notes:
--    * Field name mapping vs. the original specification:
--        admin_messages.timestamp  -> admin_messages.message_timestamp
--        qa_memory.timestamp       -> qa_memory.answer_timestamp (+ question_timestamp)
--      Both are kept because the retrieval engine needs to sort question and
--      answer separately (reply latency, recency weighting).
--    * Full-text search uses the 'simple' configuration on purpose: PostgreSQL
--      ships no Bangla stemmer, and 'simple' keeps Bangla/Banglish tokens intact.
--    * pg_trgm is optional. If the extension cannot be created (permissions),
--      the schema still succeeds and the application falls back to ILIKE search.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 0. extensions (best effort)
-- ---------------------------------------------------------------------------
do $$
begin
    create extension if not exists pg_trgm;
exception when others then
    raise notice 'pg_trgm not available (%), fuzzy search will use fallback', sqlerrm;
end $$;

-- ---------------------------------------------------------------------------
-- 1. helper functions
-- ---------------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end $$;

-- ---------------------------------------------------------------------------
-- 2. bot_settings : runtime key/value settings managed by bot admins
-- ---------------------------------------------------------------------------
create table if not exists public.bot_settings (
    key         text primary key,
    value       text not null,
    value_type  text not null default 'string'
                check (value_type in ('string', 'int', 'float', 'bool', 'json')),
    description text,
    updated_by  bigint,
    updated_at  timestamptz not null default now()
);

comment on table public.bot_settings is
    'Runtime switches (read on every request, cached in memory for a short TTL).';

-- ---------------------------------------------------------------------------
-- 3. bot_users
-- ---------------------------------------------------------------------------
create table if not exists public.bot_users (
    id                bigserial primary key,
    telegram_user_id  bigint not null unique,
    username          text,
    first_name        text,
    last_name         text,
    language_code     text,
    is_bot            boolean not null default false,
    is_target_admin   boolean not null default false,
    first_seen        timestamptz not null default now(),
    last_seen         timestamptz not null default now(),
    message_count     integer not null default 0,
    question_count    integer not null default 0,
    ai_answer_count   integer not null default 0,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

create index if not exists idx_bot_users_last_seen on public.bot_users (last_seen desc);
create index if not exists idx_bot_users_username  on public.bot_users (lower(username));

drop trigger if exists trg_bot_users_updated_at on public.bot_users;
create trigger trg_bot_users_updated_at
    before update on public.bot_users
    for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- 4. admin_messages : everything the TARGET admin writes
-- ---------------------------------------------------------------------------
create table if not exists public.admin_messages (
    id                   bigserial primary key,
    telegram_message_id  bigint not null,
    chat_id              bigint not null,
    admin_id             bigint not null,
    admin_username       text,
    message_text         text not null,
    normalized_text      text not null default '',
    message_type         text not null default 'text',
    reply_to_message_id  bigint,
    topic                text not null default 'general',
    keywords             text[] not null default '{}',
    crypto_terms         text[] not null default '{}',
    language             text not null default 'unknown',
    is_answer            boolean not null default false,
    message_timestamp    timestamptz not null,
    embedding            real[],
    search_vector        tsvector generated always as
                         (to_tsvector('simple'::regconfig, coalesce(message_text, ''))) stored,
    created_at           timestamptz not null default now(),
    constraint uq_admin_messages_chat_message unique (chat_id, telegram_message_id)
);

comment on table public.admin_messages is
    'Memory of the target admin: opinions, explanations, market calls, replies.';

create index if not exists idx_admin_messages_admin_time
    on public.admin_messages (admin_id, message_timestamp desc);
create index if not exists idx_admin_messages_chat_time
    on public.admin_messages (chat_id, message_timestamp desc);
create index if not exists idx_admin_messages_search
    on public.admin_messages using gin (search_vector);
create index if not exists idx_admin_messages_keywords
    on public.admin_messages using gin (keywords);
create index if not exists idx_admin_messages_crypto
    on public.admin_messages using gin (crypto_terms);
create index if not exists idx_admin_messages_topic
    on public.admin_messages (topic);

-- ---------------------------------------------------------------------------
-- 5. message_logs : lightweight analytics (NOT AI memory)
-- ---------------------------------------------------------------------------
create table if not exists public.message_logs (
    id                   bigserial primary key,
    chat_id              bigint not null,
    telegram_message_id  bigint not null,
    user_id              bigint not null,
    username             text,
    is_target_admin      boolean not null default false,
    is_bot               boolean not null default false,
    message_type         text not null default 'text',
    message_text         text,
    text_length          integer not null default 0,
    is_question          boolean not null default false,
    answered_by_admin    boolean not null default false,
    purpose              text not null default 'analytics'
                         check (purpose in ('analytics', 'unanswered_question')),
    topic                text not null default 'unknown',
    reply_to_message_id  bigint,
    message_timestamp    timestamptz not null,
    created_at           timestamptz not null default now(),
    constraint uq_message_logs_chat_message unique (chat_id, telegram_message_id)
);

comment on table public.message_logs is
    'Truncated message log for statistics and unanswered-question analytics.
     Never used as AI context.';

create index if not exists idx_message_logs_chat_time
    on public.message_logs (chat_id, message_timestamp desc);
create index if not exists idx_message_logs_user on public.message_logs (user_id);
create index if not exists idx_message_logs_unanswered
    on public.message_logs (is_question, answered_by_admin)
    where is_question = true;

-- ---------------------------------------------------------------------------
-- 6. pending_questions : member questions waiting for an admin answer
-- ---------------------------------------------------------------------------
create table if not exists public.pending_questions (
    id                   bigserial primary key,
    chat_id              bigint not null,
    telegram_message_id  bigint not null,
    member_user_id       bigint not null,
    member_username      text,
    question_text        text not null,
    normalized_text      text not null default '',
    keywords             text[] not null default '{}',
    crypto_terms         text[] not null default '{}',
    topic                text not null default 'general',
    is_question          boolean not null default true,
    paired               boolean not null default false,
    paired_qa_id         bigint,
    message_timestamp    timestamptz not null,
    expires_at           timestamptz not null,
    created_at           timestamptz not null default now(),
    constraint uq_pending_questions_chat_message unique (chat_id, telegram_message_id)
);

comment on table public.pending_questions is
    'Short-lived questions. When the target admin answers, the row is converted
     into a qa_memory row, otherwise it expires (and never becomes AI memory).';

create index if not exists idx_pending_questions_open
    on public.pending_questions (chat_id, paired, message_timestamp desc);
create index if not exists idx_pending_questions_expiry on public.pending_questions (expires_at);

-- ---------------------------------------------------------------------------
-- 7. qa_memory : member question + target admin answer (the core feature)
-- ---------------------------------------------------------------------------
create table if not exists public.qa_memory (
    id                   bigserial primary key,
    chat_id              bigint not null,
    member_user_id       bigint not null,
    member_username      text,
    question_message_id  bigint not null,
    question_text        text not null,
    normalized_question  text not null default '',
    admin_message_id     bigint not null,
    admin_id             bigint not null,
    admin_answer_text    text not null,
    normalized_answer    text not null default '',
    pair_method          text not null default 'reply'
                         check (pair_method in ('reply', 'window', 'manual')),
    pair_confidence      real not null default 0.90,
    is_question          boolean not null default true,
    topic                text not null default 'general',
    keywords             text[] not null default '{}',
    crypto_terms         text[] not null default '{}',
    language             text not null default 'unknown',
    question_timestamp   timestamptz not null,
    answer_timestamp     timestamptz not null,
    embedding            real[],
    question_search      tsvector generated always as
                         (to_tsvector('simple'::regconfig,
                                      coalesce(question_text, '') || ' ' ||
                                      coalesce(admin_answer_text, ''))) stored,
    usage_count          integer not null default 0,
    last_used_at         timestamptz,
    created_at           timestamptz not null default now(),
    -- convenience alias so the documented "timestamp" column always works
    "timestamp"          timestamptz generated always as (answer_timestamp) stored,
    constraint uq_qa_memory_question unique (chat_id, question_message_id),
    constraint uq_qa_memory_answer unique (chat_id, admin_message_id)
);

-- idempotent migration for databases created before the alias was added
do $$
begin
    if not exists (select 1 from information_schema.columns
                   where table_schema = 'public' and table_name = 'qa_memory'
                     and column_name = 'timestamp') then
        execute 'alter table public.qa_memory add column "timestamp" timestamptz '
                'generated always as (answer_timestamp) stored';
    end if;
end $$;

comment on table public.qa_memory is
    'Verified question/answer pairs: a member question that the TARGET admin
     actually answered. Unanswered questions never land here.';

create index if not exists idx_qa_memory_search on public.qa_memory using gin (question_search);
create index if not exists idx_qa_memory_time on public.qa_memory (answer_timestamp desc);
create index if not exists idx_qa_memory_topic on public.qa_memory (topic);
create index if not exists idx_qa_memory_keywords on public.qa_memory using gin (keywords);

-- ---------------------------------------------------------------------------
-- 8. conversation_sessions : short-term context for follow-up questions
-- ---------------------------------------------------------------------------
create table if not exists public.conversation_sessions (
    id           bigserial primary key,
    chat_id      bigint not null,
    user_id      bigint not null,
    history      jsonb not null default '[]'::jsonb,
    turn_count   integer not null default 0,
    last_question text,
    last_answer   text,
    expires_at   timestamptz not null,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    constraint uq_conversation_sessions_chat_user unique (chat_id, user_id)
);

create index if not exists idx_conversation_sessions_expiry on public.conversation_sessions (expires_at);

drop trigger if exists trg_conversation_sessions_updated_at on public.conversation_sessions;
create trigger trg_conversation_sessions_updated_at
    before update on public.conversation_sessions
    for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- 9. response_cache : identical questions answered cheaply (cost optimisation)
-- ---------------------------------------------------------------------------
create table if not exists public.response_cache (
    id           bigserial primary key,
    cache_key    text not null unique,
    chat_id      bigint,
    question_text text not null,
    answer_text  text not null,
    model        text,
    used_memory  boolean not null default false,
    hits         integer not null default 0,
    created_at   timestamptz not null default now(),
    expires_at   timestamptz not null
);

create index if not exists idx_response_cache_expiry on public.response_cache (expires_at);

-- ---------------------------------------------------------------------------
-- 10. ai_usage_log : token usage / errors / cost control
-- ---------------------------------------------------------------------------
create table if not exists public.ai_usage_log (
    id                bigserial primary key,
    chat_id           bigint,
    user_id           bigint,
    model             text,
    status            text not null default 'ok',
    error_type        text,
    cached            boolean not null default false,
    used_memory       boolean not null default false,
    prompt_tokens     integer,
    completion_tokens integer,
    total_tokens      integer,
    latency_ms        integer,
    memory_count      integer,
    qa_count          integer,
    question_hash     text,
    created_at        timestamptz not null default now()
);

create index if not exists idx_ai_usage_created on public.ai_usage_log (created_at desc);
create index if not exists idx_ai_usage_status on public.ai_usage_log (status, created_at desc);

-- ---------------------------------------------------------------------------
-- 11. optional trigram indexes (only when pg_trgm exists)
-- ---------------------------------------------------------------------------
do $$
begin
    if exists (select 1 from pg_extension where extname = 'pg_trgm') then
        execute 'create index if not exists idx_admin_messages_text_trgm
                 on public.admin_messages using gin (message_text gin_trgm_ops)';
        execute 'create index if not exists idx_qa_memory_question_trgm
                 on public.qa_memory using gin (question_text gin_trgm_ops)';
        execute 'create index if not exists idx_qa_memory_answer_trgm
                 on public.qa_memory using gin (admin_answer_text gin_trgm_ops)';
    else
        raise notice 'pg_trgm missing: fuzzy-search indexes skipped';
    end if;
end $$;

-- ---------------------------------------------------------------------------
-- 12. reporting views
-- ---------------------------------------------------------------------------
create or replace view public.v_memory_stats as
select
    (select count(*) from public.admin_messages)                                  as admin_messages,
    (select count(*) from public.qa_memory)                                       as qa_pairs,
    (select count(*) from public.bot_users)                                       as users,
    (select count(*) from public.pending_questions where paired = false)          as open_questions,
    (select count(*) from public.message_logs where answered_by_admin = false
        and is_question = true)                                                   as unanswered_questions,
    (select max(message_timestamp) from public.admin_messages)                    as latest_admin_message,
    (select max(answer_timestamp) from public.qa_memory)                          as latest_qa_pair,
    (select count(*) from public.ai_usage_log
        where created_at > now() - interval '24 hours')                           as ai_calls_24h,
    (select coalesce(sum(total_tokens), 0) from public.ai_usage_log
        where created_at > now() - interval '24 hours')                           as tokens_24h,
    now()                                                                         as generated_at;

create or replace view public.v_topic_stats as
select topic, count(*) as total, max(answer_timestamp) as latest
from public.qa_memory
group by topic
order by total desc;

create or replace view public.v_top_questions as
select qa.question_text,
       qa.usage_count,
       qa.topic,
       qa.answer_timestamp
from public.qa_memory qa
order by qa.usage_count desc, qa.answer_timestamp desc
limit 20;

-- ---------------------------------------------------------------------------
-- 13. default runtime settings (safe to re-run)
-- ---------------------------------------------------------------------------
insert into public.bot_settings (key, value, value_type, description) values
    ('ai_enabled',          'true',      'bool',   'Master switch for AI answers'),
    ('auto_reply_enabled',  'true',      'bool',   'Answer questions in the group (trigger based)'),
    ('memory_enabled',      'true',      'bool',   'Collect target-admin memory'),
    ('qa_pairing_enabled',  'true',      'bool',   'Pair member questions with admin answers'),
    ('reply_footer_enabled','true',      'bool',   'Append the AI disclosure footer in groups'),
    ('max_memory_results_override', '',  'int',    'Optional runtime override for MAX_MEMORY_RESULTS')
on conflict (key) do nothing;

-- ---------------------------------------------------------------------------
-- 14. permission helpers (documentation only, safe to re-run)
-- ---------------------------------------------------------------------------
-- Supabase exposes these tables through the PostgREST API. The bot connects
-- with the direct PostgreSQL connection string (the 'postgres' role), so RLS
-- is bypassed for the bot while still protecting data from anonymous API users.
do $$
begin
    if exists (select 1 from information_schema.tables
               where table_schema = 'public' and table_name = 'admin_messages') then
        execute 'alter table public.admin_messages        enable row level security';
        execute 'alter table public.qa_memory             enable row level security';
        execute 'alter table public.pending_questions     enable row level security';
        execute 'alter table public.message_logs          enable row level security';
        execute 'alter table public.conversation_sessions enable row level security';
        execute 'alter table public.ai_usage_log          enable row level security';
        execute 'alter table public.response_cache        enable row level security';
    end if;
end $$;

-- NOTE: no permissive policies are created on purpose. Only the service
-- connection (the bot) can read/write this data.

-- ---------------------------------------------------------------------------
-- 15. maintenance helper (called by scripts/maintenance.py and the app)
-- ---------------------------------------------------------------------------
create or replace function public.bot_run_maintenance(
    p_log_retention_days integer default 30,
    p_cache_grace_minutes integer default 60
) returns table (expired_questions integer, purged_logs integer, purged_cache integer)
language plpgsql
as $$
declare
    v_questions integer := 0;
    v_logs      integer := 0;
    v_cache     integer := 0;
begin
    update public.pending_questions
       set paired = true
     where paired = false and expires_at < now();
    get diagnostics v_questions = row_count;

    delete from public.message_logs
     where message_timestamp < now() - make_interval(days => p_log_retention_days);
    get diagnostics v_logs = row_count;

    delete from public.response_cache
     where expires_at < now() - make_interval(mins => p_cache_grace_minutes);
    get diagnostics v_cache = row_count;

    delete from public.conversation_sessions where expires_at < now();

    return query select v_questions, v_logs, v_cache;
end $$;

-- ============================================================================
--  End of schema
-- ============================================================================
