"""Identify narrowly scoped official notices on OEM portal pages."""

import re

CONSULT = re.compile(r"\bCONSULT(?:[- ]?(?:III|3)(?:\s*plus)?)?\b", re.I)
HONDA_TOOLS = re.compile(r"\b(i[- ]?HDS|HDS|J2534|Honda ECU Reprogramming|Acura ECU Reprogramming)\b", re.I)
VOLVO_TOOLS = re.compile(r"\bVIDA\b", re.I)
SUSPENDED = re.compile(r"\b(suspend(?:ed|ing|s|sion)?|suspension|outage|unavailable|service interruption)\b", re.I)
MAINTENANCE = re.compile(r"\b(scheduled|planned|emergency) (?:system |service )?(?:downtime|maintenance)\b", re.I)
FAILURE = re.compile(r"\b(error|fails?|failed|failure|incompatible|not (?:working|supported|connecting)|"
                     r"cannot|unable to|does not (?:start|recognize|connect|work)|known issues?)\b", re.I)
CURRENT = re.compile(r"\b(currently|ongoing|until further notice|remains unavailable)\b", re.I)
RESTORED = re.compile(r"\b(?:service (?:has been |is )?restored|issue (?:has been |is |was )?resolved|"
                      r"normal operation (?:has )?resumed)\b", re.I)
RELEASE = re.compile(r"\b(?:software update|release notes|new version|version .{0,30}released)\b", re.I)
BLOCKS = "[role=alert], .notice, .alert, article, tr, p, li, div:not(:has(*)), h1, h2, h3, h4"


def texts(page):
    gathered = set()
    for block in page.select(BLOCKS):
        headline = " ".join(block.stripped_strings)
        if not headline:
            continue
        next_block = block.find_next_sibling() if block.name in {"h1", "h2", "h3", "h4"} else None
        detail = " ".join(next_block.stripped_strings) if next_block else ""
        text = " ".join(filter(None, (headline, detail)))
        if 20 <= len(text) <= 850 and text not in gathered:
            gathered.add(text)
            yield block, headline, text


def classify(kind, headline, text):
    if kind == "nissan":
        if (not CONSULT.search(text) or not (SUSPENDED.search(text) or RESTORED.search(text))
                or not re.search(r"\b(?:service|programming|diagnostic|software|subscription|access)\b", text, re.I)):
            return None
        if not (re.search(r"\b(?:suspension|suspen[ds]\w*|outage|unavailable)\b", headline, re.I)
                or RESTORED.search(text)):
            return None
        return ("CONSULT", "Service alert", "nissan:consult-suspension",
                bool(CURRENT.search(text) or re.search(r"\b(?:suspended|temporary suspension)\b", text, re.I)))
    if kind == "honda":
        match = HONDA_TOOLS.search(text)
        if not match or not (FAILURE.search(text) or MAINTENANCE.search(text) or RELEASE.search(text)):
            return None
        category = "Scheduled maintenance" if MAINTENANCE.search(text) else (
            "Known issue" if FAILURE.search(text) else "Software or technical update"
        )
        return match[0], category, "", bool(CURRENT.search(text) and category != "Software or technical update")
    if not VOLVO_TOOLS.search(text) or not (MAINTENANCE.search(text) or RELEASE.search(text)):
        return None
    category = "Scheduled maintenance" if MAINTENANCE.search(text) else "Software or technical update"
    return "VIDA", category, "", bool(CURRENT.search(text) and category == "Scheduled maintenance")
