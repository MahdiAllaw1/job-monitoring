# Job Monitor

This repository monitors selected company career pages and sends Telegram alerts when a new job appears.

## How it works

- `companies.yml` contains the company name, filtered career URL, and scraper adapter.
- `monitor.py` checks each source, stores the current job IDs in `jobs_state.json`, and alerts only on jobs not seen before.
- First run stores the baseline and sends a short initialization message. It does **not** spam old jobs.
- The GitHub Action runs every 30 minutes, plus manual runs via `workflow_dispatch`.

## Telegram setup

Keep the same secrets if you already used the Crous monitor:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

You do not need to change your Telegram chat unless you want alerts in another conversation/group.

## Add a company

Add a block in `companies.yml`:

```yaml
  - name: Company Name
    url: "https://filtered-career-url"
    adapter: generic
```

Use `adapter: generic` for normal HTML pages. Use `workday` for Workday sites and `eightfold` for Eightfold sites when configured.

## Reset baseline

To make the next run treat all current jobs as already seen again, replace `jobs_state.json` with:

```json
{
  "initialized": false,
  "companies": {},
  "last_checked_epoch": null
}
```

