# facebook-ads-scraper

![release](https://img.shields.io/github/v/release/2scraper/facebook-ads-scraper?sort=semver)
![tests](https://github.com/2scraper/facebook-ads-scraper/actions/workflows/tests.yml/badge.svg)
![canary](https://github.com/2scraper/facebook-ads-scraper/actions/workflows/canary.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)
![no login](https://img.shields.io/badge/runs%20without-an%20account-success)

**Scrape Meta's Ad Library (facebook.com/ads/library) into clean JSON or
CSV, without logging in.** Search by keyword or by advertiser, in one
country or all of them; get one row per ad: its text, link, call to
action, every image and video URL, the dates it ran, the platforms it ran
on (Facebook, Instagram, Messenger, Audience Network, Threads), the
advertiser with its exact Page like count, and for political and issue
ads the spend, impressions and reach ranges and who paid for it.

- **No account, no cookies, no key.** The Ad Library is public, and this
  tool reads what it shows any logged-out visitor.
- **Measured live on 2026-10-05 and 2026-10-06** from an ordinary
  residential IP with no key and no proxy: 500 ads of one search in 2.5
  minutes, every ad read; 46 page loads in an hour with no login wall
  and no captcha. All three engines and the browserless Scraper API mode
  were run live, and Playwright and Puppeteer again through a 2Captcha
  residential proxy (60 of 60 and 40 of 40).
- **Honest results.** A blocked, stalled or partial run says so in its
  exit code and a `.meta.json` file next to the output, with the
  library's own result count beside the number of ads read. A run that
  finds nothing never overwrites your last good data.
- **Three browser engines** (Playwright, Puppeteer, Selenium) running one
  shared fetch loop, a browserless **Scraper API** mode, rotating proxies,
  2Captcha's **Scraping Browser API** over CDP, and a run-to-run diff tool.

## Quick start

```bash
git clone https://github.com/2scraper/facebook-ads-scraper.git
cd facebook-ads-scraper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install chromium

python3 playwright_scraper.py --query nike --region US --max-results 100
```

Results land in `facebook_ads.json`, with `facebook_ads.json.meta.json`
beside it. No `.env` is needed for this.

**When you need more than that:** for many searches in a row, a
specific exit country, or no browser on your machine, put a 2Captcha
residential proxy or key in `.env` (`cp .env.example .env`,
`FACEBOOK_PROXY=...`, `TWOCAPTCHA_KEY=...`). `python3 env_config.py`
shows what was picked up, without printing secrets.

## Examples

```bash
# a keyword search in one country, as CSV
python3 playwright_scraper.py --query "running shoes" --region US --format csv

# the exact phrase only, in every country, active and inactive ads
python3 playwright_scraper.py --query "air max" --exact-phrase --active-status all

# every ad of one advertiser (its numeric Page ID — the advertiser_page_id column)
python3 playwright_scraper.py --page-id 15087023444 --active-status all --max-results 500

# political and issue ads: adds spend, impressions, reach and "Paid for by"
python3 playwright_scraper.py --query election --region US --ad-type political_and_issue_ads

# a search copied from your browser's address bar, or a file of them (one per line, # comments)
python3 playwright_scraper.py --url "https://www.facebook.com/ads/library/?q=nike&country=DE"
python3 playwright_scraper.py --urls-file searches.txt

# the same with Puppeteer, or Selenium
python3 puppeteer_scraper.py --query nike --region US
python3 selenium_scraper.py --query nike --region US

# without a browser: 2Captcha's Scraper API (needs TWOCAPTCHA_KEY) — the first 30 ads of each search
python3 playwright_scraper.py --scraper-api --query nike --region US

# compare two runs
python3 diff_runs.py monday.json tuesday.json
```

## Sample output

One row of `sample_output.json`, cut from a live run with `--details` on
2026-10-06 (`--query nike --region DE`; long values shortened here):

```json
{
  "sku": "facebook-ad-1870984290951787",
  "source": "facebook.com",
  "category": "ad",
  "title": "Get the gear that's up for it all. Any time. Anywhere.",
  "brand": null,
  "price": null,
  "currency": null,
  "price_source": null,
  "product_url": "https://www.facebook.com/ads/library/?id=1870984290951787",
  "image_url": "https://scontent.fakx3-1.fna.fbcdn.net/v/t39.35426-6/7931448…",
  "scraped_at": "2026-10-06T12:44:15Z",
  "ad_id": "1870984290951787",
  "collation_id": null,
  "versions": 1,
  "is_active": true,
  "advertiser_name": "Nike",
  "advertiser_page_id": "15087023444",
  "advertiser_url": "https://www.facebook.com/nike/",
  "advertiser_likes": 39506245,
  "advertiser_categories_json": "[\"Sportswear\"]",
  "first_shown_at": "2026-09-02",
  "last_shown_at": "2026-10-06",
  "days_shown": 35,
  "platforms_json": "[\"FACEBOOK\", \"INSTAGRAM\", \"AUDIENCE_NETWORK\", \"MESSENGER\", \"THREADS\"]",
  "display_format": "DPA",
  "body": "Get the gear that's up for it all. Any time. Anywhere.",
  "link_url": "https://www.nike.com/",
  "link_title": "Nike Primary",
  "link_description": "Nike delivers innovative products, experiences and services to inspire athletes.",
  "link_caption": "http://nike.com/",
  "cta_text": "Shop now",
  "cta_type": "SHOP_NOW",
  "card_count": 6,
  "image_urls_json": "[\"https://scontent.fakx3-1.fna.fbcdn.net/v/t39.35426-6/79314…",
  "video_urls_json": null,
  "ad_categories_json": "[\"UNKNOWN\"]",
  "paid_for_by": null,
  "spend": null,
  "spend_currency": null,
  "impressions": null,
  "reach": null,
  "eu_reach": 7460923,
  "uk_reach": 2159546,
  "reach_breakdown_json": "[{\"country\": \"DE\", \"age\": \"18-24\", \"male\": 90018, \"female\": 55168, \"unknown\": 21…",
  "target_ages": "18-65",
  "target_gender": "All",
  "target_locations_json": "[\"Italy\", \"Netherlands\", \"Spain\", \"Poland\", \"France\", \"Germany\"]",
  "payer": "NIKE, Inc.",
  "beneficiary": "NIKE, Inc.",
  "advertiser_ig_username": "nike",
  "advertiser_ig_followers": 291040901,
  "advertiser_verification": "BLUE_VERIFIED",
  "region": "DE",
  "page": 0,
  "position": 2
}
```

What the columns mean:

| Column | |
|---|---|
| `sku` / `ad_id` | `facebook-ad-{Library ID}` / the Library ID the site prints on every card. Rows are deduplicated on it |
| `title` | the first line of the ad's text (at most 120 characters), else its link title, else the advertiser's name |
| `brand`, `price`, `currency`, `price_source` | always empty: family-wide columns that do not apply to an ad. A political ad's spend is a range in `spend` |
| `product_url` | the ad's own Library permalink |
| `versions`, `collation_id` | how many ads share this creative and text (the site's "2 ads use this creative and text") |
| `advertiser_*` | name, numeric Page ID, Page URL, its **exact** like count (a Page itself shows a rounded figure such as "39M"), its categories |
| `first_shown_at`, `last_shown_at`, `days_shown` | the dates it ran, in Pacific time, both days counted. For an active ad `last_shown_at` is today in Pacific time, not an end date |
| `platforms_json` | where it ran: FACEBOOK, INSTAGRAM, MESSENGER, AUDIENCE_NETWORK, THREADS, WHATSAPP |
| `display_format` | IMAGE, VIDEO, CAROUSEL, DPA (a catalogue ad), DCO (dynamic creative) |
| `body`, `link_*`, `cta_*`, `card_count` | the creative's text, link, domain line, call to action, number of cards |
| `image_urls_json`, `video_urls_json` | every image and video of the creative and its cards; a video's preview counts as an image |
| `ad_categories_json` | Meta's own categories: UNKNOWN for an ordinary ad, POLITICAL, … |
| `paid_for_by`, `spend`, `spend_currency`, `impressions`, `reach` | political and issue ads only; ranges as the library prints them (`$5K - $6K`, `>1M`) |
| `eu_reach`, `uk_reach`, `reach_breakdown_json` | with `--details`: people reached in the EU / UK (for ads delivered there) and the reach per country, age range and gender |
| `target_ages`, `target_gender`, `target_locations_json` | with `--details`: who the ad was aimed at ("18-65", "All", ["Worldwide"]) |
| `payer`, `beneficiary` | with `--details`: who paid for the ad and on whose behalf, as the library discloses them |
| `advertiser_ig_username`, `advertiser_ig_followers`, `advertiser_verification` | with `--details`: the advertiser's Instagram account and follower count, and its badge (BLUE_VERIFIED, …) |
| `region`, `page`, `position` | the country the search ran in (or ALL); 0 = the batch the page embeds, then one per scroll; rank within its search (each search in a run starts at 1) |

Media URLs are signed CDN links and stop working after some days.
Download what you need to keep.

## How a search is read

1. Open the library logged out, with the search in the URL and **an
   explicit country**: a search without `country=` runs in the visitor's
   own country (measured: a Kazakh address got `country=KZ`), so every URL
   this tool builds or accepts names one (`--region`, default ALL).
2. Wait for the results to paint. The library's shell arrives first and
   the results are streamed in after it; on 3 of 13 popular searches they
   were still missing 6 seconds after the page loaded. The tool waits up
   to 25 seconds and calls a page that never paints `not_painted` — not
   empty, not blocked.
3. Read the first batch — up to 30 ads — from the JSON the page embeds,
   with the library's own result total ("~24,000 results").
4. Wait for the list to be drawn. The data can arrive well before the
   page draws it: on a political-ads search on 2026-10-06 the list
   appeared 6–9 seconds after its data on 10 of 10 loads, and a run that
   scrolled at once hit a blank page and stopped at 30 ads on 4 of 10.
   The tool waits up to 30 seconds for the list before scrolling.
5. Scroll. Each scroll brings 10 more ads in a GraphQL response
   (`AdLibrarySearchPaginationQuery`), which the engine reads off the
   wire. There are no page numbers: the cursor is minted per visit, so
   the only way past the first 30 is to scroll the page that was served.
6. Stop at `--max-results`, at the end of the results (the library says
   there are no more), or after three scrolls in a row that bring nothing
   new — reported as `stalled`, a partial run, never as the end.

The same ad can come back twice in one search (39 distinct ads out of 40
on one live search), so rows are deduplicated on the Library ID, first
occurrence kept. A catalogue or dynamic ad (352 of the 549 ads captured)
carries a template such as `{{product.name}}` as its top-level text; the
tool then uses the first card's rendered text, so no text column holds
an unrendered template. `link_url` is the exception, on purpose: many
advertisers put Meta's URL macros in their tracking parameters
(`utm_campaign={{campaign.name}}` — 16 of 500 ads on one live search),
which Meta fills in when the ad is clicked. The tool keeps the URL as the
library publishes it rather than cut it into a different link.

**The library answers with HTTP 403 — and the full page.** Every
capture, direct and through a proxy, had status 403 on the main document
with the results in it. So the status code is not read as a block here:
the page's own content decides, and the status only matters for a page
that is not the library at all.

## Captchas

None, so far. No page in the live runs of 2026-10-05 and 2026-10-06
(Playwright, Puppeteer and Selenium, direct and through a residential
proxy, and the Scraper API) carried a captcha widget, iframe or
challenge, and none showed a login dialog. The generic captcha detection
stays on, so a real one would be reported as blocked rather than read as
data.

## Ad details (`--details`)

`--details` adds, for every ad, what the library shows in its "See ad
details" dialog: EU and UK reach with the breakdown by country, age range
and gender (for ads delivered there), the targeted ages, gender and
locations, the payer and beneficiary, and the advertiser's Instagram
account, follower count and verification badge.

How: the tool clicks "See ad details" once, keeps the request the page
sends (`AdLibraryV3AdDetailsQuery`), and sends that request again from the
page for each ad with only the ad's ids changed. Measured 2026-10-06: 30 of
30 in a row at 1.5 seconds apart, no limit hit; 15 of 15 ads of a German
search came back with all of these fields. It costs about 2 seconds per
ad. If Facebook answers "Rate limit exceeded", or a request fails, the
remaining ads keep their row without the extra columns, and the sidecar
says so per search (`details`, `details_read`) — the run itself is not
failed by it. The Scraper API mode has no live page to open the dialog in
and reports `details: unsupported`.

## Volume and blocks

From one residential IP on 2026-10-05, 46 page loads in an hour (direct
and proxied) and a 500-ad search (58 scrolls, 2.5 minutes) all read
normally. We have not found where the limit is. If the library starts
refusing an address, the tool reports it as **blocked** (a login page, or
a page that is not the library under 401/403/429), never as an empty
search. Without a proxy pool it stops after 3 blocked answers in a row
and reports the rest as `not_attempted`. To read more: rotate residential
exits with `--proxy-file` (each search gets the next exit, an exit that
keeps getting blocked is dropped, and once every exit is dropped the run
stops, never falling back to your own address), and keep
`--delay-between-pages` at 2 seconds or more.

**A datacentre address has not been measured yet.** The daily canary
runs from a GitHub runner only when a proxy secret is configured.

## Scraper API mode

With `--scraper-api` no browser is driven: each search is one 2Captcha
Scraper API call (`TWOCAPTCHA_KEY` in `.env`), routed through the
Scraping Browser profile in `FACEBOOK_CDP_ENDPOINT` when one is set.
There is nothing to scroll, so a search gives **the first 30 ads only**,
and a search with more results is marked `capped` in the sidecar (stop
`no_scroll`). Measured on 2026-10-06 on the Scraper API's own pool: 30
of 30 ads in 53 seconds. A refused key or an empty balance stops the run
at once with exit 5.

## Run results and exit codes

Every run writes `<out>` and `<out>.meta.json`: status, `stop_reason`,
the search URLs, per search the ads read, the library's own total, how
many scrolls and why it stopped (`limit`, `end`, `stalled`, `no_scroll`,
`empty`), failed searches with reasons, whether `--max-results` capped
it, solves spent, and a hash of the output file. A run that collects
nothing writes neither, so it never replaces your previous good file.

| Exit | Meaning |
|---|---|
| `0` | complete |
| `6` | partial: rows were written, but the run did not finish cleanly — `stop_reason` says why (`stalled`, `blocked`, `not_painted`, `failed_pages`, `rejected_rows`, `parse_error`, `remote_api_error`, `proxy_pool_exhausted`) |
| `3` | blocked, no rows |
| `4` | no rows: the library has no ads for any of the searches |
| `5` | nothing could be read (the page never painted, a fetch failure, or a Scraper API error); no rows |
| `2` | bad usage, including input where every line was skipped |
| `1` | crash (a bug; please report it) |

**CSV and safe writes.** In a CSV export, text that a spreadsheet would
run as a formula (starting with `=`, `+`, `-` or `@`, or with a literal
leading apostrophe) gets a leading `'`; the sidecar records this
(`csv_text_encoding: apostrophe-v1`) and `diff_runs.py` restores the
original text before comparing. JSON is unchanged. Every output file and
sidecar is written to a temporary file first and swapped in only once
complete, so a failed write never leaves a half-written file over the
previous good one.

## Monitoring changes

```bash
# what changed between two runs, as JSON
python3 diff_runs.py monday.json tuesday.json --json

# only some fields, and exit 1 if anything changed (for cron or CI)
python3 diff_runs.py monday.json tuesday.json --fields is_active,spend,impressions --fail-on-change
```

`diff_runs.py` matches ads by Library ID and lists, for each, the old and
new value of every watched field: active or not, number of versions,
text, link, call to action, format, the political spend, impressions and
reach ranges, and the advertiser's name and likes (with a `delta` for
numbers). `last_shown_at` and `days_shown` are not watched by default —
for an active ad they change every day by construction — but can be
asked for with `--fields`. `--fail-on-change` exits 1 when a watched
field changed or an ad appeared or disappeared.

The library re-ranks a search between visits, so when either run was
capped by `--max-results` an ad missing from the new run is listed as
`left_selection` (it fell out of the window), not `removed`. The diff
refuses comparisons that would mislead: different searches or
`--max-results`, a run that did not complete, and a `.meta.json` that
does not match its file.

## Options

Same flags for all three engines. Credentials go in `.env`
(`TWOCAPTCHA_KEY`, `FACEBOOK_PROXY`, `FACEBOOK_CDP_ENDPOINT`), never on
the command line.

| Option | Default | |
|---|---|---|
| `--query` | | keyword search, as typed into the library's search box |
| `--page-id` | | every ad of one advertiser: its numeric Page ID (`advertiser_page_id`) |
| `--exact-phrase` | off | match `--query` as an exact phrase |
| `--region` | ALL | two-letter country the ads were shown in, or ALL; also fills a pasted URL that names none |
| `--active-status` | active | `active`, `inactive` or `all` |
| `--ad-type` | all | `all` or `political_and_issue_ads` |
| `--details` | off | also read each ad's own details dialog (see [Ad details](#ad-details---details)); ~2s per ad |
| `--url` / `--urls-file` | | instead of the flags above: an Ad Library search URL as copied from a browser, or a file of them |
| `--max-results` | 100 | ads to read per search (the page embeds 30; each scroll adds 10) |
| `--delay-between-pages` | 2s | pause between searches |
| `--format` / `--out` | json / `facebook_ads.<format>` | output format and path |
| `--proxy` / `--proxy-file` / `--proxy-shuffle` | `FACEBOOK_PROXY` | one proxy or a rotating pool, for a local browser |
| `--proxy-block-retries` | 3 | blocked answers before that proxy is dropped from the pool |
| `--scraper-api` | off | no browser: fetch each search through 2Captcha's Scraper API (needs `TWOCAPTCHA_KEY`) — first 30 ads only |
| `--cdp-endpoint` | `FACEBOOK_CDP_ENDPOINT` | connect to a Scraping Browser API profile instead of launching a browser |
| `--solve-captcha` | when-blocked | `off` disables the Browser API's own captcha auto-solve; there is no local solver |
| `--max-solves` / `--min-score` | 8 / 0.3 | kept for the local solver, which is disabled; they change nothing today |
| `--retries` / `--retry-delay` | 2 / 3s | navigation retries per search |
| `--fingerprint` / `--fp-tags` / `--fp-country` | off | apply a 2Captcha Fingerprint API user agent (local browsers only) |
| `--dump-html` | off | save each search page's HTML next to the output, for debugging |
| `--allow-empty` | off | write an output file even when nothing was found |
| `--headless` / `--headful` | headless | show the browser window |

A pasted URL that is not a library search (another facebook.com page,
another site, a library URL with no search in it) is logged and skipped,
never requested. So is a **single-ad link (`?id=…`)**: opened logged out,
the library ran a search and returned a different ad of the same
advertiser (measured 2026-10-05), so the tool refuses it rather than
answer with the wrong row. Search the advertiser with `--page-id`
instead. Run any engine with `--help` for the full list.

## Engines

- **Playwright** (`playwright_scraper.py`) is the recommended engine.
- **Puppeteer** (`puppeteer_scraper.py`, via pyppeteer) supports the same
  modes. pyppeteer itself is no longer maintained, and its own proxy login
  no longer works on current Chromium, so this engine answers the proxy's
  password prompt itself (over CDP `Fetch`).
- **Selenium** (`selenium_scraper.py`) runs a local Chrome only and reads
  the scroll batches from Chrome's performance log. chromedriver cannot
  authenticate a Scraping Browser endpoint (the run exits 2 before
  fetching; use `--scraper-api` instead), and its `--proxy-server` cannot
  use a proxy password (the credentials are stripped, with a warning). A
  proxy that needs its password then answers HTTP 407 and the run says so
  (measured 2026-10-06 with a 2Captcha proxy); allow your IP in the
  proxy's settings instead, or use Playwright or Puppeteer.

Install one engine per virtualenv (`requirements-playwright.txt`,
`requirements-puppeteer.txt`, `requirements-selenium.txt`). Their
dependencies conflict with each other.

All three engines share one fetch loop (`page_flow.py`), so they agree on
results, exit codes and when money is spent. Docker:
`docker build -t facebook-ads-scraper .` gives an image with Playwright
and Chromium.

## Known limitations

- **The library caps its own count.** The broadest searches report
  "50,001" results; that is the library's ceiling, not the real number.
- **Ranges, not numbers.** Spend, impressions and reach are printed as
  ranges for political and issue ads and not at all for other ads, and
  the tool reports exactly what is printed.
- **Not collected, on purpose:** the contact details a political ad's
  disclosure carries (phone, e-mail, street address) — they are often a
  person's.
- **Dates are days, not times**, as the library publishes them (midnight
  Pacific time).

## Is this allowed?

The Ad Library is a transparency tool Meta publishes for anyone to read,
without an account. Meta's terms still restrict automated collection,
and Meta offers an official Ad Library API for some uses (political and
issue ads, and ads delivered in the EU). This tool reads only what a
logged-out visitor is shown, does not log in, and does not collect the
contact details in political disclosures. Whether your use is permitted
depends on your jurisdiction and purpose — check before you run it.

## Development

```bash
python3 smoke_test.py            # offline checks, no network, no engine needed
python3 .github/ci_checks.py     # credential scan
python3 -m unittest discover -s tests -p 'test_*.py'  # failure and recovery scenarios
```

Parser and flow checks run on real captures in `tests/fixtures/`. CI runs
the offline suite on Python 3.9 and 3.12, installs the built wheel
outside the checkout, builds the Docker image and launches Chromium in
it, and runs each engine in its own virtualenv. `TESTING.md` describes
live testing; `CHANGELOG.md` has the history.

## Licence

MIT, see `LICENSE`.
