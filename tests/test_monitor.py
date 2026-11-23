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
        self.assertIn("No new verified announcements", monitor.report(TODAY, [], []))
        with self.assertRaises(monitor.SourceError):
            monitor.read_html(b"<title>Just a moment...</title><form id='challenge-form' />")
        digest = monitor.report(TODAY, [], [("Honda", "ServiceExpress", "HTTP 503",
                                             "https://example.com/")])
        self.assertIn("OEM outage status is unverified", digest)
        self.assertNotIn("Service disruption", digest)

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
        monitor.deliver(webhook, "A" * 2100, TODAY, 1, 0)
        kwargs = post.call_args.kwargs
        self.assertIn("files[0]", kwargs["files"])
        self.assertEqual(json.loads(kwargs["data"]["payload_json"])["allowed_mentions"], {"parse": []})
        post.side_effect = requests.ConnectionError(webhook)
        with self.assertRaisesRegex(RuntimeError, "^Discord delivery could not be confirmed$"):
            monitor.deliver(webhook, "test", TODAY, 1, 0)


if __name__ == "__main__":
    unittest.main()
