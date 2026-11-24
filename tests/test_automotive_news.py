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
