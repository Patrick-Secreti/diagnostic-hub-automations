import unittest
from datetime import date
import automotive_news as news

class NewsTests(unittest.TestCase):
    def item(self, **changes):
        return dict({"title":"NASTF changes key programming requirements",
                     "url":"https://nastf.org/news/requirements",
                     "published":"2026-11-24", "summary":"New security credential rules.",
                     "why":"Check credential requirements before programming.",
                     "source_type":"AI web search / cited"}, **changes)

    def test_filter_dates_and_domains(self):
        today=date(2026,11,24)
        self.assertTrue(news.eligible(self.item(), today))
        for changes in ({"published":"2026-11-01"}, {"published":"2026-11-25"},
                        {"url":"https://nastf.org.example.com/news"},
                        {"title":"New car sales figures", "summary":"Sales rose"},
                        {"url":"https://nastf.org/"}):
            self.assertFalse(news.eligible(self.item(**changes), today))

    def test_deduplication(self):
        today=date(2026,11,24)
        first=self.item()
        duplicate=self.item(url=first["url"]+"?utm_source=foo")
        self.assertEqual(len(news.select([first, duplicate], {}, today)), 1)
        self.assertEqual(news.select([first], {news.canonical(first["url"]):today.isoformat()}, today), [])

    def test_discord_mentions_disabled(self):
        self.assertEqual(news.message(self.item())["allowed_mentions"], {"parse":[]})

    def test_homepage_notice_and_link(self):
        today = date(2026,11,24)
        page = news.BeautifulSoup("""<div class="announcement-banner">
        <h2>New J2534 diagnostic software release</h2>
        <p>The updated programming software is now available for independent repairers.</p>
        <a href="/news/j2534-release">Read the diagnostic software release notes</a>
        </div>""", "html.parser")
        items, leads = news.homepage_findings(page, "https://nastf.org/", today)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["date_label"], "Observed (publication unknown)")
        self.assertTrue(any(x["url"] == "https://nastf.org/news/j2534-release" for x in leads))
        self.assertEqual(news.select(items, {news.item_key(items[0]):today.isoformat()}, today), [])

    def test_homepage_outage_stays_in_oem_channel(self):
        page = news.BeautifulSoup('<div class="alert">CONSULT diagnostic services have been restored after the outage.</div>', "html.parser")
        items, _ = news.homepage_findings(page, "https://www.nissan-techinfo.com/", date(2026,11,24))
        self.assertEqual(items, [])
