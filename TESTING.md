# Testing with real credentials and the live site

**Live status, 2026-10-05.** From one residential IP, logged out, no key
and no proxy, Playwright:

| Run | Result |
|---|---|
| `--query nike --region US --max-results 80` | 80/80 ads of ~24,353, 7 scrolls, exit 0, 22s |
| `--query nike --region US --max-results 500` | 500/500 ads of ~24,343, 58 scrolls, no stall, exit 0, 2.5 min |
| `--query election --region US --ad-type political_and_issue_ads --max-results 40` | 40/40, every row with `spend`, `impressions`, `paid_for_by`, exit 0 |
| a small advertiser's `--page-id` search | 3/3 ads, `stop: end` (the library said there were no more), exit 0, not capped |
| the same search three times (`--max-results 60`) | 60/60 each time, total ~2,067–2,070 |
| `--query zqxwvjkpqz --region US` | "No ads match your search criteria": exit 4, nothing written |
| 46 page loads in an hour (recon, direct and proxied) | every one served the library under HTTP 403 with its results; no login wall, no captcha |

Puppeteer, same day: `--query adidas --region DE --max-results 50`, 50/50,
exit 0.

**2026-10-06**, the remaining engines and modes, plus a 2Captcha EU
residential proxy (`FACEBOOK_PROXY` in the environment, never on the
command line):

| Engine | Run | Result |
|---|---|---|
| Playwright | `--query nike --region US --max-results 40` | 40/40 of ~24,416, exit 0, 10s — `sample_output.*` is cut from this run |
| Selenium | `--query adidas --region DE --max-results 50` | 50/50 of ~2,098, 3 scrolls, exit 0, 20s |
| Playwright + proxy | `--page-id 15087023444 --active-status all --max-results 60` | 60/60 of ~14,812, exit 0; the log names the exit, password masked |
| Puppeteer + proxy | `--query election --region US --ad-type political_and_issue_ads --max-results 40` | 40/40 of ~7,877, exit 0 |
| `--scraper-api`, own pool | `--query nike --region US` | 30/30 (the embedded batch), `capped: true`, `stop: no_scroll`, exit 0, 53s |
| `--scraper-api`, invalid key | `--query nike` | exit 5 after one call |

**Full pass, 2026-10-06 (later the same day):**

| Run | Result |
|---|---|
| `--query "air max" --exact-phrase --region US --format csv` | 45/45, header = the `Product` columns; 729 results vs 8,411 without `--exact-phrase` |
| `--query adidas --region GB --active-status inactive` | 40/40, every row `is_active: false`, every `last_shown_at` in the past |
| `--urls-file` (two searches, a duplicate, an `?id=` link, a Page URL) `--dump-html` | 70 rows (35 FR, 35 BR from one advertiser), the two non-searches skipped with their reasons, the duplicate fetched once, one dump per search |
| `--proxy-file` with two exits, three searches | exits alternate A, B, A; exit B was dead (curl through it failed too): that search `fetch_error`, the other two read, exit 6; no password in the log |
| the same search twice, then `diff_runs.py` | 0 added / 0 removed / 0 changed, `--fail-on-change` exit 0; a diff against another search refused |
| Puppeteer on a 3-ad advertiser | 3/3, `stop: end`, not capped |
| Selenium, `--query zqxwvjkpqz` | exit 4 |
| `--out /nonexistent_dir/x.json` | **before the fix:** the whole search ran, then exit 1 (crash) with nothing written; **after:** exit 2 before any browser starts |
| political-ads search, `--max-results 60`, 5 × Playwright + 5 × Selenium | **before the fix:** 4 of 10 stopped `stalled` at 30 ads — the list was not yet drawn when scrolling began; **after:** 10 of 10 complete, the list drawn 6–9s after its data each time; Puppeteer 3 of 3 |
| `--fingerprint --fp-tags Windows` | re-checked 2026-10-06: the Fingerprint API validates instantly but its backend times out / 504s, so the run completes without a fingerprint (exit 0, warned) and applying one is still unverified. `tags` takes a single OS — `Windows`, `Linux`, `Android` pass; `Chrome`, `macOS`, `iOS`, `Mobile` and comma lists (`Windows,Chrome`) are `400 ERROR_FINGERPRINT_BAD_REQUEST` |

Offline on the same day: the suite passes with no engine installed
(Python 3.14), with each engine in its own venv, in a directory holding
exactly the Dockerfile's COPY list, and from the built wheel's install;
every module parses as Python 3.9.

**Scraping Browser API (`FACEBOOK_CDP_ENDPOINT`, a `country-us` profile), 2026-10-06:**

| Engine | Run | Result |
|---|---|---|
| Playwright | `--query nike --region US --max-results 60` | 60/60 of ~24,444, 2 scrolls, exit 0 |
| Puppeteer | `--query election --region US --ad-type political_and_issue_ads --max-results 50` | 50/50 of ~7,960, exit 0 |
| Selenium | `--query nike` | exit 2 up front: chromedriver cannot authenticate the endpoint (documented) |
| `--scraper-api` routed through the profile | `--query nike --region US` | 30/30, `stop: no_scroll`, exit 0 |
| Playwright, wrong password in the endpoint | `--query nike` | exit 5, "credentials were refused (HTTP 401)", nothing written |

The endpoint's password appeared in none of these logs.

**Re-run of everything, 2026-10-06 (third pass):** 31 live scenarios —
Playwright, Selenium and Puppeteer direct; Playwright and Puppeteer
through the residential proxy and through the Scraping Browser API;
the Scraper API on its own pool and through the profile; empty, end of
results, bad input, bad key, bad endpoint password, missing `--out`
directory — all with the expected exit code and row contents, no
template outside `link_url`, no duplicate rows, no password in any log.
Political-ads search 5 × Playwright + 5 × Selenium: 10 of 10 complete.
Selenium through the password-protected proxy: exit 5, now logged as
the proxy's HTTP 407 with what to do (it was logged as "not an Ad
Library page" before).

**Fixed the same day, found by facebook-marketplace-scraper's live runs
in the engine code all three repos share:** pyppeteer's browser cleanup
could hang for 20+ minutes after a proxy closed the connection (now
bounded at 10s, a stuck local Chromium is killed — Puppeteer re-run live
here afterwards: exit 0); a login page served under the asked-for address
(the Scraper API's pool) is now a block, not an unreadable page; an empty
Scraper API answer is now retried as an API error.

**`--details`, 2026-10-06:** Playwright, `--query adidas --region DE
--max-results 15 --details`: 15/15 ads with EU reach, the age x gender x
country breakdown, targeting, payer, beneficiary and the advertiser's
Instagram account; Selenium and Puppeteer 8/8 each. Found on the way:
pyppeteer got the library in Russian on this machine (no English button
to click) — fixed by opening every search with `locale=en_US`. Sent 30
times in a row at 1.5s apart, the details request was never refused.

**After the pre-release security review, 2026-10-06:** the pyppeteer
engine through the password-protected residential proxy still read
normally (the proxy's own auth challenge answered, nothing else), with no
password in any log; a CSV run records `csv_text_encoding` and
`diff_runs.py` reads it back to the original text.

Not yet run live: the Docker image itself (no Docker on the test
machine; its build steps were run in a directory holding exactly the
files the Dockerfile copies), a working `--fingerprint`, and any run
from a datacentre IP. Each is covered by the offline suite, but has
no live evidence behind it yet.

The quickest real check:

```bash
python3 playwright_scraper.py --query nike --region US --max-results 40 --out /tmp/fb.json
cat /tmp/fb.json.meta.json        # status: complete, product_count: 40, total_results: ~24,000
```

## 1. Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install chromium
cp .env.example .env              # only for the paid paths below
python3 env_config.py             # what was picked up, secrets masked
```

## 2. One search, and what the rows should look like

```bash
python3 playwright_scraper.py --query nike --region US --max-results 40 --out /tmp/fb.json --dump-html
```

- exit 0, `status: complete`, `capped: true`, `searches[0].stop: limit`;
- 40 rows, `page` 0 for the first 30 and 1, 2… after that, `position`
  1–40 with no gaps;
- every row has `ad_id`, `advertiser_name`, `first_shown_at`,
  `platforms_json` and `display_format`; no column contains `{{`.

Open `https://www.facebook.com/ads/library/?id=<ad_id>` for a few rows and
compare. `/tmp/fb_debug_1.html` holds the page the first 30 were read
from.

## 3. The failure answers

```bash
python3 playwright_scraper.py --query zqxwvjkpqz --region US --out /tmp/e.json   # exit 4, nothing written
python3 playwright_scraper.py --url "https://www.facebook.com/ads/library/?id=1"  # exit 2, nothing requested
python3 playwright_scraper.py --query nike --region USA                         # exit 2
```

Keyword searches match any word, so a "nonsense" phrase made of real
words is not empty: `"zzqxw no such ad 88123"` returned 6,980 results on
2026-10-06. Use one made-up word.

## 4. Puppeteer and Selenium

```bash
pip install -r requirements-puppeteer.txt   # in its own venv
PYPPETEER_EXECUTABLE_PATH=/path/to/chromium python3 puppeteer_scraper.py --query nike --region US

pip install -r requirements-selenium.txt    # in its own venv
python3 selenium_scraper.py --query adidas --region DE --max-results 50
```

Same rows, same exit codes.

## 5. The residential proxy (`--proxy` / `FACEBOOK_PROXY`)

Put `FACEBOOK_PROXY=http://login:password@host:port` in `.env` and run
section 2 again. The log names the exit, never the password. For a pool,
one proxy per line in a file and `--proxy-file proxies.txt --proxy-shuffle`.

## 6. Scraper API mode (`--scraper-api`)

```bash
python3 playwright_scraper.py --scraper-api --query nike --region US --out /tmp/sapi.json
```

Needs `TWOCAPTCHA_KEY`; no browser driver. With `FACEBOOK_CDP_ENDPOINT`
also set, each call routes through that Scraping Browser profile. Expect
30 rows and `capped: true`; an invalid key must end the run with exit 5
after one call.

## 7. The Scraping Browser API (`--cdp-endpoint`)

```bash
python3 playwright_scraper.py --query nike --region US   # with FACEBOOK_CDP_ENDPOINT in .env
```

Selenium refuses a credentialled endpoint up front (exit 2): chromedriver
cannot authenticate one.

## 8. Push to GitHub and let CI do the rest

`tests.yml` runs the offline suite on Python 3.9 and 3.12, builds the
wheel and the Docker image, and runs one `engine-smoke` job per engine.
`canary.yml` needs the `FACEBOOK_PROXY` repo secret (a residential
proxy); without it the job skips with a notice. Dispatch it once by hand
and check both branches.

## 9. What "done" looks like

Every row in the tables above has a date and a number. Add the open items
from the top of this file to the table as they are run, and update the
README's claims from the same runs.
