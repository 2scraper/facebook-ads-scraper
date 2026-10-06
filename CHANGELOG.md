# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org/) as closely as a CLI toolkit can: a patch
release means "fixes", not that every flag and default is frozen — a fix
that changes a default is called out at the top of its entry.

## [Unreleased]

## [0.1.1] - 2026-10-06

Text only — no change to what is read or written.

### Fixed

- `--max-solves` help: the local solver is disabled, so it caps nothing
  today (the README already said so); the landing pages no longer promise
  a browser-specific fingerprint (`--fp-tags` filters by OS only).
- The exit-code table lists every `stop_reason` a partial run can carry.
- the landing pages no longer call the ad-details dialog out of scope (--details reads it); the --details help gives the measured ~2s per ad; TESTING names the run sample_output.* really comes from; ten fixtures, not eight.

## [0.1.0] - 2026-10-06

First release: Meta's Ad Library (facebook.com/ads/library), read logged
out. Run live on 2026-10-05 and 2026-10-06 with all three engines, the
Scraper API mode, and Playwright and Puppeteer through a residential
proxy (see `TESTING.md`).

### Added

- Keyword searches (`--query`, `--exact-phrase`) and advertiser searches
  (`--page-id`), filtered by `--region` (a country code or ALL),
  `--active-status` and `--ad-type` (all, or political and issue ads), or
  Ad Library search URLs pasted with `--url` / `--urls-file`. Every URL
  names its country explicitly: without one the library searches in the
  visitor's own country.
- One row per ad: Library ID, text, link, call to action, format, cards,
  every image and video URL, Meta's ad categories, versions sharing the
  creative, first and last day shown, platforms, the advertiser with its
  exact Page like count and categories, and for political and issue ads
  the spend, impressions and reach ranges and "Paid for by".
- Reading past the first 30 ads by scrolling the served page and reading
  each `AdLibrarySearchPaginationQuery` batch off the wire, in all three
  engines (Playwright and pyppeteer from response events, Selenium from
  Chrome's performance log). Stops at `--max-results`, at the end of the
  results, or after three scrolls in a row with nothing new — reported as
  `stalled`, a partial run.
- Waiting (bounded) for a page whose results are streamed in after the
  shell, instead of calling it empty, and for the result list to be drawn
  before the first scroll (without it, 4 of 10 live political-ads runs
  stopped at 30 ads).
- An `--out` directory that does not exist is bad usage (exit 2) before
  any browser starts, not a crash after the whole scrape.
- `--scraper-api`: one 2Captcha Scraper API call per search, no browser;
  the embedded batch only, marked `capped` when more are announced.
- `diff_runs.py` watches ad fields (active, versions, text, link, call to
  action, format, spend, impressions, reach, advertiser name and likes).
  In a capped run a missing ad is `left_selection`, not `removed`.
- `--details`: each ad's own details dialog — EU/UK reach with the
  breakdown by country, age range and gender, targeting, payer and
  beneficiary, the advertiser's Instagram account, followers and badge —
  by sending the page's own `AdLibraryV3AdDetailsQuery` again per ad.
- Every search is opened with `locale=en_US`: pyppeteer on a non-English
  machine got the library in another language, with no "See ad details"
  button to click.
- Offline suite on real, trimmed captures (ten fixtures), a failure and
  recovery suite, CI on Python 3.9 and 3.12 with a wheel, Docker and
  per-engine job, and a daily canary that needs a `FACEBOOK_PROXY` secret
  and skips without one.
- Hardening found by live runs on 2026-10-06, in the engine code shared
  with the companion facebook-pages-scraper and
  facebook-marketplace-scraper: pyppeteer's browser cleanup is bounded (it
  could hang for 20+ minutes after a proxy closed the connection), a login page
  served under the asked-for address is a block (exit 3) recognised by
  its canonical link, and an empty Scraper API answer is retried as an
  API error.
- Hardening from a security review before release: an unparseable proxy
  line is reported without echoing it (it can carry a password); the
  pyppeteer engine answers only its OWN proxy's auth challenge, once per
  request, and cancels any other (a site's HTTP auth never sees the
  proxy's credentials); CSV cells that a spreadsheet would run as a
  formula are escaped (`csv_text_encoding: apostrophe-v1`, undone by
  `diff_runs.py`); output files and sidecars are replaced atomically.

### Deliberately not done

- `?id=` single-ad links are refused: logged out, the library answered
  one with a different ad of the same advertiser.
- The contact details in a political ad's disclosure (phone, e-mail,
  address) are not collected.
- A page answered with HTTP 403 is not treated as blocked: every good
  page of the library carries that status.
