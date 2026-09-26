
#!/usr/bin/env python3
"""
Project Cutout — reference server (API v1.1).

Dependency-free: Python standard library only (http.server, sqlite3).
Implements every endpoint in ../SPEC.md:

    POST /v1/messages    append a message (idempotency_key supported)
                         -> 201 {id, created_at}
                         -> 200 {id, created_at, duplicate: true} on replay
    GET  /v1/messages    poll with cursors; messages carry `receipts`
                         -> 200 {messages, next_cursor}
    POST /v1/receipts    record a receipt          -> 201 {ok: true}
    GET  /v1/threads     thread list with unread + resolve status
                         -> 200 {threads: [...]}
    GET  /health         unauthenticated           -> 200 {ok: true, version}

Auth:  Authorization: Bearer <token> on everything but /health.
Each agent has its own token; the agents file (default ./agents.json)
stores only sha256(token) and a role, and is re-read when it changes.
The token decides who the caller is. The shared CUTOUT_TOKEN still
works but is deprecated (see SPEC.md, "Authentication and identity").

Run:
    python3 cutout_server.py agents add koda     # prints koda's token once
    python3 cutout_server.py agents add ops --role operator
    python3 cutout_server.py agents list | rotate <id> | revoke <id>
    python3 cutout_server.py --host 127.0.0.1 --port 8765 --db ./cutout.db

This reference server speaks plain HTTP. In production, terminate TLS
in front of it (reverse proxy) and never expose it without HTTPS.
"""

import argparse
import base64
import binascii
import collections
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VERSION = "1.1"

MESSAGE_TYPES = {"note", "question", "decision", "task", "link",
                 "receipt-info", "resolve"}
RECEIPT_STATUSES = {"received", "acted", "consumed"}
AGENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
BODY_MAX_BYTES = 20 * 1024      # spec: body max 20 KB
METADATA_MAX_BYTES = 16 * 1024  # spec: metadata max 16 KB serialized
IDEMPOTENCY_KEY_MAX = 128       # spec: idempotency_key max 128 chars
RATE_LIMIT = 60             # requests (default; --rate-limit) ...
RATE_WINDOW = 60.0          # ... per 60 seconds, per token
LONG_POLL_MAX = 60
CLOCK_SKEW = timedelta(minutes=5)  # spec: skew tolerance for expiry checks

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT NOT NULL UNIQUE,
    thread_id  TEXT NOT NULL,
    sender     TEXT NOT NULL,
    recipient  TEXT NOT NULL,
    type       TEXT NOT NULL,
    body       TEXT NOT NULL,
    reply_to   TEXT,
    metadata   TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient, seq);
CREATE TABLE IF NOT EXISTS receipts (
    message_id TEXT NOT NULL,
    agent      TEXT NOT NULL,
    status     TEXT NOT NULL,
    at         TEXT NOT NULL,
    PRIMARY KEY (message_id, agent)
);
CREATE TABLE IF NOT EXISTS thread_status (
    thread_id   TEXT PRIMARY KEY,
    status      TEXT NOT NULL,          -- 'open' | 'resolved'
    resolved_at TEXT,
    resolved_by TEXT
);
"""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def utcnow():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix="msg_"):
    """ULID-style id: 48-bit ms timestamp + 80-bit randomness, Crockford32."""
    ts = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    n = (ts << 80) | rand
    return prefix + "".join(
        _CROCKFORD[(n >> (5 * i)) & 31] for i in range(25, -1, -1)
    )


def encode_cursor(seq):
    raw = str(int(seq)).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cur):
    """Return int seq, or None when the cursor is malformed."""
    try:
        padded = cur + "=" * (-len(cur) % 4)
        return int(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


def parse_timestamp(value):
    """RFC 3339 string -> aware datetime, or None if unparseable.
    Honors any UTC offset ('Z', '+00:00', '-07:00'); a timestamp with
    no offset is read as UTC."""
    if not isinstance(value, str):
        return None
    try:
        # fromisoformat only accepts a trailing 'Z' on Python 3.11+
        v = re.sub(r"[Zz]$", "+00:00", value.strip())
        # fromisoformat on Python <= 3.10 accepts exactly 3 or 6 fractional
        # digits; RFC 3339 allows 1 or more. Normalize to 6 (microseconds)
        # so every RFC 3339 value parses on every supported Python.
        v = re.sub(r"\.(\d+)(?=[+-]\d{2}:\d{2}$|$)",
                   lambda m: "." + m.group(1)[:6].ljust(6, "0"), v)
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def link_expired(link, now):
    """True when a one_time_link's expires_at is more than CLOCK_SKEW
    before `now` (an aware datetime). An unparseable expiry never counts
    as expired: erasing a URL cannot be undone."""
    exp = parse_timestamp(link.get("expires_at"))
    return exp is not None and exp < now - CLOCK_SKEW


def redact_link(link):
    """A used or expired one-time link keeps no URL (spec: never
    re-share). Marks it consumed and erases the URL in place."""
    link["consumed"] = True
    link["url"] = None
    link["url_redacted"] = True

# Shape a new one_time_link.expires_at must have: RFC 3339 date-time with
# an explicit offset. Same pattern as the edge function, so both servers
# accept the same values and Postgres can cast every one of them.
EXPIRES_AT_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?"
    r"(Z|[+-](0\d|1[0-4]):[0-5]\d)", re.IGNORECASE)


def valid_expires_at(value):
    """expires_at accepted on write: the RFC 3339 shape above, read by
    parse_timestamp() like every other expiry check (purge included)."""
    return isinstance(value, str) and bool(EXPIRES_AT_RE.fullmatch(value)) \
        and parse_timestamp(value) is not None


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# per-agent tokens
# --------------------------------------------------------------------------

ROLES = ("agent", "operator")


class AgentRegistry:
    """Agents file: {"<agent_id>": {"token_sha256", "role", "revoked_at"}}.

    Only token hashes are stored. The file is re-read whenever it changes,
    so add / rotate / revoke take effect without a restart. A missing file
    means no per-agent tokens; an unreadable one keeps the last good copy.
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._stamp = None
        self._by_hash = {}

    def _load(self):
        with open(self.path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            raise ValueError("agents file must be a JSON object")
        table = {}
        for agent_id, e in doc.items():
            if not AGENT_RE.match(agent_id) or not isinstance(e, dict) \
                    or not isinstance(e.get("token_sha256"), str) \
                    or e.get("role", "agent") not in ROLES:
                raise ValueError("bad entry for agent %r" % agent_id)
            if not e.get("revoked_at"):
                table[e["token_sha256"]] = (agent_id, e.get("role", "agent"))
        return table

    def lookup(self, token):
        """Return (agent_id, role) for a live token, else None."""
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            stamp = None
        with self._lock:
            if stamp != self._stamp:
                self._stamp = stamp
                try:
                    self._by_hash = self._load() if stamp else {}
                except (OSError, ValueError) as exc:
                    sys.stderr.write("cutout: agents file not reloaded,"
                                     " keeping previous: %s\n" % exc)
            return self._by_hash.get(sha256_hex(token))


def agents_cli(argv):
    """`cutout_server.py agents ...`: manage per-agent tokens."""
    ap = argparse.ArgumentParser(
        prog="cutout_server.py agents",
        description="Manage per-agent tokens. A new token is printed once;"
                    " only its sha256 is stored.")
    ap.add_argument("action", choices=["add", "rotate", "revoke", "list"])
    ap.add_argument("agent_id", nargs="?")
    ap.add_argument("--role", choices=ROLES,
                    help="add: default agent; rotate: keeps the old role")
    ap.add_argument("--agents-file",
                    default=os.environ.get("CUTOUT_AGENTS_FILE",
                                           "agents.json"))
    args = ap.parse_args(argv)
    path = args.agents_file
    try:
        with open(path, encoding="utf-8") as fh:
            agents = json.load(fh)
    except FileNotFoundError:
        agents = {}
    if args.action == "list":
        print(json.dumps([{"agent_id": a, "role": e.get("role", "agent"),
                           "revoked_at": e.get("revoked_at")}
                          for a, e in sorted(agents.items())], indent=2))
        return 0
    aid = args.agent_id
    if not aid or not AGENT_RE.match(aid):
        ap.error("agent_id must be a kebab-case agent id")
    if args.action == "add" and aid in agents:
        ap.error("%s already exists; use rotate" % aid)
    if args.action != "add" and aid not in agents:
        ap.error("no such agent: %s" % aid)
    if args.action == "revoke":
        agents[aid]["revoked_at"] = utcnow()
        out = {"agent_id": aid, "revoked_at": agents[aid]["revoked_at"]}
    else:  # add, rotate (rotate also clears a revocation)
        token = secrets.token_hex(32)
        role = args.role or agents.get(aid, {}).get("role", "agent")
        agents[aid] = {"token_sha256": sha256_hex(token), "role": role,
                       "revoked_at": None}
        out = {"agent_id": aid, "role": role, "token": token,
               "token_sha256": agents[aid]["token_sha256"]}
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(agents, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)  # atomic: the server never reads a partial file
    print(json.dumps(out, indent=2))
    return 0


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

class Store:
    def __init__(self, path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            # v1.1 migration: idempotency_key on pre-existing databases
            cols = [r["name"] for r in self._db.execute(
                "PRAGMA table_info(messages)")]
            if "idempotency_key" not in cols:
                self._db.execute(
                    "ALTER TABLE messages ADD COLUMN idempotency_key TEXT")
            # NULL keys never collide in a UNIQUE index, so plain posts
            # are unaffected; scoped per sender.
            self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_idem"
                " ON messages(sender, idempotency_key)")
            self._db.commit()

    def add_message(self, msg_id, thread_id, sender, recipient, mtype,
                    body, reply_to, metadata, created_at,
                    idempotency_key=None):
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO messages (id, thread_id, sender, recipient, type,"
                " body, reply_to, metadata, created_at, idempotency_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (msg_id, thread_id, sender, recipient, mtype, body, reply_to,
                 json.dumps(metadata) if metadata is not None else None,
                 created_at, idempotency_key),
            )
            self._db.commit()
            return cur.lastrowid

    def find_by_idempotency_key(self, sender, key):
        """Return (id, created_at) of the original post, or None."""
        with self._lock:
            row = self._db.execute(
                "SELECT id, created_at FROM messages"
                " WHERE sender = ? AND idempotency_key = ?",
                (sender, key)).fetchone()
        return (row["id"], row["created_at"]) if row else None

    def list_messages(self, since_seq=0, thread_id=None, to=None,
                      caller=None, limit=50, visible_to=None):
        q = ("SELECT seq, id, thread_id, sender, recipient, type, body,"
             " reply_to, metadata, created_at FROM messages WHERE seq > ?")
        args = [since_seq]
        if visible_to is not None:
            # per-agent token: to me, to '*', or sent by me
            q += " AND (recipient = ? OR recipient = '*' OR sender = ?)"
            args += [visible_to, visible_to]
        if thread_id:
            q += " AND thread_id = ?"
            args.append(thread_id)
        if to is not None:
            q += " AND recipient = ?"
            args.append(to)
        elif caller:
            # default: messages addressed to the caller, or broadcast
            q += " AND (recipient = ? OR recipient = '*')"
            args.append(caller)
        q += " ORDER BY seq ASC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return self._attach_receipts(self._redact_stale_links(
            [self._row_to_message(r) for r in rows]))

    def get_message(self, msg_id):
        with self._lock:
            row = self._db.execute(
                "SELECT seq, id, thread_id, sender, recipient, type, body,"
                " reply_to, metadata, created_at FROM messages WHERE id = ?",
                (msg_id,)).fetchone()
        if not row:
            return None
        return self._attach_receipts([self._row_to_message(row)])[0]

    def get_receipts(self, message_id):
        with self._lock:
            rows = self._db.execute(
                "SELECT agent, status, at FROM receipts"
                " WHERE message_id = ? ORDER BY at ASC",
                (message_id,)).fetchall()
        return [{"agent": r["agent"], "status": r["status"], "at": r["at"]}
                for r in rows]

    def _attach_receipts(self, msgs):
        """Spec v1.1: every message read carries its receipts."""
        ids = [m["id"] for m in msgs]
        if not ids:
            return msgs
        with self._lock:
            rows = self._db.execute(
                "SELECT message_id, agent, status, at FROM receipts"
                " WHERE message_id IN (%s)" % ",".join("?" * len(ids)),
                ids).fetchall()
        by_id = {}
        for r in rows:
            by_id.setdefault(r["message_id"], []).append(
                {"agent": r["agent"], "status": r["status"],
                 "at": r["at"]})
        for m in msgs:
            m["receipts"] = by_id.get(m["id"], [])
        return msgs

    def set_thread_resolved(self, thread_id, resolved_by, resolved_at):
        with self._lock:
            self._db.execute(
                "INSERT INTO thread_status (thread_id, status, resolved_at,"
                " resolved_by) VALUES (?, 'resolved', ?, ?)"
                " ON CONFLICT (thread_id) DO UPDATE SET"
                " status = 'resolved', resolved_at = excluded.resolved_at,"
                " resolved_by = excluded.resolved_by",
                (thread_id, resolved_at, resolved_by))
            self._db.commit()

    def reopen_thread(self, thread_id):
        with self._lock:
            self._db.execute(
                "INSERT INTO thread_status (thread_id, status, resolved_at,"
                " resolved_by) VALUES (?, 'open', NULL, NULL)"
                " ON CONFLICT (thread_id) DO UPDATE SET"
                " status = 'open', resolved_at = NULL, resolved_by = NULL",
                (thread_id,))
            self._db.commit()

    def get_thread_status(self, thread_id):
        with self._lock:
            row = self._db.execute(
                "SELECT status, resolved_at, resolved_by FROM thread_status"
                " WHERE thread_id = ?", (thread_id,)).fetchone()
        if row:
            return {"status": row["status"],
                    "resolved_at": row["resolved_at"],
                    "resolved_by": row["resolved_by"]}
        return {"status": "open", "resolved_at": None, "resolved_by": None}

    def add_receipt(self, message_id, agent, status, at):
        with self._lock:
            self._db.execute(
                "INSERT INTO receipts (message_id, agent, status, at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT (message_id, agent)"
                " DO UPDATE SET status = excluded.status, at = excluded.at",
                (message_id, agent, status, at),
            )
            self._db.commit()

    def mark_link_consumed(self, msg_id):
        """Flip metadata.one_time_link.consumed to true (if present) and
        erase its URL."""
        with self._lock:
            row = self._db.execute(
                "SELECT metadata FROM messages WHERE id = ?", (msg_id,)
            ).fetchone()
            if not row or not row["metadata"]:
                return
            try:
                meta = json.loads(row["metadata"])
            except ValueError:
                return
            link = meta.get("one_time_link") if isinstance(meta, dict) else None
            if isinstance(link, dict):
                redact_link(link)
                self._db.execute(
                    "UPDATE messages SET metadata = ? WHERE id = ?",
                    (json.dumps(meta), msg_id),
                )
                self._db.commit()

    def _redact_stale_links(self, msgs):
        """Erase the URL of any consumed or expired one_time_link before
        it is returned, and persist the redaction."""
        now = datetime.now(timezone.utc)
        for m in msgs:
            meta = m.get("metadata")
            link = meta.get("one_time_link") if isinstance(meta, dict) else None
            if not isinstance(link, dict) or link.get("url_redacted"):
                continue
            if not (link.get("consumed") or link_expired(link, now)):
                continue
            redact_link(link)
            with self._lock:
                self._db.execute(
                    "UPDATE messages SET metadata = ? WHERE id = ?",
                    (json.dumps(meta), m["id"]))
                self._db.commit()
        return msgs

    def purge_older_than(self, days):
        """Spec: retention purge. Delete messages older than `days`
        (by created_at) and mark any expired one_time_link entries as
        consumed (URL erased) on the survivors.
        Returns (deleted, links_marked)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)) \
            .isoformat().replace("+00:00", "Z")
        now = datetime.now(timezone.utc)
        with self._lock:
            doomed = self._db.execute(
                "SELECT id FROM messages WHERE created_at < ?", (cutoff,)
            ).fetchall()
            doomed_ids = [r["id"] for r in doomed]
            if doomed_ids:
                self._db.execute(
                    "DELETE FROM messages WHERE created_at < ?", (cutoff,))
                self._db.execute(
                    "DELETE FROM receipts WHERE message_id NOT IN"
                    " (SELECT id FROM messages)")
            marked = 0
            rows = self._db.execute(
                "SELECT id, metadata FROM messages"
                " WHERE metadata LIKE '%one_time_link%'").fetchall()
            for r in rows:
                try:
                    meta = json.loads(r["metadata"])
                except ValueError:
                    continue
                link = meta.get("one_time_link") \
                    if isinstance(meta, dict) else None
                if not isinstance(link, dict) or link.get("consumed") is True:
                    continue
                if link_expired(link, now):
                    redact_link(link)
                    self._db.execute(
                        "UPDATE messages SET metadata = ? WHERE id = ?",
                        (json.dumps(meta), r["id"]))
                    marked += 1
            self._db.commit()
        return len(doomed_ids), marked

    def thread_list(self, visible_to=None):
        q = ("SELECT thread_id, MAX(created_at) AS last_at, MAX(seq) AS mseq"
             " FROM messages GROUP BY thread_id")
        args = []
        if visible_to is not None:
            q += (" HAVING SUM(recipient = ? OR recipient = '*'"
                  " OR sender = ?) > 0")
            args = [visible_to, visible_to]
        with self._lock:
            rows = self._db.execute(q + " ORDER BY mseq DESC",
                                    args).fetchall()
        return [{"thread_id": r["thread_id"], "last_at": r["last_at"]}
                for r in rows]

    def unread_count(self, thread_id, caller):
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM messages m"
                " WHERE m.thread_id = ?"
                " AND (m.recipient = ? OR m.recipient = '*')"
                " AND NOT EXISTS (SELECT 1 FROM receipts r"
                "                 WHERE r.message_id = m.id AND r.agent = ?)",
                (thread_id, caller, caller)).fetchone()
        return row["n"]

    @staticmethod
    def _row_to_message(r):
        meta = None
        if r["metadata"]:
            try:
                meta = json.loads(r["metadata"])
            except ValueError:
                meta = None
        msg = {
            "id": r["id"],
            "thread_id": r["thread_id"],
            "from": r["sender"],
            "to": r["recipient"],
            "type": r["type"],
            "body": r["body"],
            "created_at": r["created_at"],
        }
        if r["reply_to"]:
            msg["reply_to"] = r["reply_to"]
        if meta is not None:
            msg["metadata"] = meta
        msg["_seq"] = r["seq"]  # internal; stripped before responding
        return msg


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "CutoutBus/" + VERSION

    # wired up in main()
    token = ""      # deprecated shared token (CUTOUT_TOKEN); may be empty
    agents = None   # AgentRegistry
    _legacy_warned = None
    store = None
    retention_days = 30
    rate_limit = RATE_LIMIT
    _rate_lock = threading.Lock()
    _rate_hits = collections.deque()
    _purge_lock = threading.Lock()
    _last_purge = 0.0

    @classmethod
    def _maybe_purge(cls):
        """Amortized retention purge: at most once every 24h."""
        if cls.retention_days <= 0 or cls.store is None:
            return
        now = time.monotonic()
        with cls._purge_lock:
            if now - cls._last_purge < 86400:
                return
            cls._last_purge = now
        deleted, marked = cls.store.purge_older_than(cls.retention_days)
        if deleted or marked:
            sys.stderr.write(
                "cutout: retention purge: %d messages deleted,"
                " %d expired links marked consumed\n" % (deleted, marked))

    # -- plumbing ------------------------------------------------------

    def log_message(self, fmt, *args):  # quieter than the default
        sys.stderr.write("cutout: " + fmt % args + "\n")

    def _send(self, code, obj, extra_headers=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in getattr(self, "_resp_headers", {}).items():
            self.send_header(k, v)
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, message, extra_headers=None):
        self._send(code, {"error": message}, extra_headers)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return None, "empty request body"
        try:
            return json.loads(raw.decode("utf-8")), None
        except (ValueError, UnicodeDecodeError):
            return None, "malformed JSON body"

    def _authenticate(self):
        """(agent_id, role) for a per-agent token; (None, "legacy") for
        the deprecated shared token; None when unauthorized."""
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return None
        presented = auth[7:].strip()
        found = self.agents.lookup(presented) \
            if self.agents and presented else None
        if found:
            return found
        if self.token and hmac.compare_digest(presented, self.token):
            self._warn_legacy()
            return (None, "legacy")
        return None

    @classmethod
    def _warn_legacy(cls):
        now = time.monotonic()
        if cls._legacy_warned is None or now - cls._legacy_warned >= 3600:
            cls._legacy_warned = now
            sys.stderr.write(
                "cutout: warning: request used the deprecated shared"
                " CUTOUT_TOKEN, which trusts from/agent/X-Agent-Id as sent."
                " Issue per-agent tokens (cutout_server.py agents add <id>)"
                " and then unset CUTOUT_TOKEN.\n")

    @classmethod
    def _rate_state(cls, consume):
        """Sliding 60s window. Returns dict with limited/retry/remaining/
        reset; consume=False only inspects (for /health)."""
        now = time.monotonic()
        with cls._rate_lock:
            dq = cls._rate_hits
            while dq and dq[0] <= now - RATE_WINDOW:
                dq.popleft()
            if consume:
                if len(dq) >= cls.rate_limit:
                    wait = dq[0] + RATE_WINDOW - now
                    return {"limited": True,
                            "retry": max(1, int(wait) + 1),
                            "remaining": 0,
                            "reset": int(time.time() + wait)}
                dq.append(now)
            return {"limited": False, "retry": 0,
                    "remaining": cls.rate_limit - len(dq),
                    "reset": int(time.time() + RATE_WINDOW)}

    @classmethod
    def _rate_headers(cls, info):
        return {"X-RateLimit-Limit": str(cls.rate_limit),
                "X-RateLimit-Remaining": str(info["remaining"]),
                "X-RateLimit-Reset": str(info["reset"])}

    def _guard(self, need_auth=True):
        """Auth, then rate limit. Returns True when the request may proceed.

        Only authenticated requests are charged against the budget, so a
        caller without the token cannot use it up for the agents that
        have it. Rate-limit headers are attached to every response
        (including 401/429) via self._resp_headers, which _send merges in.
        """
        ident = self._authenticate()
        if need_auth and ident is None:
            self._resp_headers = self._rate_headers(
                self._rate_state(consume=False))
            self._err(401, "unauthorized: bad or missing bearer token")
            return False
        info = self._rate_state(consume=True)
        self._resp_headers = self._rate_headers(info)
        self._maybe_purge()
        if info["limited"]:
            self._err(429, "rate limit exceeded",
                      {"Retry-After": str(info["retry"])})
            return False
        # self.caller is the calling agent id; self.role is "agent",
        # "operator", or "legacy" (shared token: X-Agent-Id is trusted).
        self.caller, self.role = ident or (None, "legacy")
        claimed = self.headers.get("X-Agent-Id")
        if self.role == "legacy":
            self.caller = claimed
        elif claimed is not None and claimed.strip() != self.caller:
            self._err(403, "X-Agent-Id does not match your token")
            return False
        return True

    def _check_self(self, data, field):
        """Per-agent token: `field` is optional and must name the caller.
        Fills it in and returns True, or sends 403 and returns False."""
        if self.role == "legacy":
            return True
        claimed = data.get(field)
        if claimed is not None and (not isinstance(claimed, str)
                                    or claimed.strip() != self.caller):
            self._err(403, "%s does not match your token" % field)
            return False
        data[field] = self.caller
        return True

    def _visible_to(self):
        """Agent id whose visibility limits reads, or None (no limit)."""
        return self.caller if self.role == "agent" else None

    @staticmethod
    def _public(msg):
        msg = dict(msg)
        msg.pop("_seq", None)
        return msg

    # -- routing -------------------------------------------------------

    def do_GET(self):
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/health":
                self._resp_headers = self._rate_headers(
                    self._rate_state(consume=False))
                self._send(200, {"ok": True, "version": VERSION})
            elif path == "/v1/messages":
                self._handle_get_messages()
            elif path == "/v1/threads":
                self._handle_get_threads()
            else:
                self._err(404, "not found")
        except BrokenPipeError:
            pass
        except Exception as exc:  # never leak tracebacks to clients
            self.log_message("internal error: %r", exc)
            try:
                self._err(500, "internal error")
            except BrokenPipeError:
                pass

    def do_POST(self):
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/v1/messages":
                self._handle_post_message()
            elif path == "/v1/receipts":
                self._handle_post_receipt()
            else:
                self._err(404, "not found")
        except BrokenPipeError:
            pass
        except Exception as exc:
            self.log_message("internal error: %r", exc)
            try:
                self._err(500, "internal error")
            except BrokenPipeError:
                pass

    # -- endpoints -----------------------------------------------------

    def _handle_post_message(self):
        if not self._guard():
            return
        data, err = self._read_json()
        if err:
            self._err(400, err)
            return
        if not isinstance(data, dict):
            self._err(422, "request body must be a JSON object")
            return
        if not self._check_self(data, "from"):
            return

        for field in ("thread_id", "from", "to", "type", "body"):
            val = data.get(field)
            if not isinstance(val, str) or not val.strip():
                self._err(422, "%s is required" % field)
                return

        thread_id = data["thread_id"].strip()
        sender = data["from"].strip()
        recipient = data["to"].strip()
        mtype = data["type"].strip()
        body = data["body"]
        reply_to = data.get("reply_to")
        metadata = data.get("metadata")
        idempotency_key = data.get("idempotency_key")

        if not AGENT_RE.match(sender):
            self._err(422, "from must be a kebab-case agent id")
            return
        if recipient != "*" and not AGENT_RE.match(recipient):
            self._err(422, "to must be a kebab-case agent id or '*'")
            return
        if mtype not in MESSAGE_TYPES:
            self._err(422, "type must be one of: %s"
                      % ", ".join(sorted(MESSAGE_TYPES)))
            return
        if len(body.encode("utf-8")) > BODY_MAX_BYTES:
            self._err(413, "body exceeds 20 KB")
            return
        if reply_to is not None and not isinstance(reply_to, str):
            self._err(422, "reply_to must be a string")
            return
        if metadata is not None and not isinstance(metadata, dict):
            self._err(422, "metadata must be an object")
            return
        if metadata is not None and \
                len(json.dumps(metadata).encode("utf-8")) > METADATA_MAX_BYTES:
            self._err(413, "metadata exceeds 16 KB")
            return
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) \
                    or not idempotency_key.strip() \
                    or len(idempotency_key) > IDEMPOTENCY_KEY_MAX:
                self._err(422, "idempotency_key must be a non-empty string"
                               " up to 128 chars")
                return
            idempotency_key = idempotency_key.strip()
            # Safe retry: the same logical send returns the original.
            original = self.store.find_by_idempotency_key(
                sender, idempotency_key)
            if original:
                self._send(200, {"id": original[0],
                                 "created_at": original[1],
                                 "duplicate": True})
                return
        if mtype == "link":
            link = (metadata or {}).get("one_time_link")
            if not isinstance(link, dict) or not link.get("url"):
                self._err(422, "link messages require"
                              " metadata.one_time_link.url")
                return
        link = (metadata or {}).get("one_time_link")
        if isinstance(link, dict):
            if link.get("expires_at") is not None \
                    and not valid_expires_at(link["expires_at"]):
                self._err(422, "metadata.one_time_link.expires_at must be"
                               " an RFC 3339 timestamp with an offset")
                return
            if link.get("consumed") is not None \
                    and not isinstance(link["consumed"], bool):
                self._err(422, "metadata.one_time_link.consumed must be"
                               " a boolean")
                return

        msg_id = new_id()
        created_at = utcnow()
        self.store.add_message(msg_id, thread_id, sender, recipient, mtype,
                               body, reply_to, metadata, created_at,
                               idempotency_key)
        if mtype == "resolve":
            self.store.set_thread_resolved(thread_id, sender, created_at)
        else:
            # any new work reopens a resolved thread
            self.store.reopen_thread(thread_id)
        self._send(201, {"id": msg_id, "created_at": created_at})

    def _handle_get_messages(self):
        if not self._guard():
            return
        qs = parse_qs(urlparse(self.path).query)

        def one(name, default=None):
            vals = qs.get(name)
            return vals[0] if vals else default

        since_raw = one("since")
        since_seq = 0
        if since_raw:
            since_seq = decode_cursor(since_raw)
            if since_seq is None:
                self._err(422, "invalid since cursor")
                return

        try:
            wait = int(one("wait", "0"))
        except ValueError:
            self._err(422, "wait must be an integer 0-60")
            return
        if not 0 <= wait <= LONG_POLL_MAX:
            self._err(422, "wait must be an integer 0-60")
            return

        try:
            limit = int(one("limit", "50"))
        except ValueError:
            self._err(422, "limit must be an integer 1-100")
            return
        if not 1 <= limit <= 100:
            self._err(422, "limit must be an integer 1-100")
            return

        thread_id = one("thread_id")
        to = one("to")

        deadline = time.monotonic() + wait
        rows = []
        while True:
            rows = self.store.list_messages(
                since_seq=since_seq, thread_id=thread_id, to=to,
                caller=None if self.role == "operator" else self.caller,
                limit=limit,
                visible_to=self._visible_to())
            if rows or time.monotonic() >= deadline:
                break
            time.sleep(0.25)  # long-poll: re-check until wait expires

        if rows:
            next_cursor = encode_cursor(rows[-1]["_seq"])
        else:
            # nothing new: hand back the cursor we were given so the
            # client keeps its place
            next_cursor = since_raw or encode_cursor(0)
        self._send(200, {"messages": [self._public(m) for m in rows],
                         "next_cursor": next_cursor})

    def _handle_post_receipt(self):
        if not self._guard():
            return
        data, err = self._read_json()
        if err:
            self._err(400, err)
            return
        if not isinstance(data, dict):
            self._err(422, "request body must be a JSON object")
            return
        if not self._check_self(data, "agent"):
            return

        message_id = data.get("message_id")
        agent = data.get("agent")
        status = data.get("status")
        if not isinstance(message_id, str) or not message_id:
            self._err(422, "message_id is required")
            return
        if not isinstance(agent, str) or not AGENT_RE.match(agent):
            self._err(422, "agent must be a kebab-case agent id")
            return
        if status not in RECEIPT_STATUSES:
            self._err(422, "status must be one of: %s"
                      % ", ".join(sorted(RECEIPT_STATUSES)))
            return
        msg = self.store.get_message(message_id)
        me = self._visible_to()
        if msg is None or (me is not None
                           and me not in (msg["to"], msg["from"])
                           and msg["to"] != "*"):
            self._err(404, "unknown message_id")
            return

        # idempotent on (message_id, agent): re-posting is a no-op update
        self.store.add_receipt(message_id, agent, status, utcnow())
        if status == "consumed":
            self.store.mark_link_consumed(message_id)
        self._send(201, {"ok": True})

    def _handle_get_threads(self):
        if not self._guard():
            return
        caller = self.caller
        out = []
        for t in self.store.thread_list(visible_to=self._visible_to()):
            unread = self.store.unread_count(t["thread_id"], caller) \
                if caller else 0
            st = self.store.get_thread_status(t["thread_id"])
            out.append({"thread_id": t["thread_id"],
                        "last_at": t["last_at"],
                        "unread": unread,
                        "status": st["status"],
                        "resolved_at": st["resolved_at"]})
        self._send(200, {"threads": out})


# --------------------------------------------------------------------------

def main():
    if sys.argv[1:2] == ["agents"]:
        sys.exit(agents_cli(sys.argv[2:]))
    ap = argparse.ArgumentParser(description="Project Cutout reference server"
                                 " (tokens: cutout_server.py agents -h)")
    ap.add_argument("--host", default=os.environ.get("CUTOUT_HOST",
                                                     "127.0.0.1"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("CUTOUT_PORT", "8765")))
    ap.add_argument("--db", default=os.environ.get("CUTOUT_DB",
                                                   "cutout.db"),
                    help="SQLite file path (use :memory: for ephemeral)")
    ap.add_argument("--agents-file",
                    default=os.environ.get("CUTOUT_AGENTS_FILE",
                                           "agents.json"),
                    help="per-agent token hashes (see: agents -h)")
    ap.add_argument("--token", default=os.environ.get("CUTOUT_TOKEN"),
                    help="DEPRECATED shared bearer token (prefer the env"
                         " var); kept for migration to per-agent tokens")
    ap.add_argument("--retention-days", type=int,
                    default=int(os.environ.get("CUTOUT_RETENTION_DAYS",
                                               "30")),
                    help="purge messages older than this (0 disables)")
    ap.add_argument("--rate-limit", type=int,
                    default=int(os.environ.get("CUTOUT_RATE_LIMIT",
                                               str(RATE_LIMIT))),
                    help="requests per minute per token (default 60)")
    args = ap.parse_args()

    if not args.token and not os.path.exists(args.agents_file):
        sys.exit("error: no agents file at %s; create one with"
                 " `cutout_server.py agents add <id>`" % args.agents_file)
    if args.token:
        sys.stderr.write(
            "cutout: warning: CUTOUT_TOKEN (shared token) is deprecated."
            " Requests that use it may claim any from/agent/X-Agent-Id."
            " Migrate to per-agent tokens; see SPEC.md.\n")
    if args.rate_limit < 1:
        sys.exit("error: --rate-limit must be at least 1")

    Handler.token = args.token or ""
    Handler.agents = AgentRegistry(args.agents_file)
    Handler.store = Store(args.db)
    Handler.retention_days = args.retention_days
    Handler.rate_limit = args.rate_limit
    if args.retention_days > 0:
        deleted, marked = Handler.store.purge_older_than(
            args.retention_days)
        Handler._last_purge = time.monotonic()
        if deleted or marked:
            sys.stderr.write(
                "cutout: startup retention purge: %d messages deleted,"
                " %d expired links marked consumed\n" % (deleted, marked))

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(
        "cutout: listening on http://%s:%d (db: %s)\n"
        % (args.host, args.port, args.db))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
