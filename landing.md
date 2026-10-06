# Facebook Ad Library Scraper by 2scraper

**Open-source scraper for Meta's Ad Library — every ad of a keyword or an advertiser, no login, three engines, JSON or CSV.**

Search the library by keyword or by advertiser, in one country or all of them, and get one row per ad: its text, link and call to action, every image and video URL, the dates it ran, the platforms it ran on, the advertiser with its exact Page like count — and for political and issue ads the spend, impressions and reach ranges and who paid for it.

[**View source on GitHub →**](https://github.com/2scraper/facebook-ads-scraper)

---

## Before you scrape

The Ad Library is a transparency tool Meta publishes for anyone to read without an account, but Meta's terms still restrict automated collection, and Meta offers an official Ad Library API for some uses. This tool reads only what a logged-out visitor is shown, never logs in, and does not collect the contact details in political disclosures. Whether your use is allowed depends on your jurisdiction and purpose: check first.

## What to expect

Every row comes from the data the library itself sends the page: the first 30 ads embedded in it, then 10 more per scroll. Measured live on 2026-10-05 and 2026-10-06 from an ordinary residential IP, with no key and no proxy: 500 ads of one search in 2.5 minutes, every ad read; 46 page loads in an hour with no login wall and no captcha. Playwright, Puppeteer and Selenium were all run live, and the browserless Scraper API mode too. A search that stops early says so (`stalled`, a partial run) instead of looking complete. Details in the [README](https://github.com/2scraper/facebook-ads-scraper#readme).

## What you get

- Free, open-source scraper, one script per engine — **Playwright** (recommended), **Puppeteer** (via pyppeteer) and **Selenium**, all producing the identical output schema and exit codes
- Keyword searches (`--query`, optionally `--exact-phrase`) and advertiser searches (`--page-id`), by country (`--region`), active or inactive, all ads or political and issue ads — or Ad Library URLs pasted from a browser, one at a time or a file of them
- Ad fields: Library ID, text, link, link title and description, call to action, format (image, video, carousel, catalogue, dynamic), card count, every image and video URL, Meta's ad categories, how many ads share the creative
- When and where: first and last day shown, days shown, platforms (Facebook, Instagram, Messenger, Audience Network, Threads)
- The advertiser: name, Page ID, Page URL, exact like count, Page categories
- Political and issue ads: spend, impressions and reach ranges, "Paid for by"
- `--details`: each ad's EU/UK reach by country, age and gender, its targeting, payer and beneficiary, and the advertiser's Instagram account and followers
- JSON and CSV export, with a documented `Product` schema and a `.meta.json` sidecar that records the library's own result count next to the number of ads read
- Change monitoring: `diff_runs.py` shows ads that started, stopped or changed between two runs
- A browserless mode (`--scraper-api`) that needs no browser driver installed — the first 30 ads of each search

## 2Captcha products, when you want them

| Product | What it's for |
|---|---|
| **Proxies — 2captcha.com/proxy** (2prx.com is the same product, different name) | Many searches in a row from several addresses: residential exits in `.env` or `--proxy-file`, rotated per search with per-exit failure tracking |
| **Scraper API — 2captcha.com** | No browser at all: `--scraper-api` fetches each search from 2Captcha's side, one HTTP call each (the first 30 ads; scrolling needs a browser engine) |
| **Scraping Browser API — 2captcha.com** | A remote browser session over CDP with its own proxy, fingerprint and captcha auto-solve bundled — `--cdp-endpoint` |
| **Browser fingerprints — 2captcha Fingerprint API** | Pick a Fingerprint API profile by OS and country for a locally-launched browser (applied as its user agent) |

## Who this is for

Marketers and agencies watching competitors' creatives, researchers and journalists studying political advertising, and anyone who wants Ad Library results in a spreadsheet or a script rather than a browser tab. Anything behind a login is out of scope.

## Get started

```bash
git clone https://github.com/2scraper/facebook-ads-scraper.git
cd facebook-ads-scraper
pip install -r requirements-playwright.txt && playwright install chromium

python3 playwright_scraper.py --query nike --region US --max-results 100 --format csv --out ads.csv
```

Full setup, CLI reference, and configuration details in the [repository README](https://github.com/2scraper/facebook-ads-scraper#readme).

---

**Need it running at scale, with proxies, fingerprints, and captcha solving already configured?**
[Talk to us →](https://2captcha.com/contact) · Proxies by [2captcha.com/proxy](https://2captcha.com/proxy) · Scraping Browser API & captcha solving by [2captcha.com](https://2captcha.com)
