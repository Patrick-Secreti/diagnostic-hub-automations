"""Select firsthand reports about factory tools from public forum feeds."""

from datetime import datetime
from email.utils import parsedate_to_datetime
import re
from urllib.parse import parse_qs, unquote, urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

PLATFORMS = (
    (r"\bFDRS\b", "Ford", "FDRS"),
    (r"\bFord IDS\b", "Ford", "IDS"),
    (r"\b(?:SPS2|SPS 2|Techline Connect)\b", "GM", "Techline Connect / SPS2"),
    (r"\bGDS2\b", "GM", "GDS2"),
    (r"\b(?:wiTECH|Mopar programming)\b", "Chrysler / Stellantis", "wiTECH"),
    (r"\bODIS\b", "Volkswagen / Audi", "ODIS"),
    (r"\bISTA\b", "BMW", "ISTA"),
    (r"\bXentry\b", "Mercedes-Benz", "Xentry"),
    (r"\bMDARS\b", "Mazda", "MDARS"),
    (r"\bHonda (?:i-?HDS|HDS)\b", "Honda", "HDS"),
    (r"\bVIDA\b", "Volvo", "VIDA"),
    (r"\bTOPIx\b", "JLR", "TOPIx"),
    (r"\bTechstream\b", "Toyota", "Techstream"),
    (r"\bCONSULT[- ]?(?:III|3)\b", "Nissan", "CONSULT"),
    (r"\bMUT[- ]?(?:III|3)\b", "Mitsubishi", "MUT-III"),
)
PROBLEM = re.compile(
    r"\b(?:fails?|failed|failing|failure|error(?:s| codes?)?|stuck|crash(?:es|ed|ing)?|"
    r"can't|cannot|couldn't|could not|unable to|incompatible|won't|will not|unavailable|"
    r"timed? ?out|not (?:working|connecting|loading|compatible))\b", re.I
)
FIRSTHAND = re.compile(r"\b(?:I|we)\s+(?:\w+[’']\w+\s+)?(?:\w+\s+){0,3}"
                       r"(?:get|got|see|saw|receive|received|have|had|tried|try|trying|"
                       r"can(?:not|'t)|could(?: not|n't)|am|are|keep|ran)\b", re.I)
OFFER = re.compile(r"\b(?:crack(?:ed)?|keygen|activate for free|for sale|download link|torrent)\b", re.I)


def platform(text):
    for pattern, oem, service in PLATFORMS:
        if re.search(pattern, text, re.I):
            return oem, service
    return None


def plain(html):
    page = BeautifulSoup(html, "html.parser")
    for tag in page.select("script, style, blockquote, .bbCodeBlock--quote"):
        tag.decompose()
    return " ".join(page.get_text(" ", strip=True).split())


def report_details(title, body):
    match = platform(title)
    if not match or OFFER.search(title):
        return None
    for sentence in re.split(r"(?<=[.!?])\s+", body):
        if not FIRSTHAND.search(sentence) or not PROBLEM.search(sentence):
            continue
        if OFFER.search(sentence):
            continue
        detail = sentence.lower()
        if re.search(r"\b(?:login|sign[ -]?in|authenticat\w*|credentials)\b", detail):
            symptom = "a sign-in or authentication problem"
        elif re.search(r"\b(?:stuck|failed|unable)\b.{0,50}\b(?:devices? page|page labeled devices|device setup)\b", detail):
            symptom = "getting stuck at the device setup screen"
        elif re.search(r"\b(?:crash\w*|incompatib\w*)\b", detail):
            symptom = "an application crash or compatibility problem"
        elif re.search(r"\b(?:network|connect\w*|timed? ?out)\b", detail):
            symptom = "a connection or timeout problem"
        else:
            symptom = "a failure in the diagnostic or programming workflow"
        return (*match, f"A technician reported {symptom} while using {match[1]}.")
    return None


def entries(raw):
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError("invalid community feed") from exc
    if root.tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise ValueError("missing RSS channel")
        for item in channel.findall("item")[:20]:
            yield plain(item.findtext("title", "")), item.findtext("link", ""), "", ""
    elif root.tag == "{http://www.w3.org/2005/Atom}feed":
        for item in root.findall("{*}entry")[:20]:
            link = item.find("{*}link")
            yield (plain(item.findtext("{*}title", "")).split(" • ", 1)[-1],
                   link.get("href", "") if link is not None else "",
                   plain(item.findtext("{*}content", "")), item.findtext("{*}published", ""))
    else:
        raise ValueError("unsupported community feed format")


def published_date(value, tz):
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        try:
            timestamp = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return ""
    return timestamp.astimezone(tz).date().isoformat() if timestamp.tzinfo else ""


def thread_id(url):
    parts = urlsplit(url)
    topic = re.search(r"threads/(?:[^/?#]+\.)?(\d+)(?:/|$)", unquote(url))
    if topic:
        return f"{parts.hostname}:thread:{topic[1]}"
    if parts.hostname == "diag.net" and re.fullmatch(r"/msg/m[a-z0-9]+", parts.path):
        return f"diag.net:{parts.path}"
    if parts.path == "/viewtopic.php":
        post = parse_qs(parts.query).get("p", [""])[0]
        if post.isdigit():
            return f"{parts.hostname}:post:{post}"
    return ""
