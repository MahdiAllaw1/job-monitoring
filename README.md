# Job Monitor

This repository monitors selected career pages and sends Telegram alerts when a new job appears.

## Main behavior

- No technical keyword filtering is applied.
- The career URLs in `companies.yml` are treated as the filter source.
- The first normal run stores the current jobs as a baseline and does not spam old offers.
- Later normal runs send Telegram alerts only for jobs not already in `jobs_state.json`.
- JavaScript-heavy sites are handled with Playwright/Chromium.

## Important files

```text
companies.yml                  # Companies, links, adapters
monitor.py                     # Scraper + Telegram notifier
jobs_state.json                # Already-seen jobs, do not delete casually
.github/workflows/jobs.yml     # GitHub Actions schedule + manual test mode
```

## Add or edit a company

Simple company with one link:

```yaml
  - name: Company Name
    adapter: auto
    url: "https://career-link-after-filtering"
```

Company with several pages:

```yaml
  - name: Company Name
    adapter: auto
    urls:
      - "https://career-link-page-1"
      - "https://career-link-page-2"
```

Use `adapter: browser` for pages with buttons like "show more", "load more", or dynamic job cards.
Use `adapter: workday` for Workday links.
Use `adapter: eightfold` for Eightfold links.
Use `adapter: arm` only for ARM, because it removes the changing recommendation block.

## Manual verification without breaking the baseline

Go to:

```text
Actions → Job monitor → Run workflow
```

Choose one of these:

- `new`: normal behavior, alerts only new jobs.
- `sample`: sends a Telegram sample of jobs currently detected for each company. It does **not** modify `jobs_state.json`.
- `all`: sends all detected jobs. It does **not** modify `jobs_state.json`.
- `reset_baseline`: stores current jobs as already seen.

For checking whether the parser works, use `sample` first.

## Telegram secrets

Add these in GitHub repo settings:

```text
Settings → Secrets and variables → Actions
```

Required secrets:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

## GitHub Actions permissions

Because the workflow updates `jobs_state.json`, enable:

```text
Settings → Actions → General → Workflow permissions → Read and write permissions
```

## Reset baseline manually

Either run the workflow with `reset_baseline`, or replace `jobs_state.json` with:

```json
{
  "initialized": false,
  "companies": {},
  "last_checked_epoch": null
}
```
