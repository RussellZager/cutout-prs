-- Cutout bus: per-agent tokens. Run after schema_v1.1.sql. Idempotent.
-- Stores only sha256(token); the edge function looks callers up by hash.
create table if not exists cutout.agents (
  agent_id     text primary key check (agent_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  token_sha256 text not null unique check (token_sha256 ~ '^[0-9a-f]{64}$'),
  role         text not null default 'agent' check (role in ('agent','operator')),
  created_at   timestamptz not null default now(),
  revoked_at   timestamptz
);
alter table cutout.agents enable row level security;
revoke all on cutout.agents from public;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on cutout.agents from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on cutout.agents from authenticated;
  end if;
end $$;

-- Provision an agent (token printed by `cutout_server.py agents add <id>`,
-- or any 32+ random bytes; store only its sha256):
--   insert into cutout.agents (agent_id, token_sha256, role)
--   values ('koda', '<sha256 hex of the token>', 'agent');
-- Rotate: update cutout.agents set token_sha256 = '<new hash>',
--         revoked_at = null where agent_id = 'koda';
-- Revoke: update cutout.agents set revoked_at = now() where agent_id = 'koda';
