"""Dual-target conformance harness.

One black-box suite, two implementations of SPEC v1.1:

  CUTOUT_TARGET=python    reference server (server/cutout_server.py, SQLite)
  CUTOUT_TARGET=supabase  edge function (supabase/index.ts) under local Deno,
                          against a throwaway PostgreSQL 16 cluster

Both targets run the files in this checkout, unmodified, with one shared
CUTOUT_TOKEN. Agents identify themselves with X-Agent-Id, as in SPEC v1.1.

The SPEC rate limit (60 requests per minute, one shared budget) is kept.
Before every test the harness gives the target a fresh budget: it restarts
the Python server, or empties cutout.rate_log for the edge function.

Standard library only. The harness never skips: a missing binary is a hard
error (exit 2 from tests/run_conformance.py), so a green run always means
the target actually ran.
"""

import atexit
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
from pg_fixture import PgCluster, PgUnavailable  # noqa: E402,F401

SPEC_VERSION = "1.1"
RATE_LIMIT = 60  # SPEC: 60 req/min per token
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class HarnessError(RuntimeError):
    pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def unique(prefix):
    """Unique thread / key ids so tests never see each other's data."""
    return "%s-%s" % (prefix, uuid.uuid4().hex[:10])


def new_msg_id():
    """ULID-shaped id for rows inserted directly into storage."""
    return "msg_" + "".join(secrets.choice(_CROCKFORD) for _ in range(26))


def utc_iso(seconds_from_now=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def http(base_url, method, path, *, token=None, body=None, params=None,
         headers=None, raw_body=None, timeout=40):
    """Return (status, lowercase headers, parsed JSON or {"_raw": text})."""
    url = base_url + path
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None})
    data = raw_body if raw_body is not None else (
        json.dumps(body).encode("utf-8") if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, hdrs, raw = resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        status, hdrs, raw = exc.code, exc.headers, exc.read()
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text else {}
    except ValueError:
        parsed = {"_raw": text}
    return status, {k.lower(): v for k, v in hdrs.items()}, parsed


def raw_status_line(base_url, request_bytes, timeout=4.0):
    """Send raw bytes on a fresh socket; return the HTTP status line, or
    None when the server sends nothing before `timeout` seconds."""
    u = urllib.parse.urlparse(base_url)
    s = socket.create_connection((u.hostname, u.port), timeout=timeout)
    try:
        s.sendall(request_bytes)
        chunks = b""
        while b"\r\n" not in chunks:
            data = s.recv(4096)
            if not data:
                break
            chunks += data
        return chunks.split(b"\r\n", 1)[0].decode("latin-1") or None
    except socket.timeout:
        return None
    finally:
        s.close()


def _wait_health(base_url, proc, log_path, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            st, _, body = http(base_url, "GET", "/health", timeout=2)
            if st == 200:
                return body
        except OSError:
            pass
        time.sleep(0.05)
    tail = ""
    if os.path.exists(log_path):
        with open(log_path, "r", errors="replace") as fh:
            tail = fh.read()[-2000:]
    raise HarnessError("server at %s did not become healthy; log tail:\n%s"
                       % (base_url, tail))


def _stop_proc(p):
    # Children run in their own process group; stop the whole group.
    if p.poll() is not None:
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            p.kill()
        p.wait(timeout=5)


class Target:
    """Common surface the tests use. Subclasses boot a concrete server."""

    name = "?"

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="cutout-conf-")
        self.token = "conformance-" + secrets.token_hex(16)
        self.main = None      # the process behind base_url
        self.base_url = None

    # -- API helpers ----------------------------------------------------
    def api(self, agent, method, path, *, body=None, params=None,
            headers=None, token=None, raw_body=None, auth=True,
            base_url=None, timeout=40):
        """One request as `agent` (sent as X-Agent-Id; None sends none)
        with the shared token (auth=False sends no Authorization)."""
        hdrs = dict(headers or {})
        if agent:
            hdrs.setdefault("X-Agent-Id", agent)
        tok = None
        if auth:
            tok = token if token is not None else self.token
        return http(base_url or self.base_url, method, path, token=tok,
                    body=body, params=params, headers=hdrs,
                    raw_body=raw_body, timeout=timeout)

    def post(self, agent, **fields):
        body = {"from": agent, "to": "*", "type": "note",
                "body": "status update"}
        body.update(fields)
        return self.api(agent, "POST", "/v1/messages", body=body)

    def poll(self, agent, **params):
        return self.api(agent, "GET", "/v1/messages", params=params)

    def receipt(self, agent, message_id, status):
        return self.api(agent, "POST", "/v1/receipts", body={
            "message_id": message_id, "agent": agent, "status": status})

    # -- lifecycle --------------------------------------------------------
    def start(self):
        self.main, self.base_url = self._spawn()

    def _spawn(self, extra_env=None):
        raise NotImplementedError

    def fresh_budget(self):
        """Give the next test the full SPEC rate budget."""
        raise NotImplementedError

    def stop(self):
        if self.main is not None:
            _stop_proc(self.main)
            self.main = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- storage hooks (white-box: time travel and legacy rows only) ------
    def backdate_message(self, message_id, seconds):
        raise NotImplementedError

    def run_purge(self, retention_days=30):
        """Run the implementation's own retention purge. Raises
        HarnessError if the purge itself fails."""
        raise NotImplementedError

    def raw_metadata(self, message_id):
        """Stored metadata (dict) for a message, bypassing the API."""
        raise NotImplementedError

    def insert_raw_message(self, message_id, thread_id, metadata,
                           mtype="link"):
        """Insert a row straight into storage (models data that an older
        server version accepted)."""
        raise NotImplementedError

    def message_exists(self, message_id):
        raise NotImplementedError

    def delete_message(self, message_id):
        """Remove a row a test wrote on purpose (e.g. malformed metadata an
        unfixed server accepted) so it cannot affect later tests."""
        raise NotImplementedError


class PythonTarget(Target):
    name = "python"

    def __init__(self):
        super().__init__()
        self.server = os.path.join(ROOT, "server", "cutout_server.py")
        self.db = os.path.join(self.tmp, "bus.db")

    def _spawn(self, extra_env=None):
        port = free_port()
        env = dict(os.environ)
        env.update({"CUTOUT_TOKEN": self.token,
                    "CUTOUT_RETENTION_DAYS": "30"})
        env.update(extra_env or {})
        log_path = os.path.join(self.tmp, "server-%d.log" % port)
        with open(log_path, "w") as log:
            proc = subprocess.Popen(
                [sys.executable, self.server, "--host", "127.0.0.1",
                 "--port", str(port), "--db", self.db],
                env=env, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
        base = "http://127.0.0.1:%d" % port
        try:
            _wait_health(base, proc, log_path)
        except BaseException:
            _stop_proc(proc)
            raise
        return proc, base

    def _restart(self, extra_env=None):
        # Same SQLite file, new process: the in-memory rate window resets
        # and the startup retention purge runs.
        _stop_proc(self.main)
        self.main, self.base_url = self._spawn(extra_env)

    def fresh_budget(self):
        self._restart()

    def run_purge(self, retention_days=30):
        # The reference server purges at startup (and then daily).
        self._restart({"CUTOUT_RETENTION_DAYS": str(int(retention_days))})

    def _sql(self, query, args=()):
        con = sqlite3.connect(self.db)
        try:
            rows = con.execute(query, args).fetchall()
            con.commit()
            return rows
        finally:
            con.close()

    def backdate_message(self, message_id, seconds):
        ts = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        self._sql("UPDATE messages SET created_at = ? WHERE id = ?",
                  (ts.isoformat().replace("+00:00", "Z"), message_id))

    def raw_metadata(self, message_id):
        rows = self._sql("SELECT metadata FROM messages WHERE id = ?",
                         (message_id,))
        if not rows:
            return None
        return json.loads(rows[0][0]) if rows[0][0] else {}

    def insert_raw_message(self, message_id, thread_id, metadata,
                           mtype="link"):
        self._sql(
            "INSERT INTO messages (id, thread_id, sender, recipient, type,"
            " body, metadata, created_at) VALUES (?, ?, 'koda', 'instinct',"
            " ?, 'stored by an older server', ?, ?)",
            (message_id, thread_id, mtype, json.dumps(metadata),
             datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")))

    def message_exists(self, message_id):
        return bool(self._sql("SELECT 1 FROM messages WHERE id = ?",
                              (message_id,)))

    def delete_message(self, message_id):
        self._sql("DELETE FROM receipts WHERE message_id = ?", (message_id,))
        self._sql("DELETE FROM messages WHERE id = ?", (message_id,))


# pg_cron is not available on a plain local cluster. The harness removes
# only the top-level cron statements; everything else in schema.sql runs as
# written. A schema that already guards pg_cron itself is left unchanged.
_PG_CRON_CREATE = re.compile(r"^create extension if not exists pg_cron;",
                             re.MULTILINE)
_PG_CRON_BLOCK = re.compile(
    r"^create extension if not exists pg_cron;.*?"
    r"^select cron\.schedule\([^\n]*\n?", re.DOTALL | re.MULTILINE)


def strip_pg_cron(sql):
    if not _PG_CRON_CREATE.search(sql):
        return sql
    stripped, n = _PG_CRON_BLOCK.subn("", sql)
    if n != 1 or _PG_CRON_CREATE.search(stripped) or "cron." in stripped:
        raise HarnessError("could not isolate the pg_cron statements in "
                           "supabase/schema.sql; update strip_pg_cron()")
    return stripped


SCHEMA_FILES = ("schema.sql", "schema_v1.1.sql")


class SupabaseTarget(Target):
    name = "supabase"

    def __init__(self):
        super().__init__()
        self.deno = shutil.which("deno")
        if not self.deno:
            raise PgUnavailable("deno not found on PATH")
        self.index = os.path.join(ROOT, "supabase", "index.ts")
        self.pg = PgCluster().start()
        try:
            self.apply_schema("postgres")
        except BaseException:
            self.pg.stop()  # do not leak the cluster if setup fails
            raise

    def apply_schema(self, db):
        """schema.sql (pg_cron statements removed) then schema_v1.1.sql,
        the install order in supabase/README.md."""
        for name in SCHEMA_FILES:
            with open(os.path.join(ROOT, "supabase", name)) as fh:
                sql = fh.read()
            if name == "schema.sql":
                sql = strip_pg_cron(sql)
            path = os.path.join(self.tmp, "%s.%s" % (db, name))
            with open(path, "w") as fh:
                fh.write(sql)
            self.pg.psql_file(path, db=db)

    def _spawn(self, extra_env=None):
        port = free_port()
        env = dict(os.environ)
        env.pop("POOLER_HOST", None)
        env.update({"CUTOUT_TOKEN": self.token,
                    "SUPABASE_DB_URL": self.pg.url,
                    "DENO_SERVE_ADDRESS": "tcp:127.0.0.1:%d" % port,
                    "DENO_NO_UPDATE_CHECK": "1"})
        env.update(extra_env or {})
        log_path = os.path.join(self.tmp, "deno-%d.log" % port)
        with open(log_path, "w") as log:
            proc = subprocess.Popen(
                [self.deno, "run", "--allow-net", "--allow-env",
                 "--allow-read", "--allow-sys", self.index],
                env=env, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
        base = "http://127.0.0.1:%d" % port
        try:
            _wait_health(base, proc, log_path, timeout=120)
        except BaseException:
            _stop_proc(proc)
            raise
        return proc, base

    def fresh_budget(self):
        self.pg.psql("delete from cutout.rate_log;")

    def run_purge(self, retention_days=30):
        try:
            out = self.pg.psql("select cutout.purge(%d);"
                               % int(retention_days))
        except RuntimeError as exc:
            raise HarnessError(str(exc))
        return json.loads(out) if out else {}

    @staticmethod
    def _lit(text):
        return "'%s'" % text.replace("'", "''")

    def backdate_message(self, message_id, seconds):
        self.pg.psql(
            "update cutout.messages set created_at = created_at - "
            "make_interval(secs => %d) where id = %s;"
            % (int(seconds), self._lit(message_id)))

    def raw_metadata(self, message_id):
        out = self.pg.psql("select metadata::text from cutout.messages "
                           "where id = %s;" % self._lit(message_id))
        return json.loads(out) if out else None

    def insert_raw_message(self, message_id, thread_id, metadata,
                           mtype="link"):
        self.pg.psql(
            "insert into cutout.messages (id, thread_id, from_agent, "
            "to_agent, type, body, metadata) values (%s, %s, 'koda', "
            "'instinct', %s, 'stored by an older server', %s::jsonb);"
            % (self._lit(message_id), self._lit(thread_id), self._lit(mtype),
               self._lit(json.dumps(metadata))))

    def message_exists(self, message_id):
        return self.pg.psql("select count(*) from cutout.messages "
                            "where id = %s;" % self._lit(message_id)) == "1"

    def delete_message(self, message_id):
        self.pg.psql("delete from cutout.messages where id = %s;"
                     % self._lit(message_id))

    def stop(self):
        super().stop()
        self.pg.stop()


_TARGET = None


def target_name():
    return os.environ.get("CUTOUT_TARGET", "python")


def get_target():
    """Process-wide target, started once and stopped at exit."""
    global _TARGET
    if _TARGET is not None:
        return _TARGET
    cls = {"python": PythonTarget, "supabase": SupabaseTarget}.get(
        target_name())
    if cls is None:
        raise HarnessError("unknown CUTOUT_TARGET %r" % target_name())
    target = cls()
    atexit.register(target.stop)
    target.start()
    _TARGET = target
    return target
