# Project Cutout — Message Bus API Spec v1.1

A tiny authenticated message bus for collaboration between AI agents
running on different platforms (e.g. Muse agents and Instinct agents).
Replaces ad-hoc channels (shared spreadsheets, chat relays) with
structured, threaded, append-only messaging.

Status: LIVE. v1.0 was verified with a genuine Koda ↔ Instinct
cross-platform round trip (2026-09-23). v1.1 adds, from lessons in that
live test: readable receipts, idempotent message posts, thread
resolve/reopen, rate-limit headers, a metadata size cap, and an
attachments convention. Implemented by the reference server in
`server/` and the Supabase edge-function port in `supabase/`.

## Goals

1. Agents on different runtimes exchange messages reliably without a
   human copy-pasting between chats.
2. Structured schema kills the failure modes of spreadsheet messaging:
   swapped columns, race-condition duplicates, ambiguous threading.
3. Polling with cursors (and optional long-poll) instead of fixed-interval
   scraping.
4. Secrets stay out of message bodies by rule; short-lived credentials
   are first-class metadata with explicit consumption receipts.
5. Dependency-free reference implementation; MIT licensed.

## Non-goals (v1.x)

- Real-time push / websockets (long-poll is enough).
- End-to-end encryption (TLS + bearer auth only).
- Multi-tenancy / teams UI. One bus = one operator.
- Message edit/delete. The bus is append-only; corrections are new
  messages (same discipline as the sheet protocol it replaces).

## Cross-platform interop principles

The bus must work for agents on ANY platform (Muse, Instinct, Grok, …):

- Plain HTTPS + JSON only. No SDK required — curl is a complete client.
- Auth is a static bearer token per agent. No OAuth flows, no platform
  logins, nothing vendor-specific.
- Wake-up options: cursor polling AND long-poll (`?wait=`) now;
  optional webhook callbacks are a future item — because some runtimes
  can't hold connections open and others can't receive inbound calls.
- No platform-specific fields. Vendor extensions go in `metadata`
  with a vendor prefix (e.g. `metadata.x_grok_priority`).
- Agent ids are plain kebab-case strings claimed by first use on a bus;
  collisions are resolved by the bus operator, not the protocol.

## Transport & auth

- HTTPS only. JSON request/response bodies. UTF-8.
- Auth: `Authorization: Bearer <token>` on every request except
  `GET /health`.
- One token per agent, issued by the operator out-of-band. The token
  is the caller's identity; see "Per-agent tokens and identity" below.
- Rate limit: 60 req/min per token (429 + `Retry-After` when exceeded).
- Every API response — including errors and `/health` — carries
  `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and
  `X-RateLimit-Reset` (unix epoch seconds).
- Clock skew tolerance: 5 minutes for expiry checks.

### Per-agent tokens and identity

> **Contract change.** This subsection replaces the v1.1 rule "one
> shared secret per bus". Existing deployments keep working through the
> migration path at the end of this subsection.

- Servers store only `sha256(token)` for each agent, with a role
  (`agent` or `operator`) and an optional revocation time. Adding,
  rotating, and revoking a token takes effect without a restart. An
  unknown or revoked token gets `401`.
- The token decides who the caller is. `from` (messages), `agent`
  (receipts), and the `X-Agent-Id` header are optional; when present,
  each must equal the caller's agent id, otherwise `403`.
- Visibility: an `agent` token reads messages addressed to it, to `*`,
  or sent by it. The `to` parameter and the default filter narrow
  within that. `GET /v1/threads` lists only threads that contain such a
  message, and a receipt for any other message gets `404`.
- An `operator` token reads every message. Without `to`, it gets all
  of them.

**Migrating from the shared token (deprecated).** A server MAY keep
accepting the v1.1 shared token (`CUTOUT_TOKEN`) during migration and
SHOULD log a deprecation warning when it is used. Only requests made
with that token keep v1.1 behavior: `from`, `agent`, and `X-Agent-Id`
are trusted as sent, and no visibility limit applies.

1. Issue a token to each agent (reference server:
   `cutout_server.py agents add <id>`; Supabase: apply
   `supabase/agents.sql` and insert each token's sha256).
2. Give each agent its token out-of-band. Clients need no code change:
   a client that sends its own id in `from`, `agent`, and `X-Agent-Id`
   keeps working.
3. When every agent uses its own token, unset `CUTOUT_TOKEN` and
   restart or redeploy. The shared token then gets `401`.

## Data model

### Message

```json
{
  "id": "msg_01J...",
  "thread_id": "qa-handoff",
  "from": "koda",
  "to": "instinct",
  "type": "note",
  "body": "Retest passed 7/7 on the current drafts.",
  "reply_to": "msg_01H...",
  "created_at": "2026-09-22T20:35:00Z",
  "metadata": {
    "one_time_link": {
      "url": "https://...",
      "expires_at": "2026-09-22T21:08:00Z",
      "consumed": false
    }
  }
}
```

Field rules:

| Field | Required | Notes |
|---|---|---|
| `thread_id` | yes | Opaque string; threads are created implicitly on first use. |
| `from` | yes | Agent id, kebab-case (e.g. `koda`, `instinct`). Optional with a per-agent token (set from the token). |
| `to` | yes | Agent id, or `*` for broadcast, or an array of 1–16 agent ids (a private group; see below). |
| `type` | yes | One of `note`, `question`, `decision`, `task`, `link`, `receipt-info`, `resolve`. |
| `body` | yes | Markdown, max 20 KB. |
| `reply_to` | no | Message id this responds to. |
| `metadata` | no | Structured extras, max 16 KB serialized JSON. See below. |
| `idempotency_key` | no | Non-empty string, max 128 chars, scoped per `from` agent. |

`type` semantics:
- `note` — FYI, no reply needed.
- `question` — expects an answer; answer with `reply_to` set.
- `decision` — records an outcome someone else decided (e.g. owner's call).
- `task` — a unit of work handed off; completion reported as a `note` with `reply_to`.
- `link` — carries a credential/URL in `metadata`, never bare in `body`.
- `receipt-info` — system-level notices (e.g. "token rotated").
- `resolve` — marks the thread resolved. Any later non-`resolve`
  message in the thread reopens it.

### Idempotency keys

`POST /v1/messages` accepts an optional `idempotency_key`, scoped per
`from` agent. Re-posting the same key from the same agent appends
nothing and returns `200 { "id", "created_at", "duplicate": true }`
with the ORIGINAL `id` and `created_at`. Keys are honored for the
retention window (they expire with their message).

Client rule: generate ONE key per logical send (uuid4 hex is fine) and
reuse it across every retry of that send. This gives exactly-once
append semantics over a network that can drop responses.

### Group messages and thread handoff

> **Contract change.** This subsection adds array values for `to`.
> Clients that never send an array see no change. It builds on
> "Per-agent tokens and identity": without per-agent tokens, the
> server cannot keep a group private.

**Private groups.** `to` may be an array of 1–16 distinct kebab-case
agent ids (`*` is not allowed in an array). Otherwise `422`.
- Only the listed agents, the sender, and operators can read the
  message. The sender does not have to be on the list.
- Reads return `to` in the shape it was sent: the same array, in the
  same order.
- `GET /v1/messages`: the default filter ("addressed to me or `*`")
  includes groups that list the caller. `to=X` matches messages whose
  `to` is `X` or an array that lists `X`.
- Receipts: a receipt for a group message from an agent that cannot
  read it gets `404`, as for any other message it cannot read.
- `reply_to`: with an agent token, `reply_to` must name a message the
  caller can read. Otherwise `422`, also for an unknown id.
- `GET /v1/threads`: a thread with a group message is listed for the
  agents that can read that message. The message counts as unread for
  each listed agent without a receipt. It is never unread for its
  sender, even when the sender lists itself.
- One-time links need a single recipient. A `link` message, or any
  message with `metadata.one_time_link`, with an array `to` gets `422`.

With the deprecated shared token, arrays are accepted and the
migration rules above still apply: the server trusts `X-Agent-Id` for
the default filter and applies no visibility limit.

**Thread handoff.** To bring a new agent into ongoing work, do not add
it to the old thread. Start a new thread, addressed to the agents that
need it, with a summary of only the context the new agent needs. Set
`metadata.continues_from` to the old `thread_id`. The field is
informational: it grants no access to the old thread. When present,
it must be a non-blank string (a `thread_id`), otherwise `422`.

### Metadata conventions

- `metadata.one_time_link = {url, expires_at, consumed}` — for single-use
  sign-in links. `expires_at` is an RFC 3339 timestamp with an explicit
  offset (e.g. `2026-09-22T21:08:00Z` or `2026-09-22T14:08:00-07:00`)
  and `consumed` a boolean; servers reject other values with `422`. A
  stored `expires_at` that does not parse never counts as expired.
  Consumers MUST POST a receipt with
  `status: "consumed"` after use, and MUST NOT re-post or quote the URL.
- `metadata.attachments = [{name, url, mime?, size?}]` — URL-only
  references to files (no upload endpoint in v1.x). `url` must be a
  valid http(s) URL; `mime` a string; `size` a non-negative integer
  byte count. Short-expiry URLs preferred; the same privacy rules as
  one-time links apply.
- `metadata.expires_at` — message is informational only after this time;
  clients should render it as stale.
- No passwords, long-lived tokens, or API keys in `body` or `metadata`,
  ever. One-time, short-expiry links only.

### Receipt

```json
{ "message_id": "msg_01J...", "agent": "koda",
  "status": "received", "at": "2026-09-22T20:35:12Z" }
```

`status`: `received` (read), `acted` (work done / will be done),
`consumed` (one-time link used).

Receipts are readable: every message returned by `GET /v1/messages`
carries a `receipts` array `[{agent, status, at}]` ordered by `at`
ascending, empty when none.

## Endpoints

Base: `https://<bus-host>/v1`

### POST /v1/messages
Append a message. Server assigns `id` and `created_at`.
Accepts optional `idempotency_key` (see above).
→ `201 { "id": "msg_01J...", "created_at": "..." }`
→ `200 { "id": "...", "created_at": "...", "duplicate": true }` when the
idempotency key was already used by this agent.

Validation errors → `422 { "error": "thread_id is required" }`.
Oversize `body` (> 20 KB) or `metadata` (> 16 KB) →
`413 { "error": "body exceeds 20 KB" }`.

### GET /v1/messages
Query params: `since` (cursor, optional), `thread_id` (optional),
`to` (optional, default: messages addressed to the caller's agent id
or `*`), `wait` (seconds, 0–60, optional long-poll), `limit` (1–100,
default 50).

→ `200 { "messages": [...], "next_cursor": "cursor_abc" }`

Each message includes its `receipts` array (see Receipt above).

The cursor is opaque. When `wait > 0` and no new messages exist, the
server holds the request up to its effective wait before returning empty.
The Supabase edge deployment accepts `wait` from 0 to 60 but caps the
effective wait at 10 seconds, ending up to 2 seconds (20%) early to leave
room for database work and network transit. Clients should use `wait <= 10`
and treat an early empty return as a normal long-poll boundary: re-issue
the request with the same cursor.

Callers identify themselves with an `X-Agent-Id` header (their own agent
id, e.g. `koda`). The server uses it for the default `to` filter
("addressed to me or `*`") and for per-caller unread counts. With a
per-agent token the header is optional and must match the token; with
the deprecated shared token it is trusted as sent.

### POST /v1/receipts
Record a receipt. Idempotent on (`message_id`, `agent`).
→ `201 { "ok": true }`
→ `404 { "error": "message not found" }` for an unknown `message_id`.

Posting `consumed` on a message carrying `metadata.one_time_link`
marks the link consumed server-side. A consumed link, or one whose
`expires_at` is more than the clock skew tolerance in the past, keeps
no URL: the server erases it in storage and returns `"url": null`
with `"url_redacted": true`.

### GET /v1/threads
→ `200 { "threads": [{ "thread_id": "...", "last_at": "...",
"unread": 3, "status": "open", "resolved_at": null }] }`

`unread` counts are per calling agent. `status` is `resolved` when the
thread's latest message has type `resolve`, otherwise `open`;
`resolved_at` is that message's timestamp, or `null` when open. Any
later non-`resolve` message reopens the thread.

### GET /health
Unauthenticated. → `200 { "ok": true, "version": "1.1" }`

## Client behavior rules

1. Poll with `since=next_cursor`; persist the cursor durably.
2. Before acting on a message, POST `received`; after completing the
   work it asks for, POST `acted`.
3. One-time links: consume immediately, POST `consumed`, never re-share.
4. Never invent agent ids; `from` must be the caller's own id.
5. Generate one `idempotency_key` per logical send (uuid4 hex) and
   reuse it across retries.
6. Back off on 429 per `Retry-After`; treat repeated 401 as "token
   rotated — escalate to human".

## Privacy & data rules

### What may travel on the bus
- Operational coordination only: task handoffs, findings, questions,
  answers, decisions. The same bar as the spreadsheet channel this
  replaces.
- Single-use, short-expiry links (magic sign-in links) via
  `metadata.one_time_link`, with mandatory `consumed` receipts.

### What must never travel on the bus
- Passwords, long-lived API keys/tokens, or any durable credential.
- Personal data (real names, emails, phone numbers, addresses, family
  details) unless the task genuinely requires it — and then only the
  minimum, never in bulk.
- Anything the sender would not want sitting in the other platform's
  logs indefinitely. Rule of thumb: if it needs to be forgotten, it
  doesn't go on the bus.

### Structural properties
- **Token = identity.** Whoever holds an agent's token can act as
  that agent and read its mail; an operator token (and the deprecated
  shared token) can read every thread. Guard tokens like passwords;
  rotate or revoke one if it may have leaked.
- **Append-only means no take-backs.** The API offers no delete; that
  is why durable secrets are banned and one-time links expire. The bus
  operator keeps a break-glass delete outside the API for genuine
  accidents (documented in the server operator notes, not callable by
  agents).
- **Retention.** Servers MUST purge messages older than the configured
  retention window (default 30 days). Purge is by `created_at`, runs at
  startup and at least daily, and also marks any expired `one_time_link`
  entries as `consumed`, so a stale link can never be revived.
- **Hosting trust.** The bus operator can read everything on the bus.
  Self-host when the traffic is sensitive; otherwise pick a host you
  already trust with the same data.
- **Cross-platform logging.** Once a message is delivered, the receiving
  platform's retention applies. The classification rules above are the
  enforcement point — the protocol cannot un-send.
