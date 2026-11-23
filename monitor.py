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
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import community
from evidence import statuspage, topix

EASTERN = ZoneInfo("America/New_York")
TIMEOUT = (5, 12)
MAX_BYTES = 2_000_000
LOG = logging.getLogger("oem-monitor")
USER_AGENT = "OEM-Service-Monitor/1.0 (public announcement reader)"
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
    r"(?:software|application|programming|authentication) compatibility (?:issues?|problems?)|"
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
    service: str = ""
    evidence: str = "Officially Confirmed"
    observed: str = ""
    critical: bool = False
    episode: int = 0

    @property
    def key(self):
        return digest([self.oem, self.identifier or self.url])

    @property
    def revision(self):
        revision = [self.title, self.description, self.published, self.status]
        if self.episode:
            revision.append(self.episode)
        return digest(revision)


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
    session.headers["User-Agent"] = USER_AGENT
    retry = Retry(
        total=1, backoff_factor=0.5, status_forcelist=(500, 502, 503, 504),
        allowed_methods={"GET"}, raise_on_status=False, respect_retry_after_header=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch(session, url, hosts, rules=None):
    for _ in range(6):
        if rules is not None and not rules.can_fetch(USER_AGENT, url):
            raise SourceError("public forum path excluded by robots.txt")
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
        service=affected_service(headline, details, source),
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
                              identifier=key, service=affected_service(text, "", source)))
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
        components = ", ".join(component["name"] for component in item.get("components", []))
        notices.append(Notice(
            source["oem"], "Scheduled maintenance" if maintenance else "Service disruption",
            item["name"], description[:900], url, find_date(item["created_at"]),
            status.replace("_", " "), identifier=f"witech:{item['id']}",
            active=status in {"scheduled", "in_progress", "verifying", "investigating", "identified", "monitoring"},
            updated=find_date(latest.get("created_at"), item.get("updated_at")),
            service=components or affected_service(item["name"], description, source, "wiTECH"),
            critical=not maintenance and item.get("impact") in {"major", "critical"},
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


def affected_service(title, details, source, default=""):
    # Prefer the named factory tool over the portal that published the item.
    match = community.platform(title) or community.platform(details)
    if match:
        return match[1]
    for pattern, label in (
        (r"\bMDI ?2\b", "MDI 2"), (r"\bIDS\b", "IDS"),
        (r"\bMDSFA\b", "MDSFA"), (r"\bSOTA\b", "SOTA"),
        (r"\b(?:login|sign.in|authentication)\b", "Authentication"),
    ):
        if re.search(pattern, f"{title} {details}", re.I):
            return label
    return default or source.get("service") or source.get("name", "Factory service information")


def forum_rules(session, source, hosts):
    url = source["url"]
    try:
        _, raw = fetch(session, urljoin(url, "/robots.txt"), hosts)
    except requests.HTTPError as exc:
        if exc.response.status_code != 404:
            raise
        raw = b""
    rules = RobotFileParser()
    rules.parse(raw.decode("utf-8", errors="replace").splitlines())
    if not rules.can_fetch(USER_AGENT, url):
        raise SourceError("community feed excluded by robots.txt")
    return rules


def community_notices(session, source, hosts, url, body, rules):
    notices, errors = [], []
    for title, target, text, timestamp in community.entries(body):
        link = official_url(url, target, hosts)
        if not community.platform(title) or not link or not community.thread_id(link):
            continue
        if not rules.can_fetch(USER_AGENT, link):
            continue
        if source["community"] == "ross-tech":
            try:
                link, thread = fetch(session, link, hosts, rules=rules)
                page = read_html(thread)
                post = page.select_one("article.message--post")
                content = post.select_one(".message-body") if post else None
                published = post.select_one("time[datetime]") if post else None
                if not content or not published:
                    raise SourceError("original public forum post not found")
                text = community.plain(str(content))
                timestamp = published["datetime"]
            except (requests.RequestException, SourceError) as exc:
                errors.append(f"forum post unavailable ({failure_reason(exc)})")
                continue
        discovery = community.report_details(title, text)
        date_label = community.published_date(timestamp, EASTERN)
        if discovery and date_label:
            oem, service, symptom = discovery
            notices.append(Notice(oem, "Community", title, symptom, link, date_label,
                                  identifier=community.thread_id(link), service=service,
                                  evidence="Community Reported"))
    return notices, sorted(set(errors))


def component_findings(signal):
    if signal.status not in {"Reported Issue", "Outage"} or not signal.affected:
        return []
    observed = datetime.fromisoformat(signal.verified).astimezone(EASTERN).date().isoformat()
    if signal.findings:
        return [
            Notice(signal.oem, "Known issue", title, details, signal.url,
                   identifier=f"topix:{title.casefold()}", active=True, observed=observed,
                   service=affected_service(title, "", {}, "TOPIx"), critical=critical)
            for title, details, critical in signal.findings
        ]
    reported = community.published_date(signal.reported, EASTERN) if signal.reported else ""
    return [Notice(
        signal.oem, "Service alert", f"{signal.scope} reports {signal.status.lower()}",
        "; ".join(signal.affected), signal.url, published=reported,
        identifier=f"component-status:{signal.scope}", active=True, observed=observed,
        service=signal.scope, critical=bool(signal.critical) or signal.status == "Outage",
    )]


def check_source(source):
    hosts = {urlsplit(source["url"]).hostname, *source.get("hosts", [])}
    evidence = None
    retrieved = ""
    try:
        with public_session() as session:
            kind = source["kind"]
            rules = forum_rules(session, source, hosts) if kind == "community" else None
            url, body = fetch(session, source["url"], hosts, rules=rules)
            retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if kind in {"incidents", "scheduled_maintenances"}:
                notices, errors = status_notices(source, hosts, body)
            elif kind == "statuspage":
                notices, errors = [], []
                payload = json.loads(body)
                evidence = statuspage(source, payload, retrieved)
                for feed in ("incidents", "scheduled_maintenances"):
                    found, _ = status_notices(dict(source, kind=feed), hosts, body)
                    notices.extend(found)
                if not notices:
                    notices.extend(component_findings(evidence))
            elif kind == "topix":
                notices, errors = [], []
                evidence = topix(source, read_html(body), retrieved)
                notices = component_findings(evidence)
                if evidence.status == "Unverified":
                    errors.append("public TOPIx panel has no explicit service-health signal")
            elif kind == "community":
                notices, errors = community_notices(session, source, hosts, url, body, rules)
            elif kind == "rss":
                notices, errors = rss_notices(session, source, hosts, url, body)
            else:
                notices, errors = html_notices(session, source, hosts, url, body)
    except (requests.RequestException, SourceError, ValueError, KeyError, TypeError) as exc:
        notices, errors = [], [failure_reason(exc)]
    LOG.info("%s / %s [%s] %s — %d candidate(s)%s", source["oem"], source["name"],
             source["kind"], source["url"], len(notices),
             f"; {'; '.join(errors)}" if errors else "")
    return notices, evidence, retrieved, errors


def collect(sources, state):
    notices, evidence = [], []
    health = state.setdefault("sources", {})
    with ThreadPoolExecutor(max_workers=8) as pool:
        for source, result in zip(sources, pool.map(check_source, sources), strict=True):
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
            LOG.info("Source health — %s: last retrieved=%s, last verified=%s, last status=%s",
                     source["name"], record["last_retrieved"], record["last_verified"],
                     record["last_status"])
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
            "status": notice.status, "service": notice.service, "evidence": notice.evidence, "first_seen": archive.get(key, {}).get("first_seen", today.isoformat()),
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


QUIET_MESSAGE = "No new OEM service alerts or technical updates identified today."
COLORS = {
    "OEM Service Alerts": 0xB45309,
    "Known Issues": 0x7C3AED,
    "Software & Technical Updates": 0x2563EB,
    "Community Reports": 0x475569,
    "Resolved Issues": 0x15803D,
}


def section_for(notice):
    if notice.evidence != "Officially Confirmed":
        return "Community Reports"
    if notice.status in {"resolved", "postmortem", "completed"} or notice.category == "Resolved outage":
        return "Resolved Issues"
    if notice.category in {"Scheduled maintenance", "Service alert"} or (notice.critical and notice.category != "Known issue") or re.search(
        r"\b(outage|scheduled maintenance|degraded performance)\b", notice.title, re.I
    ):
        return "OEM Service Alerts"
    if notice.category in {"Service disruption", "Known issue"} or re.search(
        r"\b(known (?:software )?(?:issues?|defects?)|compatibility (?:issue|problem)|"
        r"(?:SPS2?|FDRS|ODIS|MDARS|wiTECH|ISTA|Xentry|Techline Connect) (?:error|bug))\b",
        notice.title, re.I,
    ):
        return "Known Issues"
    return "Software & Technical Updates"


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
    date_label = f"Published {notice.published}" if notice.published else f"Observed {notice.observed or today}"
    metadata = f"**{notice.evidence}** · {date_label} · {change}"
    if change in {"Changed", "Resolved"} and notice.updated and notice.updated != notice.published:
        metadata += f" · Updated {notice.updated}"
    if notice.status:
        metadata += f" · {escape(notice.status)}"
    link = linked("Original source", notice.url, limit - 210)
    service = truncate(escape(notice.service or "Factory service information"), 125)
    context = f"**Service** {service}"
    if notice.evidence != "Officially Confirmed":
        context += " · Scope unconfirmed"
    details = truncate(escape(notice.description),
                       max(0, min(300, limit - units(metadata + context + link) - 3)))
    return {
        "name": truncate(escape(f"{notice.oem} — {notice.title}"), 256),
        "value": f"{metadata}\n{context}\n{details}\n{link}",
        "inline": False,
    }


def mark_live_episodes(notices, evidence, state):
    conditions = state.setdefault("live_conditions", {})
    for signal in evidence:
        previous = conditions.get(signal.oem, {})
        episode = previous.get("episode", 0)
        if previous.get("status") == "Operational" and signal.status in {"Outage", "Reported Issue"}:
            episode += 1
        conditions[signal.oem] = {"status": signal.status, "episode": episode}
    return [
        replace(notice, episode=conditions.get(notice.oem, {}).get("episode", 0))
        if notice.identifier.startswith(("topix:", "component-status:")) else notice
        for notice in notices
    ]


def recovery_notices(evidence, notices, state):
    tracked = state.setdefault("tracked_critical", {})
    recoveries = []
    for signal in evidence:
        previous = tracked.get(signal.oem)
        # Track significant live disruptions independently of delivery cadence.
        if signal.status == "Outage" or signal.critical:
            if not previous:
                tracked[signal.oem] = signal.verified
        elif previous and signal.status == "Operational":
            del tracked[signal.oem]
            observed = datetime.fromisoformat(signal.verified).astimezone(EASTERN).date().isoformat()
            # If the official incident feed has a recent resolution, prefer the incident link.
            explicit = any(
                n.oem == signal.oem and n.status in {"resolved", "postmortem"}
                and n.critical and n.updated and n.updated >= datetime.fromisoformat(previous).astimezone(EASTERN).date().isoformat()
                for n in notices
            )
            if explicit:
                continue
            recoveries.append(Notice(
                signal.oem, "Resolved outage", f"{signal.scope} reports restored operation",
                "The official status source now confirms normal operation after the monitored disruption.",
                signal.url, status="resolved", identifier=f"recovery:{previous}", active=True,
                service=signal.scope, observed=observed, critical=True,
            ))
    return recoveries


def report(today, pending, *, quiet=True):
    if not pending:
        return [QUIET_MESSAGE] if quiet else []
    embeds = []
    items = {name: [] for name in COLORS}
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
    for embed in embeds:
        embed["description"] = (
            f"The Diagnostic Hub · {today:%b %d, %Y} ET" +
            (f"\n\n{embed['description']}" if embed.get("description") else "")
        )
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
    payload = {"allowed_mentions": {"parse": []}}
    payload.update({"content": embeds} if isinstance(embeds, str) else {"embeds": embeds})
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
        sources = [source for source in sources if source["kind"] in {"statuspage", "topix", "incidents"}]
    notices, evidence = collect(sources, state)
    notices = mark_live_episodes(notices, evidence, state)
    notices.extend(recovery_notices(evidence, notices, state))
    if args.alerts_only:
        notices = [notice for notice in notices if notice.critical]
    pending = changes(notices, state, today)
    messages = report(today, pending, quiet=not args.alerts_only)
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
