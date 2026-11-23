from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import monitor
import research


class ResearchTests(unittest.TestCase):
    def test_monthly_and_daily_reservations_are_durable(self):
        now = datetime(2026, 11, 23, 14, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "budget.json"
            self.assertTrue(research.reserve(path, "daily", "1", now))
            self.assertFalse(research.reserve(path, "daily", "2", now + timedelta(hours=2)))
            self.assertTrue(research.reserve(path, "preview", "2", now))
            self.assertFalse(research.reserve(path, "preview", "2", now))
            ledger = json.loads(path.read_text())
            ledger["2026-11"].update({f"preview:{index}": "preview" for index in range(3, 10)})
            path.write_text(json.dumps(ledger))
            self.assertFalse(research.reserve(path, "preview", "next", now))
            self.assertTrue(research.reserve(path, "preview", "next", now.replace(month=12)))

    def test_request_is_bounded_and_targets_nissan_and_honda(self):
        payload = research.request_payload(date(2026, 11, 23), {"example.com"})
        self.assertEqual(payload["max_tool_calls"], 12)
        self.assertLessEqual(payload["max_output_tokens"], 4500)
        self.assertFalse(payload["store"])
        self.assertIn("Nissan Infiniti CONSULT", payload["input"])
        self.assertIn("Honda Acura iN", payload["input"])
        self.assertEqual(payload["tools"][0]["filters"]["allowed_domains"], ["example.com"])

    @patch("research.monitor.fetch")
    def test_citation_date_and_exact_evidence_are_required(self, fetch):
        day = date(2026, 11, 23)
        url = "https://example.com/official-notice"
        note = "CONSULT programming services are currently unavailable following a server incident."
        fetch.return_value = (url, f"""
            <meta property="article:published_time" content="2026-11-22T15:00:00Z">
            <article><h1>CONSULT programming outage</h1><p>{note}</p>
            <p>Visit Nissan TechInfo for product coverage and additional repair information.</p>
            </article>
        """.encode())
        candidate = {"oem": "Nissan", "url": url, "published": "2026-11-22", "excerpt": note,
                     "kind": "alert"}
        official = {"example.com": {"Nissan"}}
        unsupported, reason = research.verify(None, candidate, set(), official, day, {})
        self.assertIsNone(unsupported)
        self.assertIn("uncited", reason)
        fetch.assert_not_called()
        notice, reason = research.verify(None, candidate, {url}, official, day, {})
        self.assertFalse(reason)
        self.assertEqual(notice.published, "2026-11-22")
        self.assertEqual(notice.evidence, "Officially Confirmed")
        self.assertTrue(notice.active)
        for change in ({"published": "2026-11-21"},
                       {"excerpt": "CONSULT programming is restored in all regions according to the company."}):
            self.assertIsNone(research.verify(None, candidate | change, {url}, official, day, {})[0])

    def test_candidates_accept_returned_search_sources_and_annotations(self):
        response = {"output": [
            {"type": "web_search_call", "action": {"sources": [{"url": "https://example.com/search"}]}},
            {"type": "message", "content": [{"type": "output_text", "text": '{"findings":[]}',
                                            "annotations": [{"type": "url_citation",
                                                             "url": "https://example.com/cite"}]}]},
        ]}
        self.assertEqual(research.cited_candidates(response),
                         ([], {"https://example.com/search", "https://example.com/cite"}))

    def test_direct_collector_keeps_priority_and_missing_research_falls_back(self):
        original = monitor.Notice("Nissan", "Service alert", "CONSULT outage", "Official wording",
                                  "https://example.com/notice#detail")
        supplemental = monitor.replace(original, url="https://example.com/notice",
                                       description="Supplemental wording")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "research.json"
            notices = [original]
            monitor.add_research(notices, path)
            self.assertEqual(notices, [original])
            path.write_text(json.dumps([asdict(supplemental)]))
            monitor.add_research(notices, path)
            self.assertEqual(notices, [original])

    @patch.dict("os.environ", {"OPENAI_API_KEY": "fake-test-key"})
    @patch("research.requests.post")
    def test_api_error_is_bounded_without_exposing_key_or_response_body(self, post):
        post.return_value.ok = False
        post.return_value.status_code = 429
        post.return_value.json.return_value = {"error": {"code": "rate_limit_exceeded",
                                                         "message": "fake-test-key"}}
        with self.assertRaisesRegex(ValueError, "^Responses API returned HTTP 429 \\(rate_limit_exceeded\\)$"):
            research.search_response(date(2026, 11, 23), {"example.com"})
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
