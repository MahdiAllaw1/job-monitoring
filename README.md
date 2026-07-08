# Job Monitor

This repo monitors company career pages and sends Telegram alerts when new jobs appear.

## Modes

The GitHub Action has a manual input called `notify_mode`:

- `sample`: sends a small reference sample from each company and does not edit `jobs_state.json`.
- `all`: sends all detected jobs and does not edit `jobs_state.json`.
- `reset_baseline`: saves the currently detected jobs as already seen.
- `new`: normal mode. It alerts only jobs not already present in `jobs_state.json`.

Use this order after changing parsers or links:

1. Run `sample`.
2. Check the Telegram message.
3. If it looks good, run `reset_baseline`.
4. Leave the scheduled `new` mode running.

## Add or modify companies

Edit `companies.yml`.

Example with one link:

```yaml
  - name: Example Company
    adapter: browser
    url: "https://example.com/careers?country=France"
```

Example with multiple pages:

```yaml
  - name: IDEMIA
    adapter: auto
    urls:
      - "https://careers.idemia.com/search/?locationsearch=france"
      - "https://careers.idemia.com/search/?locationsearch=france&startrow=25"
```

## Adapters

Current adapters:

- `auto`: static HTML first, browser fallback.
- `browser`: Chromium/Playwright rendering, scrolling, and clicking show-more buttons.
- `arm`: ignores ARM's changing suggested-jobs block.
- `apple`: reads Apple France first-page results, currently 21 jobs.
- `workday`: uses Workday's jobs API.
- `eightfold`: tries Eightfold SmartApply and PCSX endpoints, then fallback.
- `teamtailor`: follows Teamtailor show-more links, used by IN Groupe, Dolphin, SiPearl.
- `secure_ic`: keeps only the real Secure-IC offers, not filters/categories.
- `icalps`: reads IC'Alps job headings.
- `menta`: reads Menta France job cards.
- `scalinx`: reads SCALINX job headings and their More links.
- `jobvite`: tries Arteris / Jobvite pages.

## Count warnings

`min_expected_jobs` is optional. It does not filter jobs. It only makes the bot warn you if a parser suddenly detects too few jobs.

Example:

```yaml
  - name: Apple
    adapter: apple
    url: "https://jobs.apple.com/fr-fr/search?location=france-FRAC"
    min_expected_jobs: 21
```

## Telegram secrets

In GitHub:

Settings → Secrets and variables → Actions

Required secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## GitHub Actions permission

Settings → Actions → General → Workflow permissions → Read and write permissions.

This lets the action commit `jobs_state.json`.
