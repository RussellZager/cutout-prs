"""Postgres-level checks for the Supabase port (supabase target only):
schema install, privileges, error handling and rate accounting."""

import json
import os
import time

import harness
from base import ConformanceCase, known_bug

if harness.target_name() == "supabase":

    class SchemaTests(ConformanceCase):

        def scratch_db(self, name):
            self.t.pg.psql("drop database if exists %s;" % name)
            self.t.pg.psql("create database %s;" % name)
            self.addCleanup(self.t.pg.psql,
                            "drop database if exists %s;" % name)

        def test_schema_files_are_rerunnable(self):
            self.scratch_db("conf_rerun")
            self.t.apply_schema("conf_rerun")
            self.t.apply_schema("conf_rerun")

        # schema.sql hard-requires pg_cron: fix/supabase-hardening.
        @known_bug("supabase", "fix/supabase-hardening")
        def test_schema_loads_without_pg_cron(self):
            self.scratch_db("conf_nocron")
            path = os.path.join(harness.ROOT, "supabase", "schema.sql")
            try:
                self.t.pg.psql_file(path, db="conf_nocron")
            except RuntimeError as exc:
                self.fail("schema.sql as shipped failed: %s" % exc)

    class PrivilegeTests(ConformanceCase):

        def test_client_roles_have_no_default_access(self):
            for role in ("anon", "authenticated"):
                for sql in ("select count(*) from cutout.messages;",
                            "select cutout.purge(30);"):
                    with self.subTest(role=role, sql=sql):
                        with self.assertRaisesRegex(RuntimeError,
                                                    "permission denied"):
                            self.t.pg.psql("set role %s; %s" % (role, sql))

        # Only idempotency_keys has RLS: fix/supabase-hardening.
        @known_bug("supabase", "fix/supabase-hardening")
        def test_rls_enabled_on_every_cutout_table(self):
            off = self.t.pg.psql(
                "select coalesce(string_agg(c.relname, ',' order by "
                "c.relname), '') from pg_class c join pg_namespace n on "
                "n.oid = c.relnamespace where n.nspname = 'cutout' and "
                "c.relkind = 'r' and not c.relrowsecurity;")
            self.assertEqual(off, "", "RLS disabled on: %s" % off)

        # Exposing the schema through the Data API exposes every row and
        # purge(): fix/supabase-hardening.
        @known_bug("supabase", "fix/supabase-hardening")
        def test_exposed_schema_leaks_no_rows_and_no_purge(self):
            self.post_ok("koda", thread_id=harness.unique("rls"),
                         to="instinct", body="Row that must stay private.")
            expose = ("grant usage on schema cutout to anon; "
                      "grant select on all tables in schema cutout to anon; "
                      "set local role anon; ")
            rows = self.t.pg.psql("begin; %s select count(*) from "
                                  "cutout.messages; rollback;" % expose)
            self.assertEqual(rows, "0", "anon read messages after the "
                                        "schema was exposed")
            with self.assertRaisesRegex(RuntimeError, "permission denied"):
                self.t.pg.psql("begin; %s select cutout.purge(30); "
                               "rollback;" % expose)

    class ServerFaultTests(ConformanceCase):

        CANARY = "CANARY_internal_detail_7f3c"

        def test_database_error_is_a_sanitized_500(self):
            pg = self.t.pg
            thread = harness.unique("fault")
            pg.psql(
                "create or replace function cutout.zz_conf_fault() "
                "returns trigger language plpgsql as $$ begin "
                "if new.thread_id = '%s' then raise exception '%s in "
                "cutout.zz_conf_fault'; end if; return new; end $$; "
                "create trigger zz_conf_fault before insert on "
                "cutout.messages for each row execute function "
                "cutout.zz_conf_fault();" % (thread, self.CANARY))
            try:
                st, _, body = self.t.post("koda", thread_id=thread,
                                          to="instinct", body="fault")
            finally:
                pg.psql("drop trigger if exists zz_conf_fault on "
                        "cutout.messages; drop function if exists "
                        "cutout.zz_conf_fault();")
            self.assertEqual(st, 500, body)
            self.assertEqual(set(body), {"error"}, body)
            raw = json.dumps(body)
            for needle in ("CANARY", "zz_conf_fault", "cutout."):
                self.assertNotIn(needle, raw)
            self.assertCreated(self.t.post("koda", thread_id=thread,
                                           to="instinct", body="after"))

        # A write that outlives the client-side timer answers 500 and then
        # commits anyway: fix/supabase-hardening.
        @known_bug("supabase", "fix/supabase-hardening")
        def test_slow_write_times_out_without_committing(self):
            pg = self.t.pg
            thread = harness.unique("lockwait")
            marker = harness.unique("hold")
            holder = pg.popen_psql(
                "begin; lock table cutout.messages in exclusive mode; "
                "select '%s', pg_sleep(6); commit;" % marker)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and pg.psql(
                    "select count(*) from pg_stat_activity where query "
                    "like '%%%s%%' and state = 'active' and pid <> "
                    "pg_backend_pid();" % marker) == "0":
                time.sleep(0.05)
            st, hdrs, body = self.t.post("koda", thread_id=thread,
                                         to="instinct", body="blocked")
            holder.communicate(timeout=15)
            time.sleep(1.0)
            n = pg.psql("select count(*) from cutout.messages where "
                        "thread_id = '%s';" % thread)
            self.assertEqual((st, n), (503, "0"),
                             "status %d %r; rows committed: %s"
                             % (st, body, n))
            self.assertGreaterEqual(int(hdrs.get("retry-after", "0")), 1)

    class RateAccountingTests(ConformanceCase):

        # Regression test for #3: a client that keeps retrying while
        # limited is let back in after the window.
        def test_rejected_requests_do_not_extend_lockout(self):
            limit = harness.RATE_LIMIT
            ok = [self.t.api("koda", "GET", "/v1/threads")[0]
                  for _ in range(limit)]
            self.assertEqual(ok, [200] * limit)
            # Time travel: the admitted requests happened 55 s ago.
            self.t.pg.psql("update cutout.rate_log set at = at - "
                           "interval '55 seconds';")
            moved = time.monotonic()
            rejected = [self.t.api("koda", "GET", "/v1/threads")[0]
                        for _ in range(limit)]
            self.assertLess(time.monotonic() - moved, 5.0,
                            "rejected burst too slow to stay in window")
            self.assertEqual(set(rejected), {429})
            # Wait until the admitted requests have left the window.
            time.sleep(max(0.0, 6.0 - (time.monotonic() - moved)))
            st, _, body = self.t.api("koda", "GET", "/v1/threads")
            self.assertEqual(st, 200, "still limited after the window "
                                      "passed: %r" % body)
