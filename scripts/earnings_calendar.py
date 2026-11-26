import os
import json
import urllib.request
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

now = datetime.now(ZoneInfo("America/New_York"))
day = now.date().isoformat()
key = os.environ["FINNHUB_API_KEY"]
webhook = os.environ["SLACK_EARNINGS_WEBHOOK"]
if not key or not webhook:
    raise SystemExit("Missing earnings API key or Slack webhook")
url = "https://finnhub.io/api/v1/calendar/earnings?" + urllib.parse.urlencode({"from": day, "to": day})
req = urllib.request.Request(url, headers={"X-Finnhub-Token": key})
try:
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.load(response)
except Exception:
    raise SystemExit("Earnings API request failed") from None
if not isinstance(data.get("earningsCalendar"), list):
    raise SystemExit("Invalid earnings API response")
rows = [r for r in data["earningsCalendar"] if r.get("date") == day and r.get("symbol")]
# Revenue estimates provide a rough size proxy, not market capitalization.
rows.sort(key=lambda r: r.get("revenueEstimate") or 0, reverse=True)
unique = {}
for row in rows:
    unique.setdefault(row["symbol"], row)
selected = list(unique.values())[:25]
blocks = [{"type": "header", "text": {"type": "plain_text", "text": "EARNINGS CALENDAR"}},
          {"type": "context", "elements": [{"type": "mrkdwn", "text": now.strftime("%A, %B %d, %Y") + " | US earnings | ET"}]},
          {"type": "divider"}]
for hours, label in [({"bmo"}, "BEFORE MARKET OPEN"), ({"amc"}, "AFTER MARKET CLOSE"), ({"dmh"}, "DURING MARKET HOURS"), (None, "TIME UNCONFIRMED")]:
    group = [r for r in selected if (r.get("hour") in hours if hours else r.get("hour") not in {"bmo", "amc", "dmh"})]
    if not group:
        continue
    lines = []
    for r in group:
        eps = r.get("epsEstimate")
        estimate = f"${eps:.2f}" if isinstance(eps, (int, float)) else "N/A"
        symbol = str(r["symbol"]).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"*{symbol}*  |  EPS est. {estimate}")
    blocks.append({"type":"section","text":{"type":"mrkdwn","text":"*" + label + "*\n" + "\n".join(lines)}})
if not selected:
    blocks.append({"type":"section","text":{"type":"mrkdwn","text":"No earnings listed by the data provider today."}})
blocks.append({"type":"context","elements":[{"type":"mrkdwn","text":f"Source: Finnhub | Showing {len(selected)} of {len(unique)} symbols, prioritized by estimated revenue. Dates and times may change."}]})
payload = {"text": "Earnings calendar — " + day, "blocks": blocks}
try:
    req = urllib.request.Request(webhook, data=json.dumps(payload).encode(), headers={"Content-Type":"application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as response:
        if response.read().strip() != b"ok":
            raise ValueError("Unexpected Slack response")
except Exception:
    raise SystemExit("Slack delivery failed") from None
print("Earnings calendar delivered")
