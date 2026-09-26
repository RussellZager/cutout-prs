"""One-time links (SPEC: Metadata conventions, POST /v1/receipts,
Privacy & data rules / Retention)."""

import harness
from base import ConformanceCase, iso_in

URL = "https://accounts.example.com/magic?login=7f3c9a"


class OneTimeLinkTests(ConformanceCase):

    def link(self, msg):
        return msg["metadata"]["one_time_link"]

    def test_consumed_receipt_marks_link_consumed(self):
        thread = harness.unique("link")
        created = self.link_message("koda", "instinct", thread, url=URL)
        before = self.link(self.find("instinct", thread, created["id"]))
        self.assertEqual((before["url"], before["consumed"]), (URL, False))
        st, _, body = self.t.receipt("instinct", created["id"], "consumed")
        self.assertEqual((st, body), (201, {"ok": True}))
        after = self.link(self.find("instinct", thread, created["id"]))
        self.assertIs(after["consumed"], True)

    # Regression test for #4 (URL erased after consume).
    def test_consumed_link_url_is_erased(self):
        thread = harness.unique("link-erase")
        created = self.link_message("koda", "instinct", thread, url=URL)
        self.assertEqual(self.t.receipt("instinct", created["id"],
                                        "consumed")[0], 201)
        for reader, params in (("instinct", {}), ("koda", {"to": "instinct"})):
            link = self.link(self.find(reader, thread, created["id"],
                                       **params))
            self.assertIsNone(link["url"], "%s still reads the URL" % reader)
            self.assertIs(link.get("url_redacted"), True)
        self.assertNotIn("magic?login", str(self.t.raw_metadata(
            created["id"])), "URL still present in storage")

    # Regression test for #4 (expired link URL erased on read).
    def test_expired_link_url_is_erased_on_read(self):
        thread = harness.unique("link-stale")
        created = self.link_message("koda", "instinct", thread, url=URL,
                                    expires_at=harness.utc_iso(-3600))
        link = self.link(self.find("instinct", thread, created["id"]))
        self.assertIsNone(link["url"])
        self.assertIs(link.get("url_redacted"), True)

    def test_purge_marks_expired_links_consumed(self):
        thread = harness.unique("link-purge")
        expired = self.link_message("koda", "instinct", thread, url=URL,
                                    expires_at=harness.utc_iso(-3600))
        live = self.link_message("koda", "instinct", thread, url=URL,
                                 expires_at=harness.utc_iso(3600))
        self.t.run_purge()
        self.assertIs(self.t.raw_metadata(expired["id"])
                      ["one_time_link"]["consumed"], True)
        self.assertIs(self.t.raw_metadata(live["id"])
                      ["one_time_link"]["consumed"], False)

    # Regression test for #2: expiry is compared as a timestamp, so a
    # negative UTC offset does not expire early.
    def test_purge_keeps_unexpired_link_written_with_offset(self):
        thread = harness.unique("link-tz")
        created = self.link_message("koda", "instinct", thread, url=URL,
                                    expires_at=iso_in(3600, offset_hours=-7))
        self.t.run_purge()
        link = self.t.raw_metadata(created["id"])["one_time_link"]
        self.assertIs(link["consumed"], False)
        self.assertEqual(link["url"], URL)

    # SPEC: "Clock skew tolerance: 5 minutes for expiry checks."
    # Regression test for #2.
    def test_purge_honors_five_minute_clock_skew(self):
        thread = harness.unique("link-skew")
        created = self.link_message("koda", "instinct", thread, url=URL,
                                    expires_at=harness.utc_iso(-120))
        self.t.run_purge()
        self.assertIs(self.t.raw_metadata(created["id"])
                      ["one_time_link"]["consumed"], False)

    # Regression test for #5: unparseable expires_at / non-boolean
    # consumed are rejected on write.
    def test_link_expiry_and_consumed_must_be_well_formed(self):
        thread = harness.unique("link-shape")
        good = {"url": URL, "expires_at": harness.utc_iso(600)}
        for label, link in (
                ("expiry not a timestamp", dict(good, expires_at="soon")),
                ("impossible date",
                 dict(good, expires_at="2030-02-30T00:00:00Z")),
                ("consumed not a boolean", dict(good, consumed="no"))):
            st, _, body = self.t.post(
                "koda", thread_id=thread, to="instinct", type="link",
                body="link attempt", metadata={"one_time_link": link})
            if st == 201:  # unfixed server: keep the bad row out of purges
                self.addCleanup(self.t.delete_message, body["id"])
            self.assertEqual(st, 422, "%s: %d %r" % (label, st, body))
        self.assertCreated(self.t.post(
            "koda", thread_id=thread, to="instinct", type="link",
            body="well-formed link", metadata={"one_time_link": good}))

    # Regression test for #5: one malformed stored link must not stop the
    # purge.
    def test_purge_survives_malformed_stored_link(self):
        thread = harness.unique("link-legacy")
        legacy = harness.new_msg_id()
        # Written straight to storage: models a row an older server accepted.
        self.t.insert_raw_message(legacy, thread, {"one_time_link": {
            "url": "https://legacy.example.com/magic?login=old",
            "expires_at": "soon", "consumed": "maybe"}})
        self.addCleanup(self.t.delete_message, legacy)
        old = self.post_ok("koda", thread_id=thread, to="instinct",
                           body="Old note due for purge.")
        self.t.backdate_message(old["id"], 40 * 86400)
        try:
            self.t.run_purge()
        except harness.HarnessError as exc:
            self.fail("purge failed: %s" % exc)
        self.assertFalse(self.t.message_exists(old["id"]),
                         "retention purge did not delete a 40-day message")
        st, _, body = self.t.poll("instinct", thread_id=thread)
        self.assertEqual(st, 200, "stored row broke polling: %r" % body)
