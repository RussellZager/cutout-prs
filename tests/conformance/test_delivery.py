"""Delivery guarantees: exactly-once cursors under concurrency, long-poll
timing, and retention."""

import threading
import time

import harness
from base import ConformanceCase, known_bug


def drain(case, agent, thread_id, cursor=None, limit=7, rounds=50):
    """Page through a thread until empty; return (messages, cursor)."""
    seen = []
    for _ in range(rounds):
        params = {"thread_id": thread_id, "limit": limit}
        if cursor:
            params["since"] = cursor
        batch = case.messages(agent, **params)
        seen.extend(batch["messages"])
        cursor = batch["next_cursor"]
        if not batch["messages"]:
            break
    return seen, cursor


class OrderingTests(ConformanceCase):

    def test_concurrent_posts_delivered_exactly_once(self):
        thread = harness.unique("burst")
        posted, errors = [], []
        lock = threading.Lock()

        def writer(n):
            for i in range(5):
                st, _, body = self.t.post("koda", thread_id=thread, to="*",
                                          body="writer %d item %d" % (n, i))
                with lock:
                    (posted if st == 201 else errors).append(
                        body.get("id", body))

        threads = [threading.Thread(target=writer, args=(n,))
                   for n in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        seen, _ = drain(self, "instinct", thread)
        ids = [m["id"] for m in seen]
        self.assertEqual(len(ids), len(set(ids)), "duplicate delivery")
        self.assertEqual(sorted(ids), sorted(posted), "missing delivery")


class LongPollTests(ConformanceCase):

    def test_long_poll_wakes_promptly(self):
        thread = harness.unique("wake")
        start = self.messages("instinct", thread_id=thread)["next_cursor"]
        out = {}

        def park():
            out["t0"] = time.monotonic()
            out["resp"] = self.t.poll("instinct", thread_id=thread,
                                      since=start, wait=8)
            out["returned_at"] = time.monotonic()

        th = threading.Thread(target=park)
        th.start()
        time.sleep(1.0)
        posted_at = time.monotonic()
        created = self.post_ok("koda", thread_id=thread, to="instinct",
                               body="Wake up: new task.")
        th.join(timeout=15)
        self.assertFalse(th.is_alive())
        st, _, body = out["resp"]
        self.assertEqual(st, 200)
        self.assertEqual([m["id"] for m in body["messages"]], [created["id"]])
        self.assertLess(out["returned_at"] - posted_at, 1.5,
                        "long-poll noticed the message too late")
        # Non-vacuity: the poll was parked before the post.
        self.assertGreaterEqual(out["returned_at"] - out["t0"], 0.8,
                                "poll was not parked")

    def test_long_poll_timeout_keeps_cursor(self):
        thread = harness.unique("idle")
        self.post_ok("koda", thread_id=thread, to="instinct", body="seed")
        c0 = self.messages("instinct", thread_id=thread)["next_cursor"]
        t0 = time.monotonic()
        st, _, body = self.t.poll("instinct", thread_id=thread, since=c0,
                                  wait=2)
        self.assertEqual(st, 200)
        self.assertEqual((body["messages"], body["next_cursor"]), ([], c0))
        self.assertGreaterEqual(time.monotonic() - t0, 1.5)


class RetentionTests(ConformanceCase):

    def test_purge_removes_old_messages_receipts_and_keys(self):
        thread = harness.unique("retention")
        key = harness.unique("old-send")
        old = self.post_ok("koda", thread_id=thread, to="instinct",
                           body="Stale coordination note.",
                           idempotency_key=key)
        self.t.receipt("instinct", old["id"], "received")
        keep = self.post_ok("koda", thread_id=thread, to="instinct",
                            body="Fresh note.")
        self.t.backdate_message(old["id"], 31 * 86400)
        self.t.run_purge()
        self.assertFalse(self.t.message_exists(old["id"]))
        self.assertTrue(self.t.message_exists(keep["id"]))
        again = self.t.post("koda", thread_id=thread, to="instinct",
                            body="Stale coordination note.",
                            idempotency_key=key)
        self.assertEqual(again[0], 201, "idempotency key outlived message")

    def test_retention_zero_keeps_everything(self):
        mid = self.post_ok("koda", thread_id=harness.unique("keep"),
                           to="instinct", body="Archive me.")["id"]
        self.t.backdate_message(mid, 400 * 86400)
        self.t.run_purge(retention_days=0)
        self.assertTrue(self.t.message_exists(mid),
                        "retention 0 must disable deletion")
        self.t.run_purge(retention_days=30)
        self.assertFalse(self.t.message_exists(mid),
                         "control: a 30-day purge removes it")


if harness.target_name() == "supabase":

    def _wait_for_sleeper(pg, marker, timeout=5.0):
        """Block until the slow transaction is inside pg_sleep (its INSERT
        has run but not committed)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            n = pg.psql("select count(*) from pg_stat_activity where "
                        "query like '%%%s%%' and query like '%%pg_sleep%%' "
                        "and state = 'active' and pid <> pg_backend_pid();"
                        % marker)
            if n != "0":
                return True
            time.sleep(0.05)
        return False

    class PostgresCommitOrderTests(ConformanceCase):
        """A transaction that starts first but commits last must still be
        delivered to a poller that follows next_cursor."""

        # created_at is the transaction start time, so the late commit sorts
        # behind the poller's cursor: #6 fix/postgres-commit-order.
        @known_bug("supabase", "#6 fix/postgres-commit-order")
        def test_late_committing_message_is_delivered(self):
            pg = self.t.pg
            thread = harness.unique("slowtx")
            marker = harness.unique("mark")
            self.post_ok("koda", thread_id=thread, to="instinct",
                         body="seed")
            c0 = self.messages("instinct", thread_id=thread)["next_cursor"]
            slow = pg.popen_psql(
                "begin; insert into cutout.messages (id, thread_id, "
                "from_agent, to_agent, type, body) values ('%s', '%s', "
                "'koda', 'instinct', 'note', 'slow A %s'); "
                "select pg_sleep(2); commit;"
                % (harness.new_msg_id(), thread, marker))
            self.assertTrue(_wait_for_sleeper(pg, marker),
                            "slow transaction never started")
            fast = {}

            def post_fast():
                fast["resp"] = self.t.post("koda", thread_id=thread,
                                           to="instinct", body="fast B")

            th = threading.Thread(target=post_fast)
            th.start()
            seen, cursor = [], c0
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                batch = self.messages("instinct", thread_id=thread,
                                      since=cursor)
                seen.extend(batch["messages"])
                cursor = batch["next_cursor"]
                time.sleep(0.2)
            th.join(timeout=10)
            _, err = slow.communicate(timeout=10)
            self.assertEqual(slow.returncode, 0, err)
            self.assertEqual(fast["resp"][0], 201, fast["resp"])
            bodies = sorted(m["body"].split(" mark-")[0] for m in seen)
            self.assertEqual(bodies, ["fast B", "slow A"],
                             "late-committing message was skipped")
