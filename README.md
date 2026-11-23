# The Diagnostic Hub · OEM Service Monitor

A Discord digest of official public service status and technical news for independent diagnostic and programming technicians. The report groups 17 manufacturer entries by current status, highlights significant disruptions, and summarizes dated technical announcements. Retrieval errors go to GitHub Actions logs and delivery state.

## Setup and schedule

1. Copy the destination Discord channel webhook from **Edit Channel → Integrations → Webhooks**.
2. In repository **Settings → Secrets and variables → Actions**, verify the existing `DISCORD_WEBHOOK_URL` secret.
3. Open **Actions → OEM Service Monitor → Run workflow**. Leave **dry_run** checked to preview the embed payload in the run log. Clear it on the default branch to send a live daily report.

The daily schedule is **7:00 AM Eastern**, with daylight saving handled by `America/New_York`. A separate schedule checks official live sources every three hours at minute 17 UTC. It sends an alert for a newly confirmed outage, a significant programming disruption, or a material change to a tracked critical issue. On its first successful check it baselines existing incidents, except when an official report timestamp places the incident within the last three hours. It sends a recovery only when the official source explicitly confirms normal operation. GitHub can delay or disable scheduled runs in inactive public repositories.

## Verified coverage

| Manufacturer | Public source and validation |
|---|---|
| Stellantis | **Live status** from the [wiTECH status page](https://fcawitech.statuspage.io/) and its [component summary API](https://fcawitech.statuspage.io/api/v2/summary.json). The public [incident history](https://fcawitech.statuspage.io/history) and [knowledge base](https://kb.fcawitech.com/) supply dated notices and release notes. |
| Jaguar / Land Rover | **Live status** from the [TOPIx System Status page](https://topix.landrover.jlrext.com/topix/systemstatus/index). Its public HTML exposed current TOPIx notices and a scoped SOTA programming disruption. The report uses the check time when a notice has no publication time. |
| GM | **Technical news** from [GM TechLink](https://gm-techlink.com/) and its [RSS feed](https://gm-techlink.com/?feed=rss2). [ACDelco TDS](https://www.acdelcotds.com/) had an application shell with insufficient public HTML to assess health. |
| Mazda | **Release monitoring** on [IDS](https://www.mazdaserviceinfo.com/ids-software) and [PCM reflashing](https://www.mazdaserviceinfo.com/pcm-reflashing), which publish dated installer versions. [MDARS](https://www.mazdaserviceinfo.com/mdars-software) exposed tool and subscription information without a dedicated dated release list in validation. |
| Ford / Lincoln | [Motorcraft](https://www.motorcraftservice.com/) and the [FDRS support candidate](https://www.motorcraftservice.com/Diagnostic/HelmSupport?categoryId=244&channelId=31&country=USA&language=EN-US) returned HTTP 503 during validation. FDRS remains configured for future public retrieval. |
| Honda / Acura, Subaru | [Honda TechInfo](https://techinfo.honda.com/) returned HTTP 503. [Subaru TechInfo](https://techinfo.subaru.com/) redirects to STIS, which responded inconsistently in validation. |
| Nissan / Infiniti, BMW / MINI, Mercedes-Benz | [Nissan TechInfo](https://www.nissan-techinfo.com/), [BMW Technical Information](https://bmwtechinfo.bmwgroup.com/), and [StarTekInfo](https://www.startekinfo.com/) returned access errors or insufficient public HTML during validation. |
| Toyota / Lexus, Hyundai, Kia, Volvo | Public landing or product information was readable at [Toyota TIS](https://techinfo.toyota.com/), [Hyundai TechInfo](https://www.hyundaitechinfo.com/), [Kia TechInfo](https://kiatechinfo.snapon.com/), and [Volvo Tech Info](https://www.volvotechinfo.com/). These pages provide no verified live programming status. |
| Volkswagen, Audi | The [Volkswagen](https://vw-us.erwin-store.com/) and [Audi](https://audi-us.erwin-store.com/) erWin roots redirected to readable public home pages. Newsletter and diagnostics pages are tracked for public announcements. Live health remains unverified. |
| Mitsubishi | The [public home screen](https://mitsubishitechinfo.com/epacarb/home.jsp) and [programming tools page](https://mitsubishitechinfo.com/epacarb/specialtools.jsp) were reachable from the framed [official portal](https://www.mitsubishitechinfo.com/). Member service data require authentication, and live health remains unverified. |

Status applies to the services named by the linked evidence. A component failure or a scoped programming interruption is a **Reported Issue**, unless the source confirms a complete outage within the monitored service scope. A page response or retrieval error leaves health **Unverified** unless separate, current official status evidence exists. Historical evidence is retained for troubleshooting.

## Sources and delivery state

[`sources.json`](sources.json) identifies each manufacturer, URL, and parser. Source types include public HTML, RSS, TOPIx status HTML, and Statuspage JSON. HTML overrides include `links`, `article`, `title_selector`, `date_selector`, `body_selector`, `inline`, and `expect`. Add alternate official redirect or article hosts with `hosts`. Requests use HTTPS, bounded timeouts and response sizes, and per-host pacing. The monitor stops at access restrictions and keeps checking other OEMs.

The `monitor-state` branch stores per-source retrieval and verification times, the last verified status, errors, observed announcement metadata, revision fingerprints, delivery progress, and critical-alert history. Daily news uses a seven-day publication or explicit revision window, with older and undated discoveries archived. Current official maintenance and disruptions can still appear when the original announcement is older. The digest reports **No new verified OEM announcements** on quiet days.

Keep `monitor-state` writable by GitHub Actions. Partial delivery resumes from the last recorded batch. If Discord accepted a batch before a timeout or a state push failed, check the channel before retrying.

## Local checks

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
python monitor.py --dry-run
python monitor.py --dry-run --alerts-only
```

Previews leave Discord and saved delivery state untouched. Pass `--state` to use existing delivery history.
