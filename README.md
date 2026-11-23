# OEM Service Monitor

A scheduled GitHub Actions job posts a daily digest of public factory service announcements to Discord. It looks for outages, maintenance, login and programming disruptions, diagnostic and programming software releases, and technical service announcements for U.S. or North American repair portals where available.

## Setup

1. In Discord, open the destination channel's **Edit Channel → Integrations → Webhooks** and copy the webhook URL.
2. In GitHub, open **Settings → Secrets and variables → Actions** and create or verify the repository secret `DISCORD_WEBHOOK_URL`.
3. Open **Actions → OEM Service Monitor → Run workflow** on the default branch. Leave **dry_run** enabled to preview the digest in the run log. Clear **dry_run** to deliver to Discord and persist delivery history.

The default-branch workflow is scheduled at 7:00 AM in `America/New_York`, including daylight saving changes. GitHub can queue scheduled jobs, so posts may arrive after 7:00. GitHub can disable scheduled workflows in inactive public repositories. The Actions tab shows the workflow status and allows an authorized user to re-enable it.

## Sources and coverage

[`sources.json`](sources.json) lists Ford, GM, Chrysler / Stellantis, Honda, Toyota, Nissan, Hyundai, Kia, BMW, Mercedes-Benz, Volkswagen, Audi, Subaru, Mazda, Volvo, and JLR. Chrysler / Stellantis has structured public wiTECH incident and maintenance feeds. GM TechLink has public articles, and Mazda IDS has a public release list. Other entries check official public landing or information pages for published notices.

The monitor requests public HTML and JSON over HTTPS. A successful portal response only establishes what the scanner could read on that page. Some OEMs publish notices inside authenticated portals or interactive panels, and public coverage varies by OEM. The report separates source access failures and incomplete checks from OEM-confirmed notices. To add or tune a source, edit its URL and optional selectors in `sources.json` and run a preview. `links` selects announcement links, `article` selects article content, `date_selector` selects its publication date within the article, `inline` selects notices published on the page itself, and `expect` identifies markup required for a successful check. Use `hosts` only for additional official destinations needed by an intentional redirect or article link. Use `coverage` to explain a known limitation of the public HTML scan.

## Delivery and troubleshooting

- Reports include the OEM, category, publication or observation date, description, and direct source link. Larger digests arrive as a single Discord post with an attached report. Discord mentions are disabled.
- The workflow records announcement revisions and the latest successful report date on the `monitor-state` branch. Open incidents are included even when older than 30 days. Unseen dated entries older than 30 days go into the history without posting, while material edits to known entries are posted. Undated notices show the observation date. Keep the state branch writable by the workflow.
- A no-change digest states that no new verified announcements were found. Coverage gaps indicate HTTP errors, challenges, unexpected redirects or unreadable markup. Check the source manually or adjust the public URL or selectors before interpreting these as service information.
- If the delivery step reports a missing secret or Discord rejects the webhook, verify the channel integration and update the GitHub secret. If the job posted to Discord and the state push failed, check the channel before rerunning. A post accepted by Discord followed by a state-write failure can be repeated because the two services commit independently.
- If no scheduled run appears, verify Actions are enabled and this workflow is present on the default branch. If state pushes fail, check repository rules for `monitor-state` and the workflow's `contents: write` permission.

## Local checks

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
python monitor.py --dry-run
```

A preview never posts to Discord or updates delivery state. Use `--state` to preview against a specific state file.
