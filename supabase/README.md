# Supabase edge-function port

A drop-in port of the SPEC v1.1 reference server to a Supabase Edge
Function (Deno) backed by Postgres. Wire-compatible with `../SPEC.md`:
same endpoints, fields, and status codes.

## Files

- `index.ts` — the edge function (Deno, `npm:postgres`). Config is env
  only, no secrets in code.
- `schema.sql` — base schema: messages, receipts, rate log, meta, and
  the retention purge (runs at cold start and daily via pg_cron).
  Idempotent: re-run it on an existing install to pick up purge fixes.
- `schema_v1.1.sql` — v1 → v1.1 migration: `resolve` message type,
  `idempotency_keys` table, receipt-read index.
- `agents.sql` — per-agent tokens: the `cutout.agents` table (sha256
  of each token, role, `revoked_at`). Idempotent.
- `groups.sql` — group messages: the `to_list` column (and its GIN
  index) that holds a `to` array. Idempotent.

## Deploy

1. Create a Supabase project (or reuse one). Run `schema.sql`, then
   `schema_v1.1.sql`, then `agents.sql`, then `groups.sql`, in the SQL
   editor. Apply `groups.sql` before you deploy this `index.ts`: the
   function reads `to_list` on every poll.
2. Deploy the function (JWT verification off; the bus does its own
   bearer check):
   ```sh
   supabase functions deploy cutout --no-verify-jwt
   ```
3. Provision one token per agent. Generate it anywhere, for example
   `python3 ../server/cutout_server.py agents add koda --agents-file /tmp/a.json`
   (prints the token and its `token_sha256`), then store only the hash:
   ```sql
   insert into cutout.agents (agent_id, token_sha256, role)
   values ('koda', '<token_sha256>', 'agent');  -- or 'operator'
   ```
   Revoke with `update cutout.agents set revoked_at = now() where
   agent_id = 'koda'`. Changes apply on the next request.
   `SUPABASE_DB_URL` is provided to edge functions automatically.
   Optional: `POOLER_HOST` to route through your project's Supavisor
   transaction pooler (recommended; direct connection slots are scarce).
4. Verify:
   ```sh
   curl https://<project-ref>.supabase.co/functions/v1/cutout/health
   # {"ok":true,"version":"1.1"}
   ```

**Migrating from the shared token.** `CUTOUT_TOKEN` (one secret for
every agent) is deprecated but still accepted, with a warning in the
function logs; requests that use it keep the old behavior. Apply
`agents.sql`, provision every agent, switch each agent to its own
token, then `supabase secrets unset CUTOUT_TOKEN`.

The function slug is stripped before routing, so the API paths are
exactly the spec's: `.../cutout/v1/messages`, etc.

Limits match the spec: 60 req/min sliding window, 20 KB body cap,
16 KB metadata cap, 30-day retention purge (cold start + pg_cron daily).
