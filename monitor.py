"""Collect public OEM announcements and deliver a daily Discord digest."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from urllib.parse import urldefrag, urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

EASTERN = ZoneInfo("America/New_York")
TIMEOUT = (5, 12)
MAX_BYTES = 2_000_000
LOG = logging.getLogger("oem-monitor")
DISRUPTION = re.compile(
    r"\b(?:scheduled maintenance|planned maintenance|(?:site|system|service|server|portal|"
    r"login|log[ -]?in|sign[ -]?in|programming|reprogramming|diagnostic\w*) "
    r"(?:is |are |will be )?(?:temporarily unavailable|unavailable|interrupted|outage|"
    r"disruption|degradation|interruption|issues?|problems?|failures?)|"
    r"(?:unable|cannot) to (?:log[ -]?in|sign[ -]?in|connect|program)|"
    r"maintenance window|(?:outage|service disruption) (?:notice|update))\b", re.I
)
UPDATE = re.compile(
    r"\b(?:release notes|service know-how emerging issues|TCSC top issues this week|software (?:updates?|releases?)|"
    r"(?:diagnostic|programming|reprogramming|calibration) (?:tool |application )?"
    r"(?:updates?|releases?)|"
    r"(?:technical service|service information) (?:announcements?|bulletins?)\b|"
    r"version [^,.]{0,30}\d[\w.]*,? released\b)", re.I
)
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?!\d)")
WRITTEN_DATE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.? "
    r"\d{1,2},? \d{4}\b", re.I
)
NUMERIC_DATE = re.compile(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b")
ACCESS_TITLE = re.compile(
    r"^(?:just a moment|attention required|access denied|403 forbidden|401 unauthorized|"
    r"verify you are human|sign in|log in|login|authentication required)(?:[ .!|:–-]|$)", re.I
)


class SourceError(Exception):
    """An incomplete or unreadable public source."""


@dataclass(frozen=True)
class Notice:
    oem: str
    category: str
    title: str
    description: str
    url: str
    published: str = ""
    status: str = ""
    identifier: str = ""
    active: bool = False

    @property
    def key(self):
        return digest([self.oem, self.identifier or self.url])

    @property
    def revision(self):
        return digest([self.title, self.description, self.published, self.status])


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def clean(text):
    return " ".join(text.split())


def find_date(*texts):
    for text in texts:
        if not text:
            continue
        for pattern, formats in (
            (ISO_DATE, ("%Y-%m-%d",)),
            (WRITTEN_DATE, ("%B %d %Y", "%b %d %Y")),
            (NUMERIC_DATE, ("%m-%d-%Y",)),
        ):
            match = pattern.search(text)
            if match:
                value = match[0].replace(",", "").replace(".", "").replace("/", "-")
                for fmt in formats:
                    try:
                        return datetime.strptime(value, fmt).date().isoformat()
                    except ValueError:
                        continue
    return ""


def official_url(base, target, hosts):
    url = urldefrag(urljoin(base, target))[0]
    parts = urlsplit(url)
    if (parts.scheme == "https" and parts.hostname in hosts and parts.port in {443, None}
            and parts.username is None and parts.password is None):
        return url
    return ""


def public_session():
    session = requests.Session()
    session.headers["User-Agent"] = "OEM-Service-Monitor/1.0 (public announcement reader)"
    retry = Retry(
        total=1, backoff_factor=0.5, status_forcelist=(500, 502, 503, 504),
        allowed_methods={"GET"}, raise_on_status=False, respect_retry_after_header=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch(session, url, hosts):
    for _ in range(6):
        session.cookies.clear()
        with session.get(url, timeout=TIMEOUT, allow_redirects=False, stream=True) as response:
            if response.is_redirect:
                url = official_url(url, response.headers["Location"], hosts)
                if not url:
                    raise SourceError("redirect outside configured public hosts")
                continue
            response.raise_for_status()
            body = bytearray()
            deadline = time.monotonic() + 18
            for chunk in response.iter_content(chunk_size=16384):
                body.extend(chunk)
                if len(body) > MAX_BYTES or time.monotonic() > deadline:
                    raise SourceError("response exceeded size or time limit")
            return url, bytes(body)
    raise SourceError("too many redirects")


def read_html(body):
    page = BeautifulSoup(body, "html.parser")
    title = page.title.get_text(" ", strip=True) if page.title else ""
    if ACCESS_TITLE.match(title) or page.select_one(
        "#challenge-form, iframe[src*='captcha'], input[name='cf-turnstile-response']"
    ):
        raise SourceError("access or challenge page")
    for tag in page.select('script, style, template, noscript, [hidden], [aria-hidden=true], [style*="display: none"]'):
        tag.decompose()
    visible = clean(page.get_text(" ", strip=True))
    if len(visible) < 140:
        raise SourceError("insufficient public HTML content")
    return page


def category(text):
    if DISRUPTION.search(text):
        return "Service disruption"
    if UPDATE.search(text):
        return "Software or technical update"
    return ""


def article_notice(session, source, hosts, url, headline, label):
    final, body = fetch(session, url, hosts)
    page = read_html(body)
    article = page.select_one(source.get("article", "article, main"))
    if article is None:
        raise SourceError("announcement article could not be located")
    title = article.select_one("h1")
    if title:
        headline = clean(title.get_text(" ", strip=True))
    published = page.select_one("meta[property='article:published_time'], time[datetime]")
    date_hint = published.get("content", published.get("datetime", "")) if published else ""
    if source.get("date_selector"):
        date_node = article.select_one(source["date_selector"])
        if date_node:
            date_hint = date_node.get_text(" ", strip=True)
    paragraphs = [
        clean(node.get_text(" ", strip=True)) for node in article.select("p")
        if len(clean(node.get_text(" ", strip=True))) >= 30
    ]
    details = " ".join(paragraphs[:2])
    if not details:
        details = clean(article.get_text(" ", strip=True))
    return Notice(
        source["oem"], category(headline) or label, headline, details[:600], final,
        find_date(headline, date_hint, details),
    )


def html_notices(session, source, hosts, url, body):
    page = read_html(body)
    if source.get("expect") and not page.select_one(source["expect"]):
        raise SourceError("configured public content could not be located")
    notices = []
    candidates = {}
    for anchor in page.select(source.get("links", "main a, article a, h1 a, h2 a, h3 a")):
        headline = clean(anchor.get_text(" ", strip=True))
        label = category(headline)
        link = official_url(url, anchor.get("href", ""), hosts)
        if label and link and link != url and 12 <= len(headline) <= 220:
            candidates.setdefault(link, (headline, label))
    errors = []
    for link, (headline, label) in list(candidates.items())[:12]:
        try:
            notices.append(article_notice(session, source, hosts, link, headline, label))
        except (requests.RequestException, SourceError) as exc:
            errors.append(f"announcement link unavailable ({failure_reason(exc)})")
            LOG.warning("%s %s — %s", source["name"], link, failure_reason(exc))
    if not notices and source.get("coverage"):
        errors.append(source["coverage"])
    if len(candidates) > 12:
        errors.append("announcement link limit reached")

    selector = source.get("inline", "[role=alert], main p, article p, .notice, .announcement")
    for node in page.select(selector):
        text = clean(node.get_text(" ", strip=True))
        label = category(text)
        dated = find_date(text)
        if not label or not 20 <= len(text) <= 850:
            continue
        # Generic undated software copy often describes subscriptions or instructions.
        if label != "Service disruption" and not dated and not source.get("inline"):
            continue
        key = url + "#" + digest(text.casefold())
        parent_with_id = node if node.get("id") else node.find_parent(id=True)
        link = url
        if parent_with_id and re.fullmatch(r"[\w-]+", parent_with_id["id"]):
            link += "#" + parent_with_id["id"]
        notices.append(Notice(source["oem"], label, text[:160], text, link, dated,
                              identifier=key))
    return notices, sorted(set(errors))


def status_notices(source, hosts, body):
    payload = json.loads(body)
    entries = payload[source["kind"]]
    if not isinstance(entries, list):
        raise SourceError("unexpected status feed format")
    notices = []
    for item in entries:
        incident_id = item["id"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", incident_id):
            raise SourceError("invalid status notice ID")
        url = official_url(source["url"], f"/incidents/{incident_id}", hosts)
        if not url:
            raise SourceError("unexpected status notice host")
        status = item["status"]
        updates = item.get("incident_updates", [])
        latest = max(updates, key=lambda update: update.get("created_at", ""), default={})
        maintenance = source["kind"] == "scheduled_maintenances"
        description = clean(latest.get("body") or item["name"])
        if maintenance:
            window = f"Scheduled window: {item['scheduled_for']} to {item['scheduled_until']}. "
            description = window + description
        notices.append(Notice(
            source["oem"], "Scheduled maintenance" if maintenance else "Service disruption",
            item["name"], description[:900], url, find_date(item["created_at"]),
            status.replace("_", " "), identifier=f"witech:{item['id']}",
            active=status not in {"resolved", "completed", "postmortem"},
        ))
    return notices, []


def failure_reason(exc):
    if isinstance(exc, requests.HTTPError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, SourceError):
        return str(exc)
    return type(exc).__name__


def check_source(source):
    hosts = {urlsplit(source["url"]).hostname, *source.get("hosts", [])}
    try:
        with public_session() as session:
            url, body = fetch(session, source["url"], hosts)
            if source.get("kind") in {"incidents", "scheduled_maintenances"}:
                notices, errors = status_notices(source, hosts, body)
            else:
                notices, errors = html_notices(session, source, hosts, url, body)
    except (requests.RequestException, SourceError, ValueError, KeyError, TypeError) as exc:
        notices, errors = [], [failure_reason(exc)]
    LOG.info("%s / %s — %d candidate(s)%s", source["oem"], source["name"], len(notices),
             f"; {'; '.join(errors)}" if errors else "")
    return notices, [
        (source["oem"], source["name"], error, source["url"]) for error in errors
    ]


def load_state(path):
    if path.exists():
        state = json.loads(path.read_text())
        if state["version"] != 1 or not isinstance(state["seen"], dict):
            raise ValueError("Invalid monitor state")
        return state
    return {"version": 1, "seen": {}, "last_report": ""}


def save_state(path, state):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n")
    temporary.replace(path)


def changes(notices, state, today):
    pending = []
    seen = state["seen"]
    cutoff = today - timedelta(days=30)
    unique = {notice.key: notice for notice in notices}
    for key, notice in unique.items():
        previous = seen.get(key)
        if previous == notice.revision:
            continue
        seen[key] = notice.revision
        if not previous and notice.published and not notice.active:
            if date.fromisoformat(notice.published) < cutoff:
                continue
        pending.append((notice, "Changed" if previous else "New"))
    return sorted(pending, key=lambda entry: (
        entry[0].category == "Software or technical update", entry[0].oem, entry[0].title
    ))


def escape(text):
    return re.sub(r"([\\*_~`|<>\[\]])", r"\\\1", clean(text))


def report(today, pending, failures):
    sections = [f"**OEM Service Monitor — {today:%B %d, %Y} (ET)**"]
    if not pending:
        sections.append("No new verified announcements were found in the public sources checked.")
    for notice, change in pending:
        published = notice.published or f"Date not published · observed {today}"
        status = f" · {escape(notice.status)}" if notice.status else ""
        sections.append(
            f"**{escape(notice.oem)} — {escape(notice.title)}**\n"
            f"{change} · {notice.category} · {published}{status}\n"
            f"{escape(notice.description)}\n<{notice.url}>"
        )
    if failures:
        lines = ["**Public source coverage gaps**",
                 "These checks could not be completed. OEM outage status is unverified."]
        lines.extend(f"• {escape(oem)} / {escape(name)} — {escape(reason)} <{url}>"
                     for oem, name, reason, url in failures)
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def deliver(webhook, message, today, count, failures):
    url = urlsplit(webhook)
    if url.scheme != "https" or url.netloc != "discord.com" or not re.fullmatch(
        r"/api(?:/v\d+)?/webhooks/\d+/[\w-]+/?", url.path
    ):
        raise ValueError("DISCORD_WEBHOOK_URL must be a Discord HTTPS webhook URL")
    webhook = f"https://discord.com{url.path}"
    payload = {"content": message, "allowed_mentions": {"parse": []}}
    files = None
    if len(message.encode("utf-16-le")) // 2 > 1900:
        headline = (f"{count} new or changed verified announcement(s)." if count else
                    "No new verified announcements were found in the public sources checked.")
        payload["content"] = (
            f"**OEM Service Monitor — {today:%B %d, %Y} (ET)**\n"
            f"{headline} {failures} coverage gap(s). See the attached report."
        )
        files = {"files[0]": ("oem-service-report.md", message.encode(), "text/markdown")}
    for attempt in range(3):
        try:
            options = {"files": files, "data": {"payload_json": json.dumps(payload)}} if files else {
                "json": payload
            }
            response = requests.post(
                webhook, params={"wait": "true"}, allow_redirects=False, timeout=TIMEOUT,
                **options,
            )
        except requests.RequestException:
            # An ambiguous network failure may already have delivered the message.
            raise RuntimeError("Discord delivery could not be confirmed") from None
        if response.status_code == 200:
            return
        if response.status_code != 429 or attempt == 2:
            raise RuntimeError(f"Discord delivery failed (HTTP {response.status_code})")
        retry = float(response.headers.get("Retry-After", "1"))
        time.sleep(min(max(retry, 1), 30))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("sources.json"))
    parser.add_argument("--state", type=Path, default=Path("monitor-state.json"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once-per-day", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    today = datetime.now(EASTERN).date()
    state = load_state(args.state)
    if args.once_per_day and state["last_report"] == today.isoformat():
        LOG.info("The Eastern daily report was already delivered")
        return
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook and not args.dry_run:
        raise SystemExit("Set the DISCORD_WEBHOOK_URL Actions secret before sending")
    sources = json.loads(args.config.read_text())
    if not sources:
        raise SystemExit("Configure at least one public source")
    notices = []
    failures = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for found, errors in pool.map(check_source, sources):
            notices.extend(found)
            failures.extend(errors)
    pending = changes(notices, state, today)
    message = report(today, pending, failures)
    if args.dry_run:
        print(message)
        return
    deliver(webhook, message, today, len(pending), len(failures))
    state["last_report"] = today.isoformat()
    save_state(args.state, state)
    LOG.info("Delivered report with %d new or changed announcements", len(pending))


if __name__ == "__main__":
    main()
