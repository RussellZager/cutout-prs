"""Wire contract (SPEC v1.1). Both servers must give the same observable
answer to every test in this file."""

import time

import harness
from base import ConformanceCase, ID_RE, MESSAGE_KEYS, TS_RE, known_bug


def post_head(t, content_length):
    """Request line and headers of a POST with no body bytes."""
    host = t.base_url.split("//", 1)[1]
    return ("POST /v1/messages HTTP/1.1\r\nHost: %s\r\n"
            "Authorization: Bearer %s\r\nX-Agent-Id: koda\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: %s\r\n\r\n"
            % (host, t.token, content_length)).encode()


class HealthAndAuthTests(ConformanceCase):

    def test_health_is_public_and_versioned(self):
        st, _, body = self.t.api(None, "GET", "/health", auth=False)
        self.assertEqual(st, 200)
        self.assertEqual(body, {"ok": True, "version": harness.SPEC_VERSION})

    def test_missing_or_wrong_token_is_401(self):
        self.assertError(self.t.api("koda", "GET", "/v1/threads",
                                    auth=False), 401)
        self.assertError(self.t.api("koda", "GET", "/v1/threads",
                                    token="not-the-bus-token"), 401)

    def test_unknown_route_is_404(self):
        self.assertError(self.t.api("koda", "GET", "/v1/nope"), 404)

    def test_malformed_json_is_400(self):
        self.assertError(self.t.api("koda", "POST", "/v1/messages",
                                    raw_body=b'{"thread_id": '), 400)


class PostValidationTests(ConformanceCase):

    def base(self, **over):
        body = {"thread_id": harness.unique("val"), "from": "koda",
                "to": "instinct", "type": "note",
                "body": "Quarterly numbers look right."}
        body.update(over)
        return body

    def post_raw(self, body):
        return self.t.api("koda", "POST", "/v1/messages", body=body)

    def test_required_fields_missing_is_422(self):
        for field in ("thread_id", "from", "to", "type", "body"):
            with self.subTest(field=field):
                body = self.base()
                del body[field]
                self.assertError(self.post_raw(body), 422)

    # Edge function accepts whitespace-only values: fix/serialization-parity.
    @known_bug("supabase", "fix/serialization-parity")
    def test_blank_required_fields_are_422(self):
        for field in ("thread_id", "from", "to", "type", "body"):
            self.assertError(self.post_raw(self.base(**{field: "  "})), 422)
        self.assertError(self.post_raw(self.base(idempotency_key="   ")), 422)

    def test_field_types_and_formats_are_422(self):
        cases = {
            "unknown type": {"type": "shout"},
            "metadata is a list": {"metadata": ["x"]},
            "metadata is a string": {"metadata": "x"},
            "reply_to not a string": {"reply_to": 7},
            "body not a string": {"body": {"text": "hi"}},
            "idempotency_key not a string": {"idempotency_key": 7},
            "idempotency_key too long": {"idempotency_key": "k" * 129},
        }
        for label, over in cases.items():
            with self.subTest(label):
                self.assertError(self.post_raw(self.base(**over)), 422)

    # Edge function does not check kebab-case ids: fix/serialization-parity.
    @known_bug("supabase", "fix/serialization-parity")
    def test_agent_ids_must_be_kebab_case(self):
        self.assertError(self.post_raw(self.base(**{"from": "Not Kebab"})),
                         422)
        self.assertError(self.post_raw(self.base(to="not_kebab")), 422)
        created = self.post_ok("koda", thread_id=harness.unique("kebab"),
                               to="instinct", body="Receipt target.")
        self.assertError(self.t.api("instinct", "POST", "/v1/receipts", body={
            "message_id": created["id"], "agent": "Not Kebab",
            "status": "received"}), 422)

    # Edge function stores link messages without a URL:
    # fix/serialization-parity.
    @known_bug("supabase", "fix/serialization-parity")
    def test_link_requires_one_time_link_url(self):
        for metadata in ({}, {"one_time_link": {"expires_at":
                                                harness.utc_iso(600)}}):
            with self.subTest(metadata=metadata):
                self.assertError(self.post_raw(self.base(
                    type="link", metadata=metadata)), 422)

    # Reference server accepts any attachments value:
    # fix/serialization-parity.
    @known_bug("python", "fix/serialization-parity")
    def test_attachments_validation(self):
        good = {"name": "leads.csv", "url": "https://files.example.com/l.csv",
                "mime": "text/csv", "size": 18234}
        self.assertCreated(self.post_raw(self.base(
            metadata={"attachments": [good]})))
        bad = {
            "not an array": {"name": "x"},
            "missing name": [dict(good, name="")],
            "non-http url": [dict(good, url="ftp://files.example.com/x")],
            "negative size": [dict(good, size=-1)],
            "mime not a string": [dict(good, mime=5)],
        }
        for label, att in bad.items():
            self.assertError(self.post_raw(self.base(
                metadata={"attachments": att})), 422)

    # Edge function rejects "metadata": null with 422:
    # fix/serialization-parity.
    @known_bug("supabase", "fix/serialization-parity")
    def test_null_metadata_means_no_metadata(self):
        self.assertCreated(self.post_raw(self.base(metadata=None)))

    # Edge function answers 500 to a JSON null body:
    # fix/serialization-parity.
    @known_bug("supabase", "fix/serialization-parity")
    def test_non_object_json_body_is_422(self):
        for path in ("/v1/messages", "/v1/receipts"):
            for raw in (b"null", b"[1]"):
                self.assertError(self.t.api("koda", "POST", path,
                                            raw_body=raw), 422)

    # Postgres cannot store U+0000; the edge function answers 500:
    # fix/supabase-hardening.
    @known_bug("supabase", "fix/supabase-hardening")
    def test_nul_character_is_not_a_server_error(self):
        thread = harness.unique("nul")
        for over in ({"body": "bad\u0000byte"},
                     {"metadata": {"x_note": "a\u0000b"}}):
            st, _, body = self.post_raw(self.base(thread_id=thread, **over))
            self.assertIn(st, (201, 422), "NUL gave %d %r" % (st, body))


class SizeLimitTests(ConformanceCase):

    def post(self, **fields):
        body = {"thread_id": harness.unique("size"), "from": "koda",
                "to": "instinct", "type": "note", "body": "Size check."}
        body.update(fields)
        return self.t.api("koda", "POST", "/v1/messages", body=body)

    def test_body_exactly_20kb_accepted_one_more_byte_413(self):
        # 2-byte UTF-8 characters: the limit is in bytes, not characters.
        self.assertEqual(self.post(body="\u00e9" * 10240)[0], 201)
        self.assertError(self.post(body="\u00e9" * 10240 + "x"), 413)

    def test_metadata_over_16kb_is_413(self):
        self.assertEqual(self.post(metadata={"x_pad": "m" * 15000})[0], 201)
        self.assertError(self.post(metadata={"x_pad": "m" * 16384}), 413)

    def test_idempotency_key_128_accepted_129_rejected(self):
        self.assertEqual(self.post(idempotency_key=harness.unique("k")
                                   .ljust(128, "k"))[0], 201)
        self.assertError(self.post(idempotency_key="k" * 129), 422)


class SerializationTests(ConformanceCase):

    def test_created_response_shape(self):
        body = self.post_ok("koda", thread_id=harness.unique("shape"),
                            to="instinct", body="Shape check.")
        self.assertEqual(set(body), {"id", "created_at"})
        self.assertRegex(body["id"], ID_RE)
        self.assertRegex(body["created_at"], TS_RE)

    def test_message_round_trips_every_field(self):
        thread = harness.unique("roundtrip")
        parent = self.post_ok("instinct", thread_id=thread, to="koda",
                              type="question", body="Ship Friday?")
        meta = {"x_vendor_priority": 2, "nested": {"ok": True}}
        created = self.post_ok("koda", thread_id=thread, to="instinct",
                               type="decision", body="Yes, Friday.",
                               reply_to=parent["id"], metadata=meta)
        msg = self.find("instinct", thread, created["id"])
        self.assertEqual(set(msg), MESSAGE_KEYS)
        self.assertEqual(
            {k: msg[k] for k in ("id", "thread_id", "from", "to", "type",
                                 "body", "reply_to", "metadata",
                                 "created_at", "receipts")},
            {"id": created["id"], "thread_id": thread, "from": "koda",
             "to": "instinct", "type": "decision", "body": "Yes, Friday.",
             "reply_to": parent["id"], "metadata": meta,
             "created_at": created["created_at"], "receipts": []})

    # Reference server omits unset reply_to/metadata:
    # fix/serialization-parity.
    @known_bug("python", "fix/serialization-parity")
    def test_unset_optional_fields_are_null_and_empty(self):
        thread = harness.unique("shape")
        created = self.post_ok("koda", thread_id=thread, to="instinct",
                               body="Plain note, no extras.")
        msg = self.find("instinct", thread, created["id"])
        self.assertEqual(set(msg), MESSAGE_KEYS)
        self.assertIsNone(msg["reply_to"])
        self.assertEqual(msg["metadata"], {})

    def test_body_is_stored_verbatim(self):
        thread = harness.unique("verbatim")
        text = "  Leading spaces, emoji \U0001F680, and trailing newline\n"
        created = self.post_ok("koda", thread_id=thread, to="instinct",
                               body=text)
        self.assertEqual(self.find("instinct", thread,
                                   created["id"])["body"], text)


class PollingTests(ConformanceCase):

    def test_cursor_pagination_and_echo(self):
        thread = harness.unique("pages")
        ids = [self.post_ok("koda", thread_id=thread, to="*",
                            body="batch item %d of 3" % i)["id"]
               for i in range(3)]
        p1 = self.messages("instinct", thread_id=thread, limit=2)
        self.assertEqual([m["id"] for m in p1["messages"]], ids[:2])
        p2 = self.messages("instinct", thread_id=thread, limit=2,
                           since=p1["next_cursor"])
        self.assertEqual([m["id"] for m in p2["messages"]], ids[2:])
        p3 = self.messages("instinct", thread_id=thread,
                           since=p2["next_cursor"])
        self.assertEqual(p3["messages"], [])
        self.assertEqual(p3["next_cursor"], p2["next_cursor"])

    # Edge function returns next_cursor null on a first empty poll:
    # fix/serialization-parity.
    @known_bug("supabase", "fix/serialization-parity")
    def test_first_empty_poll_returns_string_cursor(self):
        thread = harness.unique("empty")
        batch = self.messages("instinct", thread_id=thread)
        self.assertEqual(batch["messages"], [])
        self.assertIsInstance(batch["next_cursor"], str)
        created = self.post_ok("koda", thread_id=thread, to="instinct",
                               body="First message after the empty poll.")
        again = self.messages("instinct", thread_id=thread,
                              since=batch["next_cursor"])
        self.assertEqual([m["id"] for m in again["messages"]],
                         [created["id"]])

    def test_query_validation(self):
        for params in ({"since": "not-a-cursor"}, {"limit": "0"},
                       {"limit": "101"}, {"limit": "ten"}, {"wait": "61"},
                       {"wait": "-1"}):
            with self.subTest(params=params):
                self.assertError(self.t.poll("instinct", **params), 422)

    def test_limit_one_and_wait_zero_accepted(self):
        thread = harness.unique("lim")
        for i in range(2):
            self.post_ok("koda", thread_id=thread, to="instinct",
                         body="Row %d" % i)
        self.assertEqual(len(self.messages("instinct", thread_id=thread,
                                           limit=1)["messages"]), 1)
        t0 = time.monotonic()
        self.assertEqual(self.messages("instinct",
                                       thread_id=harness.unique("w0"),
                                       wait=0)["messages"], [])
        self.assertLess(time.monotonic() - t0, 2.0, "wait=0 must not park")

    def test_default_filter_is_to_me_or_broadcast(self):
        thread = harness.unique("filter")
        mine = self.post_ok("koda", thread_id=thread, to="instinct",
                            body="for instinct")
        other = self.post_ok("koda", thread_id=thread, to="grok-scout",
                             body="for grok")
        allhands = self.post_ok("koda", thread_id=thread, to="*",
                                body="for everyone")
        ids = [m["id"] for m in
               self.messages("instinct", thread_id=thread)["messages"]]
        self.assertEqual(ids, [mine["id"], allhands["id"]])
        self.assertNotIn(other["id"], ids)

    def test_explicit_to_filter(self):
        thread = harness.unique("to-filter")
        self.post_ok("koda", thread_id=thread, to="instinct", body="one")
        other = self.post_ok("koda", thread_id=thread, to="grok-scout",
                             body="two")
        ids = [m["id"] for m in self.messages(
            "instinct", thread_id=thread, to="grok-scout")["messages"]]
        self.assertEqual(ids, [other["id"]])


class ReceiptTests(ConformanceCase):

    def test_receipt_recorded_and_idempotent(self):
        thread = harness.unique("rcpt")
        mid = self.post_ok("koda", thread_id=thread, to="instinct",
                           type="task", body="Dedupe the lead sheet.")["id"]
        for _ in range(2):
            st, _, body = self.t.receipt("instinct", mid, "received")
            self.assertEqual((st, body), (201, {"ok": True}))
        receipts = self.find("koda", thread, mid, to="instinct")["receipts"]
        self.assertEqual([(r["agent"], r["status"]) for r in receipts],
                         [("instinct", "received")])
        self.assertRegex(receipts[0]["at"], TS_RE)

    def test_receipt_validation(self):
        self.assertError(self.t.receipt("instinct", "msg_doesnotexist",
                                        "received"), 404)
        mid = self.post_ok("koda", thread_id=harness.unique("rcpt-val"),
                           to="instinct", body="Validate me.")["id"]
        self.assertError(self.t.receipt("instinct", mid, "read"), 422)
        for missing in ("message_id", "agent", "status"):
            with self.subTest(missing=missing):
                body = {"message_id": mid, "agent": "instinct",
                        "status": "received"}
                del body[missing]
                self.assertError(self.t.api("instinct", "POST",
                                            "/v1/receipts", body=body), 422)

    # Reference server says "unknown message_id": fix/serialization-parity.
    @known_bug("python", "fix/serialization-parity")
    def test_unknown_message_404_uses_spec_text(self):
        st, _, body = self.t.receipt("instinct", "msg_doesnotexist",
                                     "received")
        self.assertEqual((st, body), (404, {"error": "message not found"}))

    # One row per (message, agent): a later status overwrites an earlier
    # one. fix/receipts-no-downgrade.
    @known_bug("both", "fix/receipts-no-downgrade")
    def test_later_receipt_does_not_erase_earlier(self):
        thread = harness.unique("rcpt-log")
        mid = self.post_ok("koda", thread_id=thread, to="instinct",
                           type="task", body="Validate 312 new leads.")["id"]
        for status in ("received", "acted", "received"):
            self.assertEqual(self.t.receipt("instinct", mid, status)[0], 201)
            time.sleep(0.02)
        receipts = self.find("koda", thread, mid, to="instinct")["receipts"]
        self.assertEqual([(r["agent"], r["status"]) for r in receipts],
                         [("instinct", "received"), ("instinct", "acted")])

    # Reference server returns receipts in primary-key order, not by `at`:
    # fix/receipts-no-downgrade.
    @known_bug("python", "fix/receipts-no-downgrade")
    def test_receipts_ordered_by_time_across_agents(self):
        thread = harness.unique("rcpt-order")
        mid = self.post_ok("koda", thread_id=thread, to="*",
                           body="All hands: confirm the new schedule.")["id"]
        # Time order is the reverse of alphabetical order.
        for agent in ("instinct", "grok-scout"):
            self.assertEqual(self.t.receipt(agent, mid, "received")[0], 201)
            time.sleep(0.02)
        receipts = self.find("koda", thread, mid)["receipts"]
        self.assertEqual([r["agent"] for r in receipts],
                         ["instinct", "grok-scout"])


class ThreadTests(ConformanceCase):

    def test_unread_counts_are_per_agent(self):
        thread = harness.unique("unread")
        self.post_ok("koda", thread_id=thread, to="*", body="broadcast 1")
        direct = self.post_ok("koda", thread_id=thread, to="instinct",
                              body="direct 1")
        self.assertEqual(self.thread_row("instinct", thread)["unread"], 2)
        self.assertEqual(self.thread_row("grok-scout", thread)["unread"], 1)
        self.t.receipt("instinct", direct["id"], "received")
        self.assertEqual(self.thread_row("instinct", thread)["unread"], 1)

    def test_resolve_and_reopen(self):
        thread = harness.unique("resolve")
        self.post_ok("koda", thread_id=thread, to="instinct",
                     type="question", body="Are the drafts final?")
        t = self.thread_row("koda", thread)
        self.assertEqual(set(t), {"thread_id", "last_at", "unread",
                                  "status", "resolved_at"})
        self.assertEqual((t["status"], t["resolved_at"]), ("open", None))
        res = self.post_ok("koda", thread_id=thread, to="instinct",
                           type="resolve", body="Answered, closing.")
        t = self.thread_row("instinct", thread)
        self.assertEqual((t["status"], t["resolved_at"]),
                         ("resolved", res["created_at"]))
        self.assertEqual(t["last_at"], res["created_at"])
        self.post_ok("instinct", thread_id=thread, to="koda",
                     body="One more thing.")
        t = self.thread_row("koda", thread)
        self.assertEqual((t["status"], t["resolved_at"]), ("open", None))


class IdempotencyTests(ConformanceCase):

    def test_replay_returns_the_original(self):
        thread = harness.unique("idem")
        key = harness.unique("send")
        first = self.post_ok("koda", thread_id=thread, to="instinct",
                             body="Invoice #1182 approved.",
                             idempotency_key=key)
        st, _, dup = self.t.post("koda", thread_id=thread, to="instinct",
                                 body="Invoice #1182 approved.",
                                 idempotency_key=key)
        self.assertEqual((st, dup), (200, {"id": first["id"],
                                           "created_at": first["created_at"],
                                           "duplicate": True}))
        msgs = self.messages("instinct", thread_id=thread)["messages"]
        self.assertEqual([m["id"] for m in msgs], [first["id"]])

    def test_keys_are_scoped_per_sender(self):
        thread = harness.unique("idem-scope")
        key = harness.unique("shared")
        a = self.post_ok("koda", thread_id=thread, to="*", body="from koda",
                         idempotency_key=key)
        b = self.post_ok("instinct", thread_id=thread, to="*",
                         body="from instinct", idempotency_key=key)
        self.assertNotEqual(a["id"], b["id"])

    # Reuse with a different payload is answered as a duplicate and the new
    # content is dropped: fix/idempotency-key-conflict.
    @known_bug("both", "fix/idempotency-key-conflict")
    def test_same_key_different_payload_is_409(self):
        thread = harness.unique("idem-conflict")
        key = harness.unique("send")
        self.post_ok("koda", thread_id=thread, to="instinct",
                     body="Invoice #1182 approved.", idempotency_key=key)
        self.assertError(self.t.post("koda", thread_id=thread, to="instinct",
                                     body="Invoice #1182 REJECTED.",
                                     idempotency_key=key), 409)
        msgs = self.messages("instinct", thread_id=thread)["messages"]
        self.assertEqual([m["body"] for m in msgs],
                         ["Invoice #1182 approved."])


class RateLimitTests(ConformanceCase):
    """SPEC: 60 requests per minute per token, headers on every response.
    Each test starts with a fresh budget (see ConformanceCase.setUp)."""

    HEADERS = ("x-ratelimit-limit", "x-ratelimit-remaining",
               "x-ratelimit-reset")

    def test_rate_limit_headers_on_every_response(self):
        for label, resp in (
                ("health", self.t.api(None, "GET", "/health", auth=False)),
                ("200", self.t.api("koda", "GET", "/v1/threads")),
                ("401", self.t.api("koda", "GET", "/v1/threads",
                                   auth=False))):
            with self.subTest(label):
                for h in self.HEADERS:
                    self.assertIn(h, resp[1])
                self.assertEqual(resp[1]["x-ratelimit-limit"],
                                 str(harness.RATE_LIMIT))

    def test_request_over_the_limit_is_429_with_retry_after(self):
        statuses = [self.t.api("koda", "GET", "/v1/threads")[0]
                    for _ in range(harness.RATE_LIMIT)]
        self.assertEqual(statuses, [200] * harness.RATE_LIMIT)
        resp = self.t.api("koda", "GET", "/v1/threads")
        self.assertError(resp, 429)
        hdrs = resp[1]
        self.assertGreaterEqual(int(hdrs["retry-after"]), 1)
        self.assertLessEqual(int(hdrs["retry-after"]), 61)
        self.assertEqual(hdrs["x-ratelimit-remaining"], "0")

    # Regression test for #3: a bad token does not consume the budget.
    def test_unauthenticated_requests_do_not_consume_budget(self):
        codes = {self.t.api("koda", "GET", "/v1/threads",
                            token="not-the-bus-token")[0]
                 for _ in range(harness.RATE_LIMIT + 1)}
        self.assertEqual(codes, {401})
        st, _, body = self.t.api("koda", "GET", "/v1/threads")
        self.assertEqual(st, 200, "token holder throttled by requests "
                                  "without the token: %r" % body)


class ProtocolTests(ConformanceCase):

    # A non-ASCII bearer token crashes the reference server's comparison:
    # fix/bounded-request-reads.
    @known_bug("python", "fix/bounded-request-reads")
    def test_non_ascii_token_is_401(self):
        self.assertError(self.t.api("koda", "GET", "/v1/threads",
                                    token="t\u00f6k\u00e9n-caf\u00e9"), 401)

    # Content-Length: -1 blocks a reference-server worker until the client
    # hangs up: fix/bounded-request-reads.
    @known_bug("python", "fix/bounded-request-reads")
    def test_negative_content_length_is_400_at_once(self):
        t0 = time.monotonic()
        line = harness.raw_status_line(self.t.base_url, post_head(self.t, -1))
        self.assertIsNotNone(line, "no response within 4 s")
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertRegex(line, r"^HTTP/1\.[01] 400")


if harness.target_name() == "python":

    class RequestSizeTests(ConformanceCase):
        """Reference server only: the hosted edge platform bounds request
        bodies itself, so the edge function has no equivalent check."""

        # The reference server waits for a declared body of any size:
        # fix/bounded-request-reads.
        @known_bug("python", "fix/bounded-request-reads")
        def test_oversize_content_length_is_413_before_reading(self):
            line = harness.raw_status_line(
                self.t.base_url, post_head(self.t, 10 * 1024 * 1024))
            self.assertIsNotNone(line, "server waited for the body")
            self.assertRegex(line, r"^HTTP/1\.[01] 413")
