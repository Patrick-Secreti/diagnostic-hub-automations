"""Interpret explicit status signals on public OEM status pages."""

from dataclasses import dataclass
from datetime import datetime
import re

PROGRAMMING = re.compile(
    r"\b(?:programming|reprogramming|(?:ECU )?flash(?:ing)?|SOTA|TOPIx Cloud|"
    r"SPS2?|FDRS|MDARS|ISTA|Xentry|ODIS|VCI OS|VIDA|HDS)\b", re.I
)
COMPLETE_OUTAGE = re.compile(r"\b(?:complete|full|total) (?:service )?outage\b", re.I)
CURRENT_ISSUE = re.compile(
    r"currently (?:experiencing|investigating|unavailable|inoperative)|"
    r"scheduled maintenance|planned maintenance|complete outage", re.I
)
SEVERITY = {"Unverified": 0, "Operational": 1, "Reported Issue": 2, "Outage": 3}


@dataclass(frozen=True)
class Status:
    oem: str
    status: str
    url: str
    verified: str
    scope: str
    critical: tuple = ()
    # The scope of a component outage may be narrower than the overall OEM status.
    affected: tuple = ()
    reported: str = ""


def source_timestamp(value):
    if not isinstance(value, str):
        return ""
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return ""
    return timestamp.isoformat() if timestamp.tzinfo is not None else ""


def newest_open_incident(payload, services, component_ids):
    matches = []
    for incident in payload.get("incidents", []):
        if incident.get("status") not in {"investigating", "identified", "monitoring"}:
            continue
        affected = {component["id"] for component in incident.get("components", [])}
        title_matches = PROGRAMMING.search(incident.get("name", ""))
        if affected.intersection(component_ids) or (services and title_matches):
            reported = source_timestamp(incident.get("started_at")) or source_timestamp(incident.get("created_at"))
            if reported:
                matches.append(reported)
    return max(matches, key=datetime.fromisoformat, default="")


def statuspage(source, payload, verified):
    components = [component for component in payload["components"] if not component.get("group")]
    indicator = payload["status"]["indicator"]
    states = {component["status"] for component in components}
    known = {"operational", "degraded_performance", "partial_outage", "major_outage", "under_maintenance"}
    if not components or not states <= known or indicator not in {
        "none", "minor", "major", "critical", "maintenance"
    }:
        raise ValueError("unknown or incomplete Statuspage component data")
    maintenance = [
        item["name"] for item in payload.get("scheduled_maintenances", [])
        if item["status"] in {"scheduled", "in_progress", "verifying"}
    ]
    incidents = [
        item for item in payload.get("incidents", [])
        if item["status"] in {"investigating", "identified", "monitoring"}
    ]
    if states == {"major_outage"}:
        status = "Outage"
    elif states != {"operational"} or indicator != "none" or maintenance or incidents:
        status = "Reported Issue"
    else:
        status = "Operational"
    affected = tuple(
        f"{component['name']} ({component['status'].replace('_', ' ')})"
        for component in components if component["status"] != "operational"
    ) + tuple(item["name"] for item in incidents) + tuple(maintenance)
    critical_components = [
        component for component in components
        if component["status"] in {"major_outage", "partial_outage"}
        and (status == "Outage" or PROGRAMMING.search(component["name"]))
    ]
    programming_incidents = [
        item for item in incidents
        if item.get("impact") in {"major", "critical"} and PROGRAMMING.search(item["name"])
    ]
    critical = tuple(f"{component['name']} · {component['status'].replace('_', ' ')}"
                     for component in critical_components)
    critical += tuple(f"{item['name']} · {item['status']}" for item in programming_incidents)
    reported = newest_open_incident(payload, critical, {item["id"] for item in critical_components})
    return Status(source["oem"], status, source["public_url"], verified,
                  "wiTECH service components", critical, affected, reported)


def topix(source, page, verified):
    panel = page.select_one("section.headless:has(.system-status)")
    if panel is None:
        raise ValueError("TOPIx status panel missing")
    region = panel.parent
    marker = panel.select_one(".system-status")
    text = panel.get_text(" ", strip=True)
    status = "Unverified"
    if re.search(r"all (?:systems|services) (?:are )?operational|no known issues", text, re.I):
        status = "Operational"
    if "status-warning" in marker.get("class", []) and marker.get_text(strip=True).isdigit():
        if int(marker.get_text(strip=True)) > 0:
            status = "Reported Issue"
    if COMPLETE_OUTAGE.search(text):
        status = "Outage"
    affected = []
    critical = []
    for section in region.select("section:has(.notice-title):has(.notice-details)"):
        title = section.select_one(".notice-title").get_text(" ", strip=True)
        details = section.select_one(".notice-details").get_text(" ", strip=True)
        header = section.select_one("header")
        is_issue = header and header.get_text(strip=True).lower() == "issue"
        if re.search(r"\b(?:issue (?:is|was|has been) resolved|normal operation (?:has )?resumed)\b",
                     details, re.I):
            continue
        if not is_issue and not CURRENT_ISSUE.search(details):
            continue
        if SEVERITY[status] < SEVERITY["Reported Issue"]:
            status = "Reported Issue"
        affected.append(title)
        if PROGRAMMING.search(f"{title} {details}") and re.search(
            r"inoperative|unavailable|outage|unable|cannot|not working", details, re.I
        ):
            critical.append(f"{title} — {details}")
    if status == "Outage" and not critical:
        critical.append("TOPIx — complete outage confirmed by the OEM status page")
    return Status(source["oem"], status, source["url"], verified, "TOPIx service status",
                  tuple(critical), tuple(affected))


def eastern_timestamp(value, tz):
    if not value:
        return ""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(tz).strftime(
        "%b %d, %Y %I:%M %p %Z"
    )
