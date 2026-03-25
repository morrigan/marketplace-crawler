# Marketplace Crawler

This project checks marketplace search pages, remembers which listings were already seen, and sends a Resend email when a new matching item appears.

## What It Does

- Fetches one or more marketplace search URLs
- Extracts candidate listing links from the page HTML
- Filters them by your exact keywords
- Saves seen items to `data/state.json`
- Sends an email to the address set in `ALERT_RECIPIENT` when something new appears
- Avoids spamming old results on the first run by bootstrapping the current results as the baseline
- Prevents duplicate alerts for the same listing URL even if it appears in multiple saved searches

## Files

- `watcher.py`: main watcher script
- `config.example.json`: template for your marketplace definitions
- `.env.example`: template for your Resend API key
- `crontab.example`: sample twice-daily cron entry
- `tests/`: lightweight smoke tests and local fixtures, not a complete test suite

## Setup

1. Copy `config.example.json` to `config.json`.
2. Replace the example marketplace entries with your real marketplace URLs and keywords.
3. Copy `.env.example` to `.env`.
4. Set a valid Resend API key and `ALERT_RECIPIENT` in `.env`.
5. Set `email.from` in `config.json` to a sender address/domain verified in Resend.

## Config Notes

Each marketplace entry supports:

- `name`: label used in logs and emails
- `search_url`: exact marketplace search page URL
- `allowed_domains`: allowed domains for extracted listing links
- `candidate_url_patterns`: regex fragments used to keep only real listing URLs
- `keywords`: required keywords checked against the listing title and URL
- `match_mode`: `"all"` or `"any"`
- `exclude_keywords`: text that should exclude a listing
- `bootstrap_existing`: if `true`, the first run saves current matches without emailing them
- `max_items_per_run`: cap per marketplace
- `min_title_length`: helps drop navigation links
- `headers`: optional per-site HTTP headers
- `raw_listing_url_patterns`: optional regexes for sites that embed listing URLs in page JSON instead of normal anchor tags
- `blocked_markers`: optional text markers that should cause the run to fail instead of treating a captcha/block page as empty results

## Usage

Dry run:

```bash
python3 watcher.py --config config.json --dry-run
```

Normal run:

```bash
python3 watcher.py --config config.json
```

Force-bootstrap all currently visible listings:

```bash
python3 watcher.py --config config.json --bootstrap-all
```

Run only one marketplace by name:

```bash
python3 watcher.py --config config.json --only "Example Marketplace"
```

## Scheduling Twice A Day

Install the sample cron entry and adjust the times if needed:

```bash
crontab crontab.example
```

The provided example runs at `08:00` and `20:00` every day.

## TODO

- Expand automated tests beyond the current smoke checks in `tests/`
- Add tests for real marketplace HTML samples and parser regressions
- Add tests for state migration, duplicate suppression, and email payload formatting

## Limitations

This implementation is intentionally dependency-free. It works best on marketplace pages where listing links are present in the raw HTML. 
