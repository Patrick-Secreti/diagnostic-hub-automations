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
from urllib.parse import quote, urldefrag, urljoin, urlsplit
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
    r"(?:technical service|service information) (?:announcements?|bulletins?|updates?)\b|"
    r"(?:service portal|aftermarket|service information) (?:announcement|launch)|"
    r"new (?:mopar )?technical service portal|"
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
    title = article.select_one(source.get("title_selector", "h1"))
    if title:
        headline = clean(title.get_text(" ", strip=True))
    published = page.select_one("meta[property='article:published_time'], time[datetime]")
    date_hint = published.get("content", published.get("datetime", "")) if published else ""
    if source.get("date_selector"):
        date_node = article.select_one(source["date_selector"])
        if date_node:
            date_hint = date_node.get_text(" ", strip=True)
    content = article.select_one(source["body_selector"]) if source.get("body_selector") else article
    content = content or article
    paragraphs = [
        clean(node.get_text(" ", strip=True)) for node in content.select("p")
        if len(clean(node.get_text(" ", strip=True))) >= 30
    ]
    details = " ".join(paragraphs[:2])
    if not details:
        details = clean(content.get_text(" ", strip=True))
    if not details:
        details = f"Official release notes for {headline} are available at the source."
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


def status_evidence(body):
    status = json.loads(body)["status"]["indicator"]
    if status == "none":
        return "Operational"
    if status in {"minor", "major", "critical", "maintenance"}:
        return "Reported Issue"
    raise SourceError("unknown status indicator")


def check_source(source):
    hosts = {urlsplit(source["url"]).hostname, *source.get("hosts", [])}
    evidence = None
    try:
        with public_session() as session:
            url, body = fetch(session, source["url"], hosts)
            if source.get("kind") in {"incidents", "scheduled_maintenances"}:
                notices, errors = status_notices(source, hosts, body)
            elif source.get("kind") == "status":
                notices, errors = [], []
                evidence = (source["oem"], status_evidence(body), source["public_url"])
            else:
                notices, errors = html_notices(session, source, hosts, url, body)
    except (requests.RequestException, SourceError, ValueError, KeyError, TypeError) as exc:
        notices, errors = [], [failure_reason(exc)]
    LOG.info("%s / %s — %d candidate(s)%s", source["oem"], source["name"], len(notices),
             f"; {'; '.join(errors)}" if errors else "")
    return notices, [
        (source["oem"], source["name"], error, source["url"]) for error in errors
    ], evidence


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


DASHBOARD = (
    ("Ford", ("Ford",)), ("GM", ("GM",)), ("Honda", ("Honda",)),
    ("Toyota", ("Toyota",)), ("Nissan", ("Nissan",)), ("BMW", ("BMW",)),
    ("Mercedes-Benz", ("Mercedes-Benz",)),
    ("Stellantis", ("Chrysler / Stellantis",)),
    ("Volkswagen/Audi", ("Volkswagen", "Audi")), ("JLR", ("JLR",)),
)
STATUS_MARKS = {"Operational": "🟢", "Reported Issue": "🟠", "Unverified": "⚪"}
COLORS = {
    "Service Status": 0xD97706, "OEM News": 0x2563EB, "Software Releases": 0x7C3AED,
    "Technical Service Updates": 0x0F766E, "Announcements": 0x475569,
    "Public Source Coverage": 0x64748B,
}


def current_issue(notice):
    if notice.category == "Scheduled maintenance":
        return notice.status in {"in progress", "verifying"}
    return notice.category == "Service disruption" and notice.active and notice.status in {
        "investigating", "identified", "monitoring",
    }


def dashboard(evidence, notices):
    states = {}
    for oem, status, url in evidence:
        previous = states.get(oem)
        if not previous or status == "Reported Issue":
            states[oem] = (status, url)
    for notice in notices:
        if current_issue(notice):
            states[notice.oem] = ("Reported Issue", notice.url)
    rows = []
    for name, brands in DASHBOARD:
        readings = [states.get(brand, ("Unverified", "")) for brand in brands]
        statuses = [status for status, _ in readings]
        if "Reported Issue" in statuses:
            status = "Reported Issue"
        elif all(reading == "Operational" for reading in statuses):
            status = "Operational"
        else:
            status = "Unverified"
        links = [(brand, url) for brand, (reading, url) in zip(brands, readings)
                 if url and reading == status]
        rows.append((name, status, links))
    return rows


def section_for(notice):
    title = notice.title
    if notice.category in {"Service disruption", "Scheduled maintenance"}:
        return "Service Status"
    if re.search(r"release notes|software (?:update|release)|version .{0,35}released|"
                 r"(?:diagnostic|(?:re)?programming) (?:tool |application )?(?:update|release)",
                 title, re.I):
        return "Software Releases"
    if re.search(r"bulletin|technical service update|TCSC top issues|calibration update", title, re.I):
        return "Technical Service Updates"
    if re.search(r"announcement|notice|launch|new .{0,25}portal", title, re.I):
        return "Announcements"
    return "OEM News"


def units(text):
    return len(text.encode("utf-16-le")) // 2


def truncate(text, limit):
    if limit <= 0:
        return ""
    if units(text) <= limit:
        return text
    return text.encode("utf-16-le")[:2 * (limit - 1)].decode("utf-16-le", "ignore").rstrip("\\ ") + "…"


def linked(label, url, limit=350):
    url = quote(url, safe=":/?#[]@!$&'*+,;=%-._~")
    if units(url) > limit:
        parts = urlsplit(url)
        url = f"{parts.scheme}://{parts.netloc}/"
        label = "Official site"
    return f"[{label}]({url})"


def fields_for(title, fields):
    for offset in range(0, len(fields), 4):
        heading = title if offset == 0 else f"{title} · continued"
        yield {"title": heading, "color": COLORS[title], "fields": fields[offset:offset + 4]}


def notice_field(notice, change, today, limit=1024):
    date_label = notice.published or f"Date unpublished · observed {today}"
    metadata = f"{date_label} · {change}"
    if notice.status:
        metadata += f" · {escape(notice.status)}"
    link = linked("Official source", notice.url, limit - 170)
    details = truncate(escape(notice.description), max(0, min(330, limit - units(metadata + link) - 2)))
    return {
        "name": truncate(escape(f"{notice.oem} — {notice.title}"), 256),
        "value": f"{metadata}\n{details}\n{link}",
        "inline": False,
    }


def report(today, pending, failures, evidence=(), notices=()):
    rows = dashboard(evidence, notices)
    counts = {status: sum(row[1] == status for row in rows) for status in STATUS_MARKS}
    dashboard_lines = []
    for name, status, links in rows:
        sources = " · ".join(linked(brand if len(links) > 1 else "Status source", url, limit=260)
                             for brand, url in links)
        dashboard_lines.append(f"{STATUS_MARKS[status]} **{name}** · {status}" +
                               (f" · {sources}" if sources else ""))
    embeds = [{
        "title": "OEM Service Monitor",
        "description": f"**{today:%B %d, %Y} (ET)**\n\n" + "\n".join(dashboard_lines),
        "color": 0x172554,
    }, {
        "title": "Status Summary",
        "description": "\n".join(
            f"{mark} **{counts[status]}** {status}" for status, mark in STATUS_MARKS.items()
        ) + "\n\nOperational requires a current official status signal. Unverified means "
        "available sources could not establish current service health. "
        "Status applies to the public services shown in the linked evidence.\n\n" +
        (f"**{len(pending)}** new or changed verified items · **{len(failures)}** coverage gaps"
         if pending else "No new verified announcements were found in the public sources checked."
         + (f" **{len(failures)}** coverage gaps." if failures else "")),
        "color": 0x2563EB,
    }]
    sections = {name: [] for name in COLORS}
    pending_keys = {notice.key for notice, _ in pending}
    ongoing = {notice.key: notice for notice in notices if current_issue(notice)}
    items = {name: [] for name in COLORS}
    for key, notice in ongoing.items():
        if key not in pending_keys:
            items["Service Status"].append((notice, "Ongoing"))
    for notice, change in pending:
        items[section_for(notice)].append((notice, change))
    for oem, name, reason, url in failures:
        sections["Public Source Coverage"].append({
            "name": truncate(escape(f"{oem} — {name}"), 256),
            "value": f"{escape(reason)} · {linked('Public page', url)}",
            "inline": False,
        })
    for name, entries in items.items():
        fields = sections[name]
        for notice, change in entries:
            field = notice_field(notice, change, today)
            if "Official site" in field["value"].splitlines()[-1]:
                embeds.extend(fields_for(name, fields))
                fields = []
                field = notice_field(notice, change, today, limit=3800)
                embeds.append({
                    "title": truncate(f"{name} · {field['name']}", 256),
                    "description": field["value"],
                    "color": COLORS[name],
                })
            else:
                fields.append(field)
        embeds.extend(fields_for(name, fields))
    return pack_embeds(embeds)


def pack_embeds(embeds):
    messages = []
    batch = []
    size = 0
    for embed in embeds:
        length = units(embed["title"]) + units(embed.get("description", ""))
        length += sum(units(field["name"]) + units(field["value"])
                      for field in embed.get("fields", []))
        if batch and (len(batch) == 10 or size + length > 6000):
            messages.append(batch)
            batch = []
            size = 0
        batch.append(embed)
        size += length
    if batch:
        messages.append(batch)
    return messages


def deliver(webhook, embeds):
    url = urlsplit(webhook)
    if url.scheme != "https" or url.netloc != "discord.com" or not re.fullmatch(
        r"/api(?:/v\d+)?/webhooks/\d+/[\w-]+/?", url.path
    ):
        raise ValueError("DISCORD_WEBHOOK_URL must be a Discord HTTPS webhook URL")
    webhook = f"https://discord.com{url.path}"
    payload = {"embeds": embeds, "allowed_mentions": {"parse": []}}
    for attempt in range(3):
        try:
            response = requests.post(
                webhook, params={"wait": "true"}, allow_redirects=False, timeout=TIMEOUT,
                json=payload,
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
    if state.get("delivery") and not args.dry_run:
        previous = state["delivery"]["date"]
        resume_delivery(webhook, args.state, state)
        if previous == today.isoformat():
            return
    notices = []
    failures = []
    evidence = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for found, errors, signal in pool.map(check_source, sources):
            notices.extend(found)
            failures.extend(errors)
            if signal:
                evidence.append(signal)
    pending = changes(notices, state, today)
    messages = report(today, pending, failures, evidence, notices)
    if args.dry_run:
        print(json.dumps(messages, ensure_ascii=False, indent=2))
        return
    state["delivery"] = {
        "date": today.isoformat(), "messages": messages, "next": 0, "items": len(pending),
    }
    save_state(args.state, state)
    resume_delivery(webhook, args.state, state)


def resume_delivery(webhook, path, state):
    delivery = state["delivery"]
    for batch in delivery["messages"][delivery["next"]:]:
        deliver(webhook, batch)
        delivery["next"] += 1
        save_state(path, state)
    state["last_report"] = delivery["date"]
    del state["delivery"]
    save_state(path, state)
    LOG.info("Delivered report with %d new or changed announcements", delivery["items"])


if __name__ == "__main__":
    main()
