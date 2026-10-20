# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org/) as closely as a CLI toolkit can: a patch
release means "fixes", not that every flag and default is frozen — a fix
that changes a default is called out at the top of its entry.

## [Unreleased]

> **Behaviour changes.** A local browser run through `--proxy` /
> `--proxy-file` now sends one `HEAD` for `facebook.com/robots.txt`
> through the proxy before the browser starts: a gateway `407` or `402`
> ends the run with exit 5 and `<out>.latest_attempt.meta.json`
> (`status: failed`, `stop_reason: proxy_auth_failed`), leaving the data
> and `<out>.meta.json` as they were. Behind a local proxy, images,
> video/audio and web fonts are no longer downloaded (`--load-media`
> restores them). Nothing changes over `--cdp-endpoint`, with
> `--scraper-api` or with no proxy.

### Added

- Proxy pre-check in all three browser engines. Any other answer, or a
  transport error, lets the run go on as before. Selenium checks the
  proxy without its password, as Chrome uses it there, and a `407` then
  says to allow your IP in the proxy's settings.
- Media blocking behind a local proxy: Playwright by resource type
  (`context.route`), Puppeteer by resource type over CDP `Fetch` (inside
  the interception that already answers a proxy password), Selenium by
  file extension (`Network.setBlockedURLs`). `<out>.meta.json` records
  `media_blocked`. New flag `--load-media` on every browser engine.

### Changed

- CI only: a failed canary check now always ends with an `::error::` line. A run
  through the `FACEBOOK_PROXY` secret that exited nonzero used to end red with no
  annotation, and a missing or wrong output file showed only a traceback.
  The message notes that a proxy rejecting its credential (HTTP 407) shows
  up in Chromium as a navigation timeout (exit 5), not as a 407.
- CI only: the canary now runs weekly (Mondays 06:41 UTC) and on a manual dispatch, with its optional secret `FACEBOOK_PROXY` passed on every run; the daily schedule is removed.
- CI only: the canary no longer requires 7 scroll *actions* for 100 ads. One scroll can bring two result batches: on 2026-10-20 the 100 ads arrived in 8 batches over 5 scrolls, and the run was marked red although it was complete. The check now requires at least one scroll, and keeps requiring rows from the 7th scroll batch or later. A smoke check runs the canary step against that run (`tests/fixtures/fb_ads_canary_nike_us_20261020.json`: the sidecar verbatim, rows cut to the checked columns, advertiser names and ids replaced by placeholders).

## [0.2.0] - 2026-10-15

> **Behaviour changes.** `selenium_scraper.py` and `puppeteer_scraper.py`
> now reject a negative `--retries`, `--retry-delay`,
> `--delay-between-pages` or `--max-solves` with exit 2, as
> `playwright_scraper.py` already did; all three now also reject a
> negative `--proxy-block-retries`. A page that states how many ads match
> but whose ads cannot be read now ends as `parse_error` (exit 5 with no
> rows, 6 with some) instead of `not_painted`. `diff_runs.py --json` no
> longer carries the always-empty `changed`, `source_changed` and
> `currency_changed` keys. Rewriting an existing output file now keeps
> that file's permissions.

### Fixed

- The three engines share one set of argument validators, so they accept
  and reject the same values with the same message.
- A Meta payload change that leaves the result count in place but the ads
  unreadable is reported as a parser failure, with the library's stated
  total kept in the sidecar, rather than as a page that never painted.
- `FACEBOOK_URL` in the environment or `.env` no longer makes
  `--query`/`--page-id` exit 2; a typed `--url` with `--query` is still
  refused.
- Searches never fetched because the run stopped early are listed in the
  sidecar's `unattempted_pages` and no longer counted in `failed_pages`.
- A CSV run's sidecar records `csv_cells_escaped`; a key the caller puts
  in the sidecar is no longer overwritten by the writer's own.
- `diff_runs.py` no longer compares price, currency or sort, which an ad
  does not have; its suite check uses Ad Library searches.
- The credential scan now reads `smoke_test.py` too (its credential-shaped
  fixtures are assembled from pieces) and fails if it read no file.
- The daily canary no longer skips green when `FACEBOOK_PROXY` is not
  set: it runs the search from the runner's own address and is red if the
  search is blocked, empty or incomplete (naming the secret in the error).
- Comments, the automated-review prompt and test names state their rules
  directly instead of pointing at a private document; a suite check keeps
  it that way. Comments that described other sites were removed.

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
