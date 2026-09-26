"""Shared test base for the conformance suite."""

import re
import time
import unittest
from datetime import datetime, timedelta, timezone

import harness

TARGET = harness.target_name()

ID_RE = re.compile(r"^msg_[0-9A-HJKMNP-TV-Z]{26}$")
# RFC 3339 in UTC. The fraction is optional: the reference server writes
# microseconds, the edge function milliseconds.
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
MESSAGE_KEYS = {"id", "thread_id", "from", "to", "type", "body", "reply_to",
                "metadata", "created_at", "receipts"}


def known_bug(targets, fix):
    """Mark a test that fails on main because of a known bug.

    `targets` is "python", "supabase" or "both": the servers that have the
    bug today. `fix` names the pull request or branch that removes it. On an
    affected target the test is wrapped in unittest.expectedFailure, so the
    run stays green while the bug exists. When the fix merges, unittest
    reports an "unexpected success" and the run fails: delete the
    @known_bug line (or narrow `targets`) in the same PR.

    The runner also checks that each expected failure is an assertion
    failure, so a crash or harness error cannot hide behind the marker.
    """
    affected = {"python", "supabase"} if targets == "both" else {targets}
    if not affected <= {"python", "supabase"}:
        raise ValueError("unknown target in %r" % (targets,))

    def deco(fn):
        fn.known_bug = (sorted(affected), fix)
        if TARGET in affected:
            return unittest.expectedFailure(fn)
        return fn
    return deco


def iso_in(seconds, offset_hours=0):
    """RFC 3339 timestamp `seconds` from now, written in a given offset."""
    tz = timezone(timedelta(hours=offset_hours))
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)) \
        .astimezone(tz).isoformat(timespec="seconds")


class ConformanceCase(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.t = harness.get_target()

    def setUp(self):
        # Every test starts with the full SPEC budget of 60 requests.
        self.t.fresh_budget()

    # -- assertions ------------------------------------------------------
    def assertError(self, resp, status):
        """SPEC v1.1 error shape: the status and {"error": "<text>"}."""
        st, _, body = resp
        self.assertEqual(st, status, "expected HTTP %d, got %d %r"
                         % (status, st, body))
        self.assertIsInstance(body, dict)
        self.assertIsInstance(body.get("error"), str,
                              "error body lacks an error string: %r" % body)

    def assertCreated(self, resp):
        st, _, body = resp
        self.assertEqual(st, 201, "expected 201, got %d %r" % (st, body))
        return body

    # -- helpers ---------------------------------------------------------
    def post_ok(self, agent, **fields):
        return self.assertCreated(self.t.post(agent, **fields))

    def messages(self, agent, **params):
        st, _, body = self.t.poll(agent, **params)
        self.assertEqual(st, 200, "poll failed: %d %r" % (st, body))
        return body

    def find(self, agent, thread_id, message_id, **params):
        batch = self.messages(agent, thread_id=thread_id, **params)
        hits = [m for m in batch["messages"] if m["id"] == message_id]
        return hits[0] if hits else None

    def link_message(self, sender, recipient, thread_id, *, expires_at=None,
                     url="https://accounts.example.com/magic?login=7f3c9a"):
        return self.post_ok(
            sender, thread_id=thread_id, to=recipient, type="link",
            body="One-time sign-in link for the vendor dashboard.",
            metadata={"one_time_link": {
                "url": url, "consumed": False,
                "expires_at": expires_at or harness.utc_iso(900)}})

    def thread_row(self, agent, thread_id):
        st, _, body = self.t.api(agent, "GET", "/v1/threads")
        self.assertEqual(st, 200, body)
        hits = [x for x in body["threads"] if x["thread_id"] == thread_id]
        return hits[0] if hits else None

    def wait_until(self, predicate, timeout=5.0, interval=0.1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()
