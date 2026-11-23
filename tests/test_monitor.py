from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import requests

import monitor
from evidence import Status, statuspage, topix

TODAY = date(2026, 11, 23)


class MonitorTests(unittest.TestCase):
    def test_markup_notice_and_no_false_subscription_update(self):
        html = b"""
          <main><p>Generic documentation and details about configuring the workshop system.</p>
          <p>Ask your administrator for diagnostic software updates instructions and pricing.</p>
          <p id="release">Version IDS 145.01, released 11/22/2026. Includes a fix for log collection.</p></main>
        """
        source = {"oem": "Mazda", "name": "IDS"}
        found, errors = monitor.html_notices(
            None, source, {"example.com"}, "https://example.com/ids", html
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].published, "2026-11-22")
        self.assertTrue(found[0].url.endswith("#release"))

    def test_status_incident_revision_and_deduplication_across_runs(self):
        item = {"id": "abc", "shortlink": "https://example.com/incidents/abc",
                "name": "Some aftermarket users cannot log in", "created_at": "2026-11-22T09:00:00Z",
                "status": "investigating", "incident_updates": [
                    {"body": "Working on a fix", "created_at": "2026-11-23T11:00:00Z"},
                    {"body": "Looking into reports", "created_at": "2026-11-23T10:00:00Z"},
                ]}
        source = {"oem": "Chrysler / Stellantis", "kind": "incidents",
                  "url": "https://example.com/feed"}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = monitor.load_state(path)
            found, _ = monitor.status_notices(source, {"example.com"},
                                              json.dumps({"incidents": [item, item]}))
            self.assertEqual(found[0].published, "2026-11-22")
            self.assertEqual(found[0].description, "Working on a fix")
            self.assertEqual([label for _, label in monitor.changes(found, state, TODAY)], ["New"])
            monitor.save_state(path, state)
            state = monitor.load_state(path)
            self.assertEqual(monitor.changes(found, state, TODAY), [])
            item["status"] = "resolved"
            found, _ = monitor.status_notices(source, {"example.com"},
                                              json.dumps({"incidents": [item]}))
            self.assertEqual([label for _, label in monitor.changes(found, state, TODAY)], ["Resolved"])

    def test_older_dated_item_is_baselined(self):
        entry = monitor.Notice("Mazda", "Update", "Old version", "A release",
                               "https://example.com", "2026-01-01")
        state = {"seen": {}}
        self.assertEqual(monitor.changes([entry], state, TODAY), [])
        self.assertIn(entry.key, state["seen"])

    def test_access_gap_and_clean_no_change_report(self):
        with self.assertRaises(monitor.SourceError):
            monitor.read_html(b"<title>Just a moment...</title><form id='challenge-form' />")
        self.assertEqual(monitor.report(TODAY, []), [monitor.QUIET_MESSAGE])
        self.assertEqual(monitor.report(TODAY, [], quiet=False), [])

    def test_statuspage_distinguishes_operational_partial_and_complete_outage(self):
        source = {"oem": "Chrysler / Stellantis", "public_url": "https://example.com/status"}
        checked = "2026-11-23T12:00:00+00:00"
        payload = {"status": {"indicator": "none"}, "components": [
            {"id": "flash", "name": "ECU Flash", "status": "operational"},
            {"id": "login", "name": "Login services", "status": "operational"},
        ]}
        normal = statuspage(source, payload, checked)
        self.assertEqual(normal.status, "Operational")
        payload["scheduled_maintenances"] = [{"name": "Upcoming maintenance", "status": "scheduled"}]
        self.assertEqual(statuspage(source, payload, checked).status, "Reported Issue")
        payload["scheduled_maintenances"] = []
        payload["components"][0]["status"] = "partial_outage"
        degraded = statuspage(source, payload, checked)
        self.assertEqual(degraded.status, "Reported Issue")
        self.assertIn("ECU Flash", degraded.critical[0])
        payload["status"]["indicator"] = "critical"
        payload["components"][0]["status"] = "major_outage"
        self.assertEqual(statuspage(source, payload, checked).status, "Reported Issue")
        payload["components"][1]["status"] = "major_outage"
        outage = statuspage(source, payload, checked)
        self.assertEqual(outage.status, "Outage")
        self.assertEqual(monitor.component_findings(normal), [])
        notice, = monitor.component_findings(outage)
        self.assertTrue(notice.critical)
        self.assertEqual(monitor.section_for(notice), "OEM Service Alerts")

    def test_topix_programming_issue_is_scoped_and_ordinary_page_is_unverified(self):
        checked = "2026-11-23T12:00:00+00:00"
        source = {"oem": "JLR", "url": "https://example.com/status"}
        html = b"""<div><p>Current network status of the public TOPIx system and supported workshop services.</p>
        <section class="headless"><h2>TOPIx</h2>
        <span class="system-status status-warning">1</span></section>
        <section><header>Notice</header><div class="notice-title">SOTA</div>
        <div class="notice-details">We are currently experiencing SOTA server issues.
        Programming buttons are inoperative for selected vehicles.</div></section></div>"""
        signal = topix(source, monitor.read_html(html), checked)
        self.assertEqual(signal.status, "Reported Issue")
        self.assertIn("selected vehicles", signal.critical[0])
        with self.assertRaises(ValueError):
            topix(source, monitor.read_html(html.replace(b'system-status', b'generic')), checked)

    def test_recovery_requires_positive_evidence_and_is_not_repeated(self):
        checked = "2026-11-23T12:00:00+00:00"
        issue = Status("JLR", "Reported Issue", "https://example.com/status",
                       checked, "TOPIx", ("SOTA programming unavailable",))
        state = {"seen": {}}
        self.assertEqual(monitor.recovery_notices([issue], [], state), [])
        self.assertEqual(monitor.recovery_notices([], [], state), [])
        normal = monitor.replace(issue, status="Operational", critical=())
        recovery, = monitor.recovery_notices([normal], [], state)
        self.assertEqual(monitor.section_for(recovery), "Resolved Issues")
        self.assertFalse(recovery.published)
        self.assertEqual(recovery.observed, "2026-11-23")
        self.assertEqual(monitor.recovery_notices([normal], [], state), [])

    @patch("monitor.check_source")
    def test_failed_retrieval_keeps_history_without_publishing_stale_status(self, check_source):
        source = {"oem": "JLR", "name": "TOPIx", "url": "https://example.com/status", "kind": "topix"}
        checked = "2026-11-23T12:00:00+00:00"
        operational = Status("JLR", "Operational", source["url"], checked, "TOPIx")
        check_source.side_effect = [([], operational, checked, []), ([], None, "", ["SSLError"])]
        state = {}
        self.assertEqual(monitor.collect([source], state)[1], [operational])
        self.assertEqual(monitor.collect([source], state)[1], [])
        record = state["sources"][source["url"]]
        self.assertEqual(record["last_status"], "Operational")
        self.assertEqual(record["last_verified"], checked)
        self.assertEqual(record["last_retrieved"], checked)
        self.assertEqual(record["errors"], ["SSLError"])

    def test_archive_excludes_stale_and_undated_discoveries(self):
        state = {"seen": {}}
        old = monitor.Notice("GM", "Technical", "Stale", "Old body",
                             "https://example.com/old", "2026-10-01")
        undated = monitor.Notice("GM", "Technical", "Undated", "Old body",
                                 "https://example.com/undated")
        self.assertEqual(monitor.changes([old, undated], state, TODAY), [])
        self.assertEqual(len(state["archive"]), 2)
        revised = monitor.replace(old, description="Important new change", updated="2026-11-22")
        self.assertEqual(monitor.changes([revised], state, TODAY), [(revised, "Changed")])
        self.assertEqual(monitor.changes([revised], state, TODAY), [])

    def test_categorized_items_keep_dates_and_direct_links_across_batches(self):
        titles = ["GM Service Know-How Emerging Issues", "Programming tool update",
                  "Technical service bulletin", "New Mopar Technical Service Portal"]
        pending = []
        for number in range(64):
            notice = monitor.Notice(
                "GM", "Software or technical update", titles[number % 4] + f" {number}",
                "🚗" * 480, f"https://example.com/item/{number}", "2026-11-22",
            )
            pending.append((notice, "New"))
        batches = monitor.report(TODAY, pending)
        self.assertGreater(len(batches), 1)
        seen = []
        categories = set()
        for batch in batches:
            self.assertLessEqual(len(batch), 10)
            total = 0
            for embed in batch:
                description = monitor.units(embed.get("description", ""))
                self.assertLessEqual(description, 4096)
                total += monitor.units(embed["title"]) + description
                if "fields" in embed:
                    categories.add(embed["title"].split(" · ")[0])
                for field in embed.get("fields", []):
                    self.assertFalse(field["inline"])
                    self.assertLessEqual(monitor.units(field["name"]), 256)
                    self.assertLessEqual(monitor.units(field["value"]), 1024)
                    self.assertIn("2026-11-22", field["value"])
                    self.assertIn("[Original source](https://example.com/item/", field["value"])
                    total += monitor.units(field["name"]) + monitor.units(field["value"])
                    seen.append(field["name"])
            self.assertLessEqual(total, 6000)
        self.assertEqual(len(set(seen)), 64)
        self.assertEqual(categories, {"Software & Technical Updates"})

    def test_long_article_link_is_kept_in_a_dedicated_embed(self):
        url = "https://example.com/article?id=" + ("x" * 1050) + "(release)"
        notice = monitor.Notice("GM", "Software or technical update", "Software release",
                               "Addresses a connection problem.", url, "2026-11-22")
        embeds = [embed for batch in monitor.report(TODAY, [(notice, "New")])
                  for embed in batch]
        self.assertTrue(embeds[0]["title"].startswith("Software & Technical Updates"))
        self.assertIn(url.replace("(release)", "%28release%29"), embeds[0]["description"])

    @patch("monitor.fetch")
    def test_article_reads_selected_headline_date_and_body(self, fetch):
        url = "https://example.com/news"
        fetch.return_value = (url, b"""
          <main><h1>News</h1><h2>New Mopar Technical Service Portal</h2>
          <div title="Published on Sat, Sep 18, 2021">September 18, 2021</div>
          <div class="views">7,500 views as of today</div>
          <div class="article-content"><p>The portal streamlines management of tools,
          subscriptions and users for aftermarket repair businesses.</p></div></main>
        """)
        source = {"oem": "Chrysler / Stellantis", "title_selector": "h2",
                  "date_selector": "[title^=Published]", "body_selector": ".article-content"}
        notice = monitor.article_notice(None, source, {"example.com"}, url, "Headline", "Update")
        self.assertEqual(monitor.section_for(notice), "Software & Technical Updates")
        self.assertEqual(notice.published, "2021-09-18")
        self.assertNotIn("views", notice.description)

    def test_community_atom_filters_and_keeps_evidence_separate(self):
        feed = b"""<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><title>Forum &#8226; FDRS login error</title>
          <link href="https://example.com/viewtopic.php?p=104"/>
          <published>2026-11-23T01:00:00Z</published>
          <content type="html">I get an authentication error when I try to sign in.</content></entry>
          <entry><title>FORScan connection error</title>
          <link href="https://example.com/viewtopic.php?p=105"/>
          <published>2026-11-23T01:00:00Z</published>
          <content>I get an adapter error.</content></entry>
          <entry><title>FDRS servers down?</title>
          <link href="https://example.com/viewtopic.php?p=106"/>
          <published>2026-11-23T01:00:00Z</published>
          <content type="html">&lt;blockquote&gt;I get an error.&lt;/blockquote&gt; Is this true?</content></entry>
        </feed>"""
        rules = monitor.RobotFileParser()
        rules.parse(["User-agent: *", "Allow: /"])
        source = {"community": "forscan"}
        found, errors = monitor.community_notices(
            None, source, {"example.com"}, "https://example.com/app.php/feed",
            feed, rules,
        )
        self.assertEqual(errors, [])
        notice, = found
        self.assertEqual(notice.published, "2026-11-22")
        self.assertEqual(notice.service, "FDRS")
        self.assertEqual(notice.evidence, "Community Reported")
        embed, = monitor.report(TODAY, [(notice, "New")])[0]
        self.assertEqual(embed["title"], "Community Reports")
        self.assertIn("Scope unconfirmed", embed["fields"][0]["value"])
        self.assertNotIn("Officially Confirmed", embed["fields"][0]["value"])

    def test_same_official_discovery_is_shared_across_delivery_modes(self):
        incident = monitor.Notice(
            "Ford", "Service alert", "FDRS outage", "Programming is unavailable.",
            "https://example.com/notice", active=True, service="FDRS", critical=True,
        )
        state = {"seen": {}}
        pending = monitor.changes([incident], state, TODAY)
        field = monitor.report(TODAY, pending, quiet=False)[0][0]["fields"][0]
        self.assertIn("Officially Confirmed", field["value"])
        self.assertIn("Observed 2026-11-23", field["value"])
        self.assertEqual(monitor.changes([incident], state, TODAY), [])

    def test_repeat_symptom_can_reopen_after_confirmed_restoration(self):
        url = "https://example.com/status"
        issue = Status("JLR", "Reported Issue", url, "2026-11-23T12:00:00+00:00", "TOPIx",
                       affected=("Programming unavailable",),
                       findings=(("SOTA issue", "Programming is unavailable.", True),))
        state = {"seen": {}}
        findings = monitor.component_findings(issue)
        self.assertTrue(monitor.changes(monitor.mark_live_episodes(findings, [issue], state), state, TODAY))
        self.assertEqual(monitor.changes(monitor.mark_live_episodes(findings, [issue], state), state, TODAY), [])
        monitor.mark_live_episodes([], [monitor.replace(issue, status="Operational")], state)
        self.assertTrue(monitor.changes(monitor.mark_live_episodes(findings, [issue], state), state, TODAY))

    @patch("monitor.requests.post")
    def test_quiet_delivery_uses_exactly_one_plain_message(self, post):
        post.return_value.status_code = 200
        monitor.deliver("https://discord.com/api/webhooks/123/token", monitor.report(TODAY, [])[0])
        self.assertEqual(post.call_args.kwargs["json"], {
            "content": "No new OEM service alerts or technical updates identified today.",
            "allowed_mentions": {"parse": []},
        })

    @patch("monitor.deliver")
    def test_partial_delivery_resumes_at_next_batch(self, deliver):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            first = [{"title": "Dashboard"}]
            second = [{"title": "News"}]
            state = monitor.load_state(path)
            state["delivery"] = {
                "date": TODAY.isoformat(), "messages": [first, second], "next": 0, "items": 1,
            }
            deliver.side_effect = [None, RuntimeError("HTTP failure")]
            with self.assertRaises(RuntimeError):
                monitor.resume_delivery("webhook", path, state)
            state = monitor.load_state(path)
            self.assertEqual(state["delivery"]["next"], 1)
            deliver.reset_mock(side_effect=True)
            monitor.resume_delivery("webhook", path, state)
            deliver.assert_called_once_with("webhook", second)
            self.assertNotIn("delivery", monitor.load_state(path))
            self.assertEqual(monitor.load_state(path)["last_report"], TODAY.isoformat())

    def test_rejects_external_redirect(self):
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.is_redirect = True
        response.headers = {"Location": "https://example.org/login"}
        with self.assertRaises(monitor.SourceError):
            monitor.fetch(session, "https://example.com/feed", {"example.com"})
        session.get.assert_called_once()

    @patch("monitor.requests.post")
    def test_discord_payload_and_secret_safe_error(self, post):
        webhook = "https://discord.com/api/webhooks/123/secret_token"
        post.return_value.status_code = 200
        embeds = [{"title": "OEM Service Monitor"}]
        monitor.deliver(webhook, embeds)
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["json"], {"embeds": embeds, "allowed_mentions": {"parse": []}})
        self.assertNotIn("files", kwargs)
        post.side_effect = requests.ConnectionError(webhook)
        with self.assertRaisesRegex(RuntimeError, "^Discord delivery could not be confirmed$"):
            monitor.deliver(webhook, embeds)


if __name__ == "__main__":
    unittest.main()
