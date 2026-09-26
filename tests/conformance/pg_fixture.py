"""Throwaway PostgreSQL cluster for the supabase conformance target.

Starts a private cluster on a free 127.0.0.1 port (no unix socket), creates
the roles hosted Supabase provides (anon, authenticated, service_role) so the
schema's GRANT/REVOKE/RLS statements run unchanged, and removes everything on
stop(). Standard library only; needs initdb, pg_ctl and psql on PATH.
"""

import os
import shutil
import socket
import subprocess
import tempfile


class PgUnavailable(RuntimeError):
    """A PostgreSQL binary is missing. Callers fail closed (exit 2)."""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _need(binary):
    path = shutil.which(binary)
    if not path:
        raise PgUnavailable("%s not found on PATH" % binary)
    return path


class PgCluster:
    def __init__(self):
        self.initdb = _need("initdb")
        self.pg_ctl = _need("pg_ctl")
        self.psql_bin = _need("psql")
        self.dir = tempfile.mkdtemp(prefix="cutout-pg-")
        self.data = os.path.join(self.dir, "data")
        self.log = os.path.join(self.dir, "pg.log")
        self.port = _free_port()
        self.url = "postgres://postgres@127.0.0.1:%d/postgres" % self.port
        self._started = False

    def start(self):
        subprocess.run(
            [self.initdb, "-D", self.data, "-U", "postgres", "--auth=trust",
             "--no-sync"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        opts = ("-p %d -c listen_addresses=127.0.0.1 "
                "-c unix_socket_directories='' -c fsync=off" % self.port)
        subprocess.run(
            [self.pg_ctl, "-D", self.data, "-o", opts, "-l", self.log, "-w",
             "start"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self._started = True
        self.psql("do $$ begin %s end $$;" % " ".join(
            "if not exists (select 1 from pg_roles where rolname = '%s') "
            "then create role %s nologin; end if;" % (r, r)
            for r in ("anon", "authenticated", "service_role")))
        return self

    def _args(self, db):
        return [self.psql_bin, "-h", "127.0.0.1", "-p", str(self.port),
                "-U", "postgres", "-d", db, "-qAtX", "-v", "ON_ERROR_STOP=1"]

    def psql(self, sql, db="postgres"):
        """Run SQL and return stdout (tuples only, unaligned). Raises
        RuntimeError with psql's stderr when the SQL fails."""
        proc = subprocess.run(self._args(db) + ["-c", sql],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError("psql failed: %s" % proc.stderr.strip())
        return proc.stdout.strip()

    def psql_file(self, path, db="postgres"):
        proc = subprocess.run(self._args(db) + ["-f", path],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError("psql -f %s failed: %s"
                               % (os.path.basename(path), proc.stderr.strip()))
        return proc.stdout

    def popen_psql(self, sql):
        """Start psql in the background (for overlapping transactions)."""
        return subprocess.Popen(self._args("postgres") + ["-c", sql],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def stop(self):
        if self._started:
            subprocess.run([self.pg_ctl, "-D", self.data, "-m", "immediate",
                            "stop"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            self._started = False
        shutil.rmtree(self.dir, ignore_errors=True)
