from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import requests

import monitor

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
            self.assertEqual([label for _, label in monitor.changes(found, state, TODAY)], ["Changed"])

    def test_older_dated_item_is_baselined(self):
        entry = monitor.Notice("Mazda", "Update", "Old version", "A release",
                               "https://example.com", "2026-01-01")
        state = {"seen": {}}
        self.assertEqual(monitor.changes([entry], state, TODAY), [])
        self.assertIn(entry.key, state["seen"])

    def test_access_gap_and_clean_no_change_report(self):
        with self.assertRaises(monitor.SourceError):
            monitor.read_html(b"<title>Just a moment...</title><form id='challenge-form' />")
        embeds = monitor.report(TODAY, [], [("Honda", "ServiceExpress", "HTTP 503",
                                              "https://example.com/")])[0]
        self.assertIn("No new verified announcements", embeds[1]["description"])
        self.assertIn("**Honda** · Unverified", embeds[0]["description"])
        self.assertNotIn("**Honda** · Reported Issue", embeds[0]["description"])
        self.assertEqual(embeds[-1]["title"], "Public Source Coverage")

    def test_dashboard_uses_positive_evidence_and_current_incidents(self):
        status_url = "https://example.com/status"
        incident = monitor.Notice("Chrysler / Stellantis", "Service disruption",
                                  "Login issue", "Investigating", status_url,
                                  status="investigating", active=True)
        future = monitor.Notice("Ford", "Scheduled maintenance", "Next week", "",
                                status_url, status="scheduled", active=True)
        evidence = [("Chrysler / Stellantis", "Operational", status_url),
                    ("GM", "Operational", status_url),
                    ("Audi", "Operational", status_url)]
        rows = {name: value for name, value, _ in monitor.dashboard(evidence, [incident, future])}
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows["Stellantis"], "Reported Issue")
        self.assertEqual(rows["GM"], "Operational")
        self.assertEqual(rows["Ford"], "Unverified")
        self.assertEqual(rows["Volkswagen/Audi"], "Unverified")
        self.assertEqual(monitor.status_evidence('{"status":{"indicator":"none"}}'), "Operational")
        self.assertEqual(monitor.status_evidence('{"status":{"indicator":"major"}}'), "Reported Issue")
        with self.assertRaises(monitor.SourceError):
            monitor.status_evidence('{"status":{"indicator":"unknown"}}')

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
        batches = monitor.report(TODAY, pending, [])
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
                    self.assertIn("[Official source](https://example.com/item/", field["value"])
                    total += monitor.units(field["name"]) + monitor.units(field["value"])
                    seen.append(field["name"])
            self.assertLessEqual(total, 6000)
        self.assertEqual(len(set(seen)), 64)
        self.assertEqual(categories, {"OEM News", "Software Releases",
                                      "Technical Service Updates", "Announcements"})

    def test_long_article_link_is_kept_in_a_dedicated_embed(self):
        url = "https://example.com/article?id=" + ("x" * 1050) + "(release)"
        notice = monitor.Notice("GM", "Software or technical update", "Software release",
                               "Addresses a connection problem.", url, "2026-11-22")
        embeds = [embed for batch in monitor.report(TODAY, [(notice, "New")], [])
                  for embed in batch]
        self.assertTrue(embeds[2]["title"].startswith("Software Releases"))
        self.assertIn(url.replace("(release)", "%28release%29"), embeds[2]["description"])

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
        self.assertEqual(monitor.section_for(notice), "Announcements")
        self.assertEqual(notice.published, "2021-09-18")
        self.assertNotIn("views", notice.description)

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
