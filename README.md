# The Diagnostic Hub · OEM Service Monitor

A Discord digest of public discoveries for independent diagnostic and programming technicians. It only displays sections with qualifying items: **OEM Service Alerts**, **Known Issues**, **Software & Technical Updates**, **Community Reports**, and **Resolved Issues**. Items include the manufacturer, affected service, evidence label, publication or observation date, a short explanation, and the original source. Retrieval errors and source health stay in GitHub Actions logs and delivery state.

## Setup and schedule

1. Copy the destination Discord channel webhook from **Edit Channel → Integrations → Webhooks**.
2. In repository **Settings → Secrets and variables → Actions**, verify the existing `DISCORD_WEBHOOK_URL` secret.
3. Open **Actions → OEM Service Monitor → Run workflow**. Leave **dry_run** checked to preview the embed payload in the run log. Clear it on the default branch to send a live daily report.

The daily schedule is **7:00 AM Eastern**, with daylight saving handled by `America/New_York`. A separate schedule checks official live sources every three hours at minute 17 UTC. It sends an alert for a previously unreported official outage or significant programming disruption, a material update, or a confirmed recovery. The scheduled checks and daily digest share announcement history, so delivery in either mode suppresses repeats in the other. An off-hours check with no qualifying change is silent. GitHub can delay or disable scheduled runs in inactive public repositories.

## Verified coverage

| Manufacturer | Public source and validation |
|---|---|
| Stellantis | **Live status** from the [wiTECH status page](https://fcawitech.statuspage.io/) and its [component summary API](https://fcawitech.statuspage.io/api/v2/summary.json). The public [incident history](https://fcawitech.statuspage.io/history), [unresolved feed](https://fcawitech.statuspage.io/api/v2/incidents/unresolved.json), and [release-note category](https://kb.fcawitech.com/category/witech-2-0-public-articles-aftermarket/release-notes/345/) returned usable responses. The component summary includes the aggregate signal from the standalone status endpoint and avoids a duplicate poll. |
| Jaguar / Land Rover | **Live status** from the [TOPIx System Status page](https://topix.landrover.jlrext.com/topix/systemstatus/index). Its public HTML exposed current TOPIx notices and a scoped SOTA programming disruption. The report uses the check time when a notice has no publication time. |
| GM | **Technical news** from [GM TechLink](https://gm-techlink.com/) and its [RSS feed](https://gm-techlink.com/?feed=rss2). [ACDelco TDS](https://www.acdelcotds.com/) had an application shell with insufficient public HTML to assess health. |
| Mazda | **Release monitoring** on [IDS](https://www.mazdaserviceinfo.com/ids-software) and [PCM reflashing](https://www.mazdaserviceinfo.com/pcm-reflashing), which publish dated installer versions. [MDARS](https://www.mazdaserviceinfo.com/mdars-software) exposed tool and subscription information without a dedicated dated release list in validation. |
| Ford / Lincoln | [Motorcraft](https://www.motorcraftservice.com/) and the [FDRS support candidate](https://www.motorcraftservice.com/Diagnostic/HelmSupport?categoryId=244&channelId=31&country=USA&language=EN-US) returned HTTP 503 during validation. FDRS remains configured for future public retrieval. |
| Honda / Acura, Subaru | [Honda TechInfo](https://techinfo.honda.com/), its [known-issues page](https://techinfo.honda.com/rjanisis/pubs/web/resource_Tools_KnownIssues.htm), and its [public notices page](https://techinfo.honda.com/rjanisis/RJAAI001_logon_content.asp) returned HTTP 503 in local validation. The dedicated public notice parser is covered by fixtures. [Subaru TechInfo](https://techinfo.subaru.com/) redirects to STIS, which responded inconsistently in validation. |
| Nissan / Infiniti, BMW / MINI, Mercedes-Benz | [Nissan TechInfo](https://www.nissan-techinfo.com/), [BMW Technical Information](https://bmwtechinfo.bmwgroup.com/), and [StarTekInfo](https://www.startekinfo.com/) returned access errors or insufficient public HTML during validation. Nissan’s homepage parser targets explicit CONSULT suspension notices. The live homepage returned HTTP 403 locally, so the suspension was not verified from that response. |
| Toyota / Lexus, Hyundai, Kia, Volvo | Public landing or product information was readable at [Toyota TIS](https://techinfo.toyota.com/), [Hyundai TechInfo](https://www.hyundaitechinfo.com/), [Kia TechInfo](https://kiatechinfo.snapon.com/), and [Volvo Tech Info](https://www.volvotechinfo.com/). These pages provide no verified live programming status. |
| Volkswagen, Audi | The [Volkswagen](https://vw-us.erwin-store.com/) and [Audi](https://audi-us.erwin-store.com/) erWin roots redirected to readable public home pages. Newsletter and diagnostics pages are tracked for public announcements. Live health remains unverified. |
| Mitsubishi | The [public home screen](https://mitsubishitechinfo.com/epacarb/home.jsp) and [programming tools page](https://mitsubishitechinfo.com/epacarb/specialtools.jsp) were reachable from the framed [official portal](https://www.mitsubishitechinfo.com/). Member service data require authentication, and live health remains unverified. |

Report scope follows the original notice. A reachability failure does not establish service health or generate an alert. When no new information qualifies, the daily digest sends one message: “No new OEM service alerts or technical updates identified today.”

## Public community reports

The [Ross-Tech Forum RSS feed](https://forums.ross-tech.com/index.php?forums/-/index.rss) and [FORScan Forum Atom feed](https://forum.forscan.org/app.php/feed) are public. The monitor checks robots rules, requires a named factory tool and a dated firsthand symptom, excludes quoted reports, and labels the resulting items **Community Reported** with scope unconfirmed. For Ross-Tech, the date comes from the original public thread post, since feed order can reflect later activity. The current adapters do not infer independent corroboration or widen a report’s scope from reposts. Reddit has no enabled source without a permitted access path.

[Diagnostic Network’s Programming Atom API](https://diag.net/api/v1/messages?o=atom&to=23) returned dated anonymous public entries. Its [Get Messages documentation](https://docs.diag.net/#get-messages) expressly supports RSS/Atom subscribers and directs public integrations to anonymous results. Only that documented topic feed is enabled. The website discussions collector remains disabled under the [Terms of Use](https://diag.net/terms). The monitor uses no credentials and makes no requests to individual Diagnostic Network discussions. Qualifying feed entries are labeled **Community Reported**.

[Repairer Driven News RSS](https://www.repairerdrivennews.com/feed/) returned dated article descriptions. The monitor uses that public feed for short, strictly targeted references labeled **Industry Reported**, linking directly to the publisher’s article. Automotive industry coverage does not establish an OEM outage. Vehicle Service Pros’ feed returned RSS but its [publisher terms](https://www.endeavorbusinessmedia.com/endeavor-terms) restrict automated collection. AftermarketNews’ category loaded, while its feed and terms returned access errors. iATN’s public landing pages loaded, but its [terms](https://www.iatn.net/terms-of-use) restrict automated retrieval for storage or offsite reading. These three candidates remain disabled. Third-party status pages do not establish official status and are not enabled.

## Sources and delivery state

[`sources.json`](sources.json) identifies each manufacturer, URL, and parser. Source types include public HTML, official RSS, TOPIx status HTML, Statuspage JSON, and public forum RSS or Atom. HTML overrides include `links`, `article`, `title_selector`, `date_selector`, `body_selector`, `inline`, and `expect`. Add alternate official redirect or article hosts with `hosts`. Requests use HTTPS, bounded timeouts and response sizes, and per-host pacing. The monitor stops at access restrictions and keeps checking other OEMs.

The `monitor-state` branch stores per-source retrieval and verification times, the last verified status, errors, observed announcement metadata, revision fingerprints, delivery progress, and critical-alert history. The first daily digest backfills relevant items from the prior seven days. Subsequent digests use the rolling seven-day publication window, with explicitly dated material updates eligible separately. Original publication and first discovery are shown separately. Unchanged official disruptions appear as ongoing context regardless of age. If a source no longer supplies a current check, the digest shows the last confirmation date and does not infer a recovery. Confirmed restorations remain as context for seven days after the source’s resolution date, or after the first observed confirmation if no timestamp is provided. Ongoing and recently resolved context never triggers an off-hours notification by itself.

Keep `monitor-state` writable by GitHub Actions. Partial delivery resumes from the last recorded batch. If Discord accepted a batch before a timeout or a state push failed, check the channel before retrying.

## OpenAI supplemental research

The 7 AM Eastern run uses one Responses API request with `gpt-5.4-mini` and built-in web search. It targets ten searches and caps built-in tool calls at 12 and output at 4,500 tokens. Searches cover factory programming, known issues, releases, technical bulletins, outages and restorations, with dedicated Nissan CONSULT and Honda iN queries. Public source domains are drawn from the existing OEM collectors, the Honda and Nissan newsrooms, Ross-Tech, FORScan, and Repairer Driven News. Diagnostic Network stays on its documented anonymous Atom collector. Restricted sites and Reddit without authorized access are excluded.

A supplemental finding must be cited by the search tool and independently retrievable from its original public source. The verifier checks the source’s publication date and exact supporting excerpt. It attributes official sources to their manufacturer and labels verified trade coverage and firsthand forum reports separately. Direct collectors take precedence when they have the same notice. The existing history handles deduplication, backfill, and delivery. API errors, budget exhaustion, or empty research results leave the existing collectors available, and never turn a source access failure into an outage report.

Store the key as the `OPENAI_API_KEY` Actions secret. To test without delivery, dispatch **OEM Service Monitor** with both **dry_run** and **ai_research** enabled. Ordinary dry runs and off-hours checks use no API credits. Logs show token usage, search and tool counts, estimate components, and verification results. The workflow never logs the key or the raw API response.

The `monitor-research-budget` branch records a **$0.50 reservation per API attempt** before making the call. At most 40 reservations per UTC month are allowed, including up to eight manual previews, and only one daily attempt may reserve per Eastern date. A failed or ambiguous attempt keeps its reservation and is never retried automatically. A rejected ledger push prevents the API call. The estimate uses standard rates of $0.75 per million input tokens, $0.075 per million cached input tokens, $4.50 per million output tokens and $0.01 per web tool call. Verify actual charges in the OpenAI dashboard. The $20 reservation ceiling applies to this workflow and is separate from account billing or other uses of the key. [OpenAI pricing](https://developers.openai.com/api/docs/pricing)

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
