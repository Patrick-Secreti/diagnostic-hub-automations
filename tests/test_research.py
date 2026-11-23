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
        recovered, _ = research.verify(None, candidate | {"published": "2026-11-21"},
                                       {url}, official, day, {})
        self.assertEqual(recovered.published, "2026-11-22")
        independent, _ = research.verify(
            None, candidate | {"excerpt": "CONSULT programming is restored in all regions."},
            {url}, official, day, {},
        )
        self.assertTrue(independent.active)
        self.assertEqual(independent.description, note)

    def test_candidates_accept_returned_search_sources_and_annotations(self):
        response = {"output": [
            {"type": "web_search_call", "action": {"sources": [{"url": "https://example.com/search"}]}},
            {"type": "message", "content": [{"type": "output_text", "text": '{"findings":[]}',
                                            "annotations": [{"type": "url_citation",
                                                             "url": "https://example.com/cite"}]}]},
        ]}
        self.assertEqual(research.cited_candidates(response),
                         ([], {"https://example.com/search": "", "https://example.com/cite": ""}))

    def test_tool_sources_are_retained_when_model_output_is_invalid(self):
        url = "https://example.com/fdrs-bulletin"
        response = {"output": [
            {"type": "web_search_call", "action": {"query": "FDRS releases",
                                                  "sources": [{"url": url}]}},
            {"type": "message", "content": [{"type": "output_text", "text": "not JSON"}]},
        ]}
        with self.assertLogs(research.LOG, "INFO") as logs:
            proposed, sources = research.cited_candidates(response)
            self.assertEqual(research.ranked_leads(proposed, sources)[0]["url"], url)
        self.assertIn("FDRS releases", "\n".join(logs.output))

    @patch("research.monitor.fetch")
    def test_short_original_nissan_notice_is_supported_without_inventing_a_date(self, fetch):
        url = "https://www.nissan-techinfo.com/"
        fetch.return_value = (url, b"<h1>Temporary suspension of CONSULT-III plus services</h1>"
                                   b"<p>Programming access is suspended until further notice.</p>")
        notice, reason = research.verify(None, {"url": url}, {url},
                                         {"www.nissan-techinfo.com": {"Nissan"}},
                                         date(2026, 11, 23), {})
        self.assertFalse(reason)
        self.assertTrue(notice.active)
        self.assertFalse(notice.published)

    @patch("research.monitor.fetch")
    def test_honda_in_original_source_lead_without_model_date_or_excerpt(self, fetch):
        url = "https://techinfo.honda.com/in-outage"
        fetch.return_value = (url, b"""
          <main><h1>Honda iN dealer network outage</h1><time datetime="2026-11-22">November 22</time>
          <p>The Honda iN dealer network is currently experiencing an outage that affects dealer sign-in.</p>
          <p>Honda will post updates on this page as service is restored.</p></main>""")
        notice, reason = research.verify(None, {"url": url}, {url},
                                         {"techinfo.honda.com": {"Honda"}}, date(2026, 11, 23), {})
        self.assertFalse(reason)
        self.assertEqual(notice.service, "iN")
        self.assertEqual(notice.published, "2026-11-22")

    @patch("research.monitor.fetch")
    def test_original_technician_post_does_not_require_oem_confirmation(self, fetch):
        url = "https://forums.ross-tech.com/index.php?threads/123/"
        fetch.return_value = (url, b"""
          <h1>ODIS authentication error during programming</h1>
          <article class="message--post"><time datetime="2026-11-22">November 22</time>
          <div class="message-body"><p>I get an authentication error every time I connect ODIS to start programming.</p>
          <p>These are observations from my own diagnostic session at the workshop.</p></div></article>""")
        rules = monitor.RobotFileParser()
        rules.parse(["User-agent: *", "Allow: /"])
        notice, reason = research.verify(None, {"url": url}, {url}, {}, date(2026, 11, 23),
                                         {"forums.ross-tech.com": rules})
        self.assertFalse(reason)
        self.assertEqual(notice.evidence, "Community Reported")
        self.assertIn("authentication problem", notice.description)

    def test_preflight_skips_non_article_urls_without_leaking_session_paths(self):
        url = "https://example.com/portal%3BTISESSIONID%3Dsecret"
        self.assertNotIn("secret", research.safe_url(url))
        self.assertIn("session identifier", research.skip_reason(url))
        self.assertIn("PDF", research.skip_reason("https://example.com/file.pdf"))
        self.assertFalse(research.skip_reason("https://www.nissan-techinfo.com/"))
        self.assertFalse(research.skip_reason("https://example.com/dated/notice"))

    def test_recent_actionable_source_outranks_preferred_oem_generic_pages(self):
        day = date(2026, 11, 23)
        recent = "https://example.com/2026/11/22/fdrs-programming-outage/"
        legacy = "https://usa.nissannews.com/2024/09/03/consult-quick-start/"
        generic = "https://techinfo.honda.com/contact-us"
        ranked = research.ranked_leads([{"url": generic, "kind": "alert"}], {
            generic: "Honda support", legacy: "CONSULT quick start", recent: "FDRS programming outage",
        }, day)
        self.assertEqual(ranked[0]["url"], recent)
        self.assertIn("recent date signal", research.relevance(ranked[0], day)[1])
        _, explanation = research.relevance({
            "url": "https://hondanews.com/en-US/releases/strong-growth-in-sales",
        }, day)
        self.assertNotIn("named tool", explanation)

    @patch("research.verify")
    def test_tls_failure_does_not_consume_other_host_slots(self, verify):
        day = date(2026, 11, 23)
        portal = next(url for url in research.PRIORITY_PORTALS if "honda" in url)
        second = "https://techinfo.honda.com/honda-in-outage-second"
        def inspect(session, lead, *args):
            if "honda" in lead["url"]:
                raise research.requests.exceptions.SSLError("unable to get local issuer certificate")
            return None, "public shell"

        verify.side_effect = inspect
        sources = [{"kind": "portal", "url": portal, "oem": "Honda"},
                   {"kind": "portal", "url": "https://www.nissan-techinfo.com/", "oem": "Nissan"}]
        _, rows = research.verify_leads([], {portal: "Honda iN outage", second: "Honda iN outage"},
                                        sources, day)
        by_url = {row["source_url"]: row for row in rows}
        self.assertEqual(by_url[portal]["outcome"], "Rejected")
        self.assertEqual(by_url[second]["outcome"], "Not checked")
        self.assertIn("TLS verification already failed", by_url[second]["reason"])

    def test_verified_alternative_feed_keeps_community_label(self):
        day = date(2026, 11, 23)
        url = "https://diag.net/msg/m123example"
        notice = monitor.Notice("Honda", "Community", "Honda iN outage",
                                "A technician reported an authentication problem in iN.", url,
                                "2026-11-22", evidence="Community Reported", service="iN")
        findings, rows = research.verify_leads([], {}, [], day, alternatives=[{
            "url": url, "title": notice.title, "published": notice.published,
            "service": notice.service, "origin": "Diagnostic Network public Atom", "verified": notice,
        }])
        self.assertEqual(findings, [notice])
        self.assertEqual(rows[0]["original_publication"], "2026-11-22")
        self.assertIn("Community Reported", rows[0]["reason"])

    @patch("research.monitor.fetch")
    def test_independent_nissan_report_can_pass_without_oem_portal(self, fetch):
        url = "https://www.repairerdrivennews.com/2026/11/22/nissan-consult-suspension/"
        fetch.return_value = (url, b"""
          <meta property="og:title" content="Shops report Nissan CONSULT-III Plus interruption">
          <meta property="article:published_time" content="2026-11-22T17:00:00Z">
          <article><p>Independent repairers told the publication that their Nissan CONSULT-III Plus
          programming access was suspended while they tried to complete customer repairs.</p>
          <p>The report accounts for the technicians' direct observations.</p></article>""")
        rules = monitor.RobotFileParser()
        rules.parse(["User-agent: *", "Allow: /"])
        notice, reason = research.verify(None, {"url": url}, {url}, {}, date(2026, 11, 23),
                                         {"www.repairerdrivennews.com": rules})
        self.assertFalse(reason)
        self.assertEqual(notice.oem, "Nissan")
        self.assertEqual(notice.evidence, "Industry Reported")
        self.assertFalse(notice.active)

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
