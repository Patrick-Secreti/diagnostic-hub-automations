"""Collect public OEM announcements and deliver a daily Discord digest."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from threading import Lock
from urllib.parse import quote, urldefrag, urljoin, urlsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from evidence import SEVERITY, eastern_timestamp, statuspage, topix

EASTERN = ZoneInfo("America/New_York")
TIMEOUT = (5, 12)
MAX_BYTES = 2_000_000
LOG = logging.getLogger("oem-monitor")
REQUEST_LOCK = Lock()
NEXT_REQUEST = {}
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
    r"new (?:mopar )?technical service portal|known (?:software )?(?:issues?|defects?)|"
    r"(?:current .{1,20} release|version) [^,.]{0,35}, released|"
    r"(?:FDRS|SPS2?|GDS2|Techline Connect|wiTECH|ISTA|Xentry|ODIS|MDARS|HDS|VIDA) "
    r"(?:error|bug|fix|known issue)|"
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
    updated: str = ""

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
        host = urlsplit(url).hostname
        with REQUEST_LOCK:
            now = time.monotonic()
            start = max(now, NEXT_REQUEST.get(host, now))
            NEXT_REQUEST[host] = start + 0.5
        if start > now:
            time.sleep(start - now)
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
    canonical = page.select_one('link[rel=canonical][href]')
    if canonical:
        final = official_url(final, canonical["href"], hosts) or final
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
    modified = page.select_one('meta[property="article:modified_time"][content]')
    return Notice(
        source["oem"], category(headline) or label, headline, details[:600], final,
        find_date(date_hint, headline),
        updated=find_date(modified["content"]) if modified else "",
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
            active=status in {"scheduled", "in_progress", "verifying", "investigating", "identified", "monitoring"},
            updated=find_date(latest.get("created_at"), item.get("updated_at")),
        ))
    return notices, []


def failure_reason(exc):
    if isinstance(exc, requests.HTTPError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, SourceError):
        return str(exc)
    return type(exc).__name__


def rss_notices(session, source, hosts, url, body):
    try:
        channel = ElementTree.fromstring(body).find("channel")
    except ElementTree.ParseError as exc:
        raise SourceError("invalid public RSS feed") from exc
    if channel is None:
        raise SourceError("public RSS channel missing")
    notices, errors = [], []
    for entry in channel.findall("item")[:20]:
        title = clean(entry.findtext("title", ""))
        label = category(title)
        link = official_url(url, entry.findtext("link", ""), hosts)
        if not label or not link or link == url:
            continue
        try:
            notice = article_notice(session, source, hosts, link, title, label)
            if not notice.published and entry.findtext("pubDate"):
                published = parsedate_to_datetime(entry.findtext("pubDate")).date().isoformat()
                notice = replace(notice, published=published)
            notices.append(notice)
        except (requests.RequestException, SourceError, ValueError) as exc:
            errors.append(f"RSS article unavailable ({failure_reason(exc)})")
    return notices, sorted(set(errors))


def check_source(source):
    hosts = {urlsplit(source["url"]).hostname, *source.get("hosts", [])}
    evidence = None
    retrieved = ""
    try:
        with public_session() as session:
            url, body = fetch(session, source["url"], hosts)
            retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
            kind = source["kind"]
            if kind in {"incidents", "scheduled_maintenances"}:
                notices, errors = status_notices(source, hosts, body)
            elif kind == "statuspage":
                notices, errors = [], []
                evidence = statuspage(source, json.loads(body), retrieved)
            elif kind == "topix":
                notices, errors = [], []
                evidence = topix(source, read_html(body), retrieved)
                if evidence.status == "Unverified":
                    errors.append("public TOPIx panel has no explicit service-health signal")
            elif kind == "rss":
                notices, errors = rss_notices(session, source, hosts, url, body)
            else:
                notices, errors = html_notices(session, source, hosts, url, body)
    except (requests.RequestException, SourceError, ValueError, KeyError, TypeError) as exc:
        notices, errors = [], [failure_reason(exc)]
    LOG.info("%s / %s — %d candidate(s)%s", source["oem"], source["name"], len(notices),
             f"; {'; '.join(errors)}" if errors else "")
    return notices, evidence, retrieved, errors


def collect(sources, state):
    notices, evidence = [], []
    health = state.setdefault("sources", {})
    with ThreadPoolExecutor(max_workers=8) as pool:
        for source, result in zip(sources, pool.map(check_source, sources)):
            found, signal, retrieved, errors = result
            notices.extend(found)
            record = health.setdefault(source["url"], {
                "last_retrieved": None, "last_status": None, "last_verified": None,
            })
            record.update(oem=source["oem"], type=source["kind"], errors=errors)
            if retrieved:
                record["last_retrieved"] = retrieved
            if signal and signal.status != "Unverified":
                evidence.append(signal)
                record.update(last_status=signal.status, last_verified=signal.verified)
    return notices, evidence


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
    archive = state.setdefault("archive", {})
    cutoff = today - timedelta(days=7)
    unique = {}
    for notice in notices:
        previous = unique.get(notice.key)
        if not previous or (notice.updated, notice.published) >= (previous.updated, previous.published):
            unique[notice.key] = notice
    for key, notice in unique.items():
        previous = seen.get(key)
        archive[key] = {
            "oem": notice.oem, "url": notice.url, "title": notice.title,
            "published": notice.published, "updated": notice.updated,
            "status": notice.status, "first_seen": archive.get(key, {}).get("first_seen", today.isoformat()),
        }
        if previous == notice.revision:
            continue
        seen[key] = notice.revision
        revised = previous or notice.status in {"resolved", "postmortem"}
        effective = notice.updated if revised and notice.updated else notice.published
        recent = bool(effective and cutoff <= date.fromisoformat(effective) <= today)
        if notice.active or recent:
            label = "Changed" if previous else "New"
            if notice.status in {"resolved", "postmortem"}:
                label = "Resolved"
            pending.append((notice, label))
    return sorted(pending, key=lambda entry: (entry[0].oem, entry[0].title))


def escape(text):
    return re.sub(r"([\\*_~`|<>\[\]])", r"\\\1", clean(text))


DASHBOARD = {
    "Ford": "Ford / Lincoln", "GM": "GM", "Honda": "Honda / Acura",
    "Toyota": "Toyota / Lexus", "Nissan": "Nissan / Infiniti", "BMW": "BMW / MINI",
    "Mercedes-Benz": "Mercedes-Benz", "Chrysler / Stellantis": "Stellantis",
    "Volkswagen": "Volkswagen", "Audi": "Audi", "JLR": "Jaguar / Land Rover",
    "Subaru": "Subaru", "Mazda": "Mazda", "Hyundai": "Hyundai", "Kia": "Kia",
    "Volvo": "Volvo", "Mitsubishi": "Mitsubishi",
}
STATUS_MARKS = {"Outage": "🔴", "Reported Issue": "🟠", "Operational": "🟢", "Unverified": "⚪"}
COLORS = {
    "Critical Alerts": 0xB91C1C, "Diagnostic Software Releases": 0x7C3AED,
    "ECU Programming Updates": 0x2563EB, "Known Software Defects": 0xB45309,
    "Technical Service Bulletins": 0x0F766E, "OEM Service Portal Announcements": 0x475569,
    "Scheduled Maintenance": 0xD97706, "Resolved Outages": 0x15803D,
}


def dashboard(evidence):
    states = {}
    for signal in evidence:
        previous = states.get(signal.oem)
        if not previous or SEVERITY[signal.status] > SEVERITY[previous.status]:
            states[signal.oem] = signal
    return sorted(
        ((label, states.get(oem)) for oem, label in DASHBOARD.items()),
        key=lambda row: -SEVERITY[row[1].status] if row[1] else 0,
    )


def section_for(notice):
    title = notice.title
    if notice.status in {"resolved", "postmortem"} or notice.category == "Resolved outage":
        return "Resolved Outages"
    if notice.category == "Scheduled maintenance" or re.search(r"maintenance", title, re.I):
        return "Scheduled Maintenance"
    if re.search(r"bulletin|technical service update|TCSC top issues|service know-how", title, re.I):
        return "Technical Service Bulletins"
    if re.search(r"known (?:software )?(?:issues?|defects?)|software (?:bug|defect)|"
                 r"(?:FDRS|SPS2?|GDS2|Techline Connect|wiTECH|ISTA|Xentry|ODIS|MDARS|HDS|VIDA) "
                 r"(?:error|bug|fix)", title, re.I):
        return "Known Software Defects"
    if notice.category == "Service disruption":
        return "OEM Service Portal Announcements"
    if re.search(r"programming|reprogramming|calibration|reflash|MDSFA|\bMMP\b|SPS2?", title, re.I):
        return "ECU Programming Updates"
    if re.search(r"release notes|software (?:update|release)|released|diagnostic", title, re.I):
        return "Diagnostic Software Releases"
    return "OEM Service Portal Announcements"


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
    if change in {"Changed", "Resolved"} and notice.updated and notice.updated != notice.published:
        metadata += f" · Updated {notice.updated}"
    if notice.status:
        metadata += f" · {escape(notice.status)}"
    link = linked("Official source", notice.url, limit - 170)
    details = truncate(escape(notice.description), max(0, min(330, limit - units(metadata + link) - 2)))
    return {
        "name": truncate(escape(f"{notice.oem} — {notice.title}"), 256),
        "value": f"{metadata}\n{details}\n{link}",
        "inline": False,
    }


def alert_field(signal):
    timing = eastern_timestamp(signal.reported, EASTERN)
    when = f"Reported {timing}" if timing else (
        f"Verified {eastern_timestamp(signal.verified, EASTERN)} · report time unavailable"
    )
    details = truncate(escape(" · ".join(signal.critical or (signal.scope,))), 475)
    return {"name": truncate(escape(f"{DASHBOARD.get(signal.oem, signal.oem)} — {signal.status}"), 256),
            "value": f"{details}\n{when}\n{linked('Official status', signal.url)}", "inline": False}


def critical_signals(evidence):
    return [signal for signal in evidence if signal.status == "Outage" or signal.critical]


def update_alerts(evidence, state):
    active = state.setdefault("critical", {})
    observed = state.setdefault("alert_observed", {})
    changed = []
    for signal in evidence:
        if signal.status == "Unverified":
            continue
        previously_checked = signal.oem in observed
        observed[signal.oem] = signal.status
        previous = active.get(signal.oem)
        if signal.status == "Outage" or signal.critical:
            signature = digest([signal.status, sorted(signal.critical)])
            active[signal.oem] = signature
            # A pre-existing incident on the first poll belongs in the daily digest.
            verified = datetime.fromisoformat(signal.verified)
            recent_report = signal.reported and (
                timedelta() <= verified - datetime.fromisoformat(signal.reported) <= timedelta(hours=3)
            )
            if previous != signature and (previously_checked or recent_report):
                changed.append(signal)
        elif previous and signal.status == "Operational":
            del active[signal.oem]
            resolution = replace(signal, critical=("Official status now reports normal operation.",))
            changed.append(resolution)
            state.setdefault("resolutions", {})[signal.oem] = {
                "url": signal.url, "verified": signal.verified, "scope": signal.scope,
            }
    return changed


def report(today, pending, evidence=()):
    rows = dashboard(evidence)
    counts = {status: 0 for status in STATUS_MARKS}
    lines, unknown = [], []
    for name, signal in rows:
        status = signal.status if signal else "Unverified"
        counts[status] += 1
        if status == "Unverified":
            unknown.append(name)
            continue
        link = linked(signal.scope, signal.url)
        when = eastern_timestamp(signal.verified, EASTERN)
        lines.append(f"{STATUS_MARKS[status]} **{name}** · {status} · {link}\n"
                     f"Verified {when}")
        if signal.affected:
            lines.append(truncate(escape(" · ".join(signal.affected)), 240))
    if unknown:
        lines.append(f"⚪ **Unverified** · {', '.join(unknown)}\n"
                     "Current official service health could not be established.")
    counts_line = "  ·  ".join(
        f"{mark} **{counts[status]}** {status}" for status, mark in STATUS_MARKS.items()
    )
    heading = f"**The Diagnostic Hub · {today:%B %d, %Y} (ET)**"
    embeds = []
    chunk = heading
    for line in lines:
        if units(chunk + line) + 2 > 4096:
            embeds.append({"title": "OEM Service Status" if not embeds else "OEM Service Status · continued",
                           "description": chunk, "color": 0x172554})
            chunk = line
        else:
            chunk += f"\n\n{line}"
    embeds.append({"title": "OEM Service Status" if not embeds else "OEM Service Status · continued",
                   "description": chunk, "color": 0x172554})
    embeds.append({
        "title": "Status Summary",
        "description": counts_line + "\n\n" +
        (f"**{len(pending)}** new or materially updated technical announcements" if pending
         else "**0** new technical announcements · No new verified OEM announcements"),
        "color": 0x2563EB,
    })
    embeds.extend(fields_for("Critical Alerts", [alert_field(signal) for signal in critical_signals(evidence)]))
    if pending:
        embeds.append({"title": "OEM Technical News", "color": 0x2563EB})
    items = {name: [] for name in COLORS if name != "Critical Alerts"}
    for notice, change in pending:
        items[section_for(notice)].append((notice, change))
    for name, entries in items.items():
        fields = []
        for notice, change in entries:
            field = notice_field(notice, change, today)
            if field["value"].splitlines()[-1].startswith("[Official site]"):
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
    parser.add_argument("--alerts-only", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    today = datetime.now(EASTERN).date()
    state = load_state(args.state)
    if args.once_per_day and not args.alerts_only and state["last_report"] == today.isoformat():
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
        previous_alerts = state["delivery"].get("alerts_only", False)
        resume_delivery(webhook, args.state, state)
        if previous == today.isoformat() and not args.alerts_only and not previous_alerts:
            return
    if args.alerts_only:
        sources = [source for source in sources if source["kind"] in {"statuspage", "topix"}]
    notices, evidence = collect(sources, state)
    if args.alerts_only:
        alerts = update_alerts(evidence, state)
        messages = pack_embeds(list(fields_for("Critical Alerts", [alert_field(signal) for signal in alerts])))
        pending = alerts
    else:
        for oem, resolved in state.get("resolutions", {}).items():
            verified = resolved["verified"]
            observed = datetime.fromisoformat(verified).astimezone(EASTERN).date().isoformat()
            notices.append(Notice(
                oem, "Resolved outage", f"{resolved['scope']} reports normal operation",
                "The official status source now confirms normal operation after a monitored critical disruption.",
                resolved["url"], identifier=f"recovery:{verified}", updated=observed,
                published=observed,
            ))
        pending = changes(notices, state, today)
        messages = report(today, pending, evidence)
    if args.dry_run:
        print(json.dumps(messages, ensure_ascii=False, indent=2))
        return
    if not messages:
        save_state(args.state, state)
        LOG.info("No new or changed critical alerts")
        return
    state["delivery"] = {
        "date": today.isoformat(), "messages": messages, "next": 0, "items": len(pending),
        "alerts_only": args.alerts_only,
    }
    save_state(args.state, state)
    resume_delivery(webhook, args.state, state)


def resume_delivery(webhook, path, state):
    delivery = state["delivery"]
    for batch in delivery["messages"][delivery["next"]:]:
        deliver(webhook, batch)
        delivery["next"] += 1
        save_state(path, state)
    if not delivery.get("alerts_only"):
        state["last_report"] = delivery["date"]
    del state["delivery"]
    save_state(path, state)
    LOG.info("Delivered report with %d new or changed announcements", delivery["items"])


if __name__ == "__main__":
    main()
