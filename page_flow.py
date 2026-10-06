#!/usr/bin/env python3
"""page_flow.py — what one Ad Library search MEANS, and how far it is
scrolled, decided once for all engines (CLAUDE.md §1: three copies of this
triage would drift, and the drift would be silent — one engine reporting
exit 3 where its twin reports 0 on the same page).

The library answers a logged-out search these ways, all seen live on
2026-10-05 (see ad_parser.py):

  1. the results, under HTTP **403** → `content`. The status is not a
     block here: every good capture had it. So the page's own state is
     decided first, and the status only ever refines an `unknown` page;
  2. the library's shell with nothing painted yet → `loading`: WAIT,
     bounded, never retry (a retry re-rolls a page that was about to
     paint — CLAUDE.md §18);
  3. "No ads match your search criteria" → `empty`: a complete answer,
     zero rows, not a failure;
  4. a login or checkpoint page → `blocked` (never seen on this route,
     handled so that it cannot pass as an empty search);
  5. anything else (Chromium's own error page, an interstitial) →
     `blocked` under 401/403/429, a `fetch_error` otherwise.

A search has no page addresses: the first 30 ads are in the page, and
each scroll brings the next 10 through a GraphQL response the engine
captures (CLAUDE.md §18: one page kind may have no per-page addresses at
all). So one search is one browser session, scrolled until `--max-results`
ads are read, the library says there are no more (`has_next_page: false`),
or three scrolls in a row bring nothing new and leave the page height
unchanged (CLAUDE.md §8: three rounds, not one). The last is reported as
`stalled` — the run is partial, never complete-looking.

Triage is driver-independent; the shared async flow below consumes NAMED
engine operations — goto, content, current_url, wait, scroll_to_bottom,
page_height, take_responses, close — and no JavaScript crosses this
boundary.
"""
from __future__ import annotations

import logging as _logging
from dataclasses import asdict, dataclass, field
from pathlib import Path as _Path
from typing import List, Optional
from typing import Protocol as _Protocol

import ad_parser as ap
from captcha_solver import detect_from_html
from output_writer import Product
from output_writer import finish_run as _finish_run
from proxy_pool import is_proxy_dead_error as _is_proxy_dead_error, redact_credentials as _redact

_log = _logging.getLogger("page_flow")

BLOCKING_STATUSES = (401, 403, 429)
# With one exit and no pool to rotate through, a block is per IP: every later
# search gets the same answer. After this many in a row the run stops and
# reports the rest as not attempted, keeping what it has.
STOP_AFTER_CONSECUTIVE_BLOCKS = 3
# Measured 2026-10-05: three of 13 popular searches were still unpainted 6s
# after load and painted a few seconds later. 25s is generous for that and
# short enough that a page which never paints does not hang the run.
PAINT_WAIT_S = 25
UNKNOWN_WAIT_S = 8
POLL_S = 1.0
# One scroll brings one batch of 10 in about a second on a residential line;
# 2.5s leaves room for a slow exit without making a 100-ad search slow.
SCROLL_WAIT_S = 2.5
STALL_ROUNDS = 3
BATCH_SIZE = 10  # ads per scroll batch, measured
# The results can be in the page's data well before the list is drawn.
# Measured 2026-10-06 on a political-ads search: on 4 of 8 loads the page
# was still one blank viewport (scrollHeight 720) 7.5s after its data
# arrived, so three scrolls hit nothing and the run ended `stalled` at 30
# ads; 15s later the list was drawn (6,928px) and scrolling worked every
# time. So scrolling waits, bounded, until the document is taller than a
# blank page or has grown since the first look.
RENDER_WAIT_S = 30
RENDERED_MIN_HEIGHT_PX = 1500
CHALLENGE_WAIT_S = 15
# --details: one "See ad details" click to capture the page's own details
# request, then that request again per ad (measured 2026-10-06: 30 of 30 at
# 1.5s apart, no limit hit).
DETAILS_CLICK_WAIT_S = 6
DETAILS_CLICK_ATTEMPTS = 3
DETAILS_DELAY_S = 1.5


def is_challenge(html: str) -> bool:
    """A bot-challenge marker on a page that is NOT the library's own. The
    library's own pages never carried one; a page built from its shell is
    the site's, whatever strings it contains."""
    return ap.SHELL_MARKER not in (html or "") and detect_from_html(html or "", ap.BOT_CHALLENGE_MARKERS)


@dataclass
class SearchOutcome:
    url: str
    rows: List[Product] = field(default_factory=list)
    state: str = "unknown"          # content | empty | login | loading | unknown
    blocked: bool = False
    failure: Optional[str] = None   # fetch_error | not_painted | stalled | proxy_pool_exhausted | remote_api_error | parse_error
    total_results: Optional[int] = None
    has_next_page: Optional[bool] = None
    scrolls: int = 0
    rejected: int = 0
    stop: Optional[str] = None      # limit | end | stalled | no_scroll | empty
    details: Optional[str] = None   # with --details: done | rate_limited | no_request | unsupported | error
    details_read: int = 0


# --------------------------------------------------------------------------- #
# The engine boundary (CLAUDE.md §26)
# --------------------------------------------------------------------------- #
class PageSession(_Protocol):
    async def goto(self, url: str) -> Optional[int]: ...  # HTTP status or None; raises on failure
    async def content(self) -> str: ...
    async def current_url(self) -> str: ...
    async def wait(self, seconds: float) -> None: ...
    async def scroll_to_bottom(self) -> None: ...
    async def page_height(self) -> int: ...
    async def take_responses(self) -> List[str]: ...  # results-carrying GraphQL bodies since the last call
    async def close(self) -> None: ...
    # --details (an engine with can_details = False need not provide these):
    async def click_text(self, text: str) -> bool: ...  # click the first element whose text is exactly `text`
    async def take_requests(self) -> List[tuple]: ...  # (form body, answer) of the page's details requests since the last call
    async def post_form(self, data: str) -> tuple: ...  # (HTTP status, body) of a POST to /api/graphql/ from the page


class Engine(_Protocol):
    name: str
    readiness_s: float
    can_scroll: bool

    async def open(self, proxy) -> PageSession: ...
    async def sleep(self, seconds: float) -> None: ...
    async def solve_captcha(self, session: PageSession, *, html: str, url: str) -> Optional[dict]: ...


class SolveBudget:
    """One cap on PAID captcha solves for the whole run (CLAUDE.md §23: a
    per-page limit nothing sums is a bill). `limit=0` means never pay."""

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self.spent = 0

    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    def spend(self) -> None:
        self.spent += 1


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def resolve_urls(args) -> tuple:
    """(search URLs, skipped). `--query` / `--page-id` build one search;
    `--url` wins over `--urls-file`, each a pasted Ad Library search URL.
    Anything else is logged and skipped, never fetched; duplicates are
    fetched once."""
    region = ap.normalize_region(getattr(args, "region", None))
    if getattr(args, "query", None) or getattr(args, "page_id", None):
        if args.url or args.urls_file:
            raise ValueError("give --query/--page-id OR --url/--urls-file, not both: a pasted URL carries its own filters")
        return [ap.search_url(query=args.query, page_id=args.page_id, region=region,
                              active_status=args.active_status, ad_type=args.ad_type,
                              exact_phrase=args.exact_phrase)], 0
    if args.url:
        candidates = [args.url]
    elif args.urls_file:
        try:
            lines = _Path(args.urls_file).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ValueError(f"could not read --urls-file {args.urls_file!r}: {exc}")
        candidates = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    else:
        return [], 0
    urls, skipped = [], 0
    for text in candidates:
        url = ap.normalize_input(text, region=region)
        if url is None:
            _log.warning("Skipping %s — %s.", text, ap.refusal_reason(text))
            skipped += 1
            continue
        if url not in urls:
            urls.append(url)
    return urls, skipped


def _dump_path(out_path: str, index: int) -> str:
    return f"{_Path(out_path).with_suffix('')}_debug_{index}.html"


async def _solve_within_budget(engine: Engine, session: PageSession, args, *, html: str, url: str) -> Optional[dict]:
    budget = getattr(args, "_solve_budget", None)
    if budget is not None and budget.remaining() == 0:
        _log.warning("Captcha solving skipped: the run's solve budget is spent (--max-solves %d).", budget.limit)
        return None
    result = await engine.solve_captcha(session, html=html, url=url)
    if budget is not None and result and result.get("action") in ("solved", "warning_solver_error"):
        budget.spend()  # a task was created and billed, whatever came back
    return result


# --------------------------------------------------------------------------- #
# One search
# --------------------------------------------------------------------------- #
async def _wait_for_paint(session: PageSession) -> tuple:
    """(state, html) once the page is no longer `loading`, or still
    `loading` after PAINT_WAIT_S. A page that is not yet recognisable at
    all (`unknown`) gets UNKNOWN_WAIT_S too: measured 2026-10-05, one
    search looked 2s after load was still neither the library's shell nor
    anything else, and the same search a minute later painted normally —
    a verdict from the first look called that a block."""
    html = await session.content()
    state = ap.page_state(html, final_url=await session.current_url())
    waited = 0.0
    while (state == "loading" and waited < PAINT_WAIT_S) or (state == "unknown" and waited < UNKNOWN_WAIT_S):
        await session.wait(POLL_S)
        waited += POLL_S
        try:
            html = await session.content()
        except Exception:  # noqa: BLE001 — mid-navigation
            continue
        state = ap.page_state(html, final_url=await session.current_url())
    if waited and state in ("content", "empty"):
        _log.info("Results painted after %.0fs.", waited)
    return state, html


async def _scroll(engine: Engine, session: PageSession, collector: ap.Collector, outcome: SearchOutcome) -> None:
    """Scroll until the limit, the end of the results, or STALL_ROUNDS
    scrolls in a row with no new ad and no growth of the page."""
    if not getattr(engine, "can_scroll", True):
        outcome.stop = "no_scroll"
        return
    max_scrolls = collector.limit // BATCH_SIZE + 2 * STALL_ROUNDS + 5
    stalled = 0
    height = first_height = await session.page_height()
    waited = 0.0
    while (not collector.full and collector.has_next_page is not False and height <= RENDERED_MIN_HEIGHT_PX
           and height <= first_height and waited < RENDER_WAIT_S):
        await session.wait(POLL_S)
        waited += POLL_S
        height = await session.page_height()
    if waited:
        _log.info("The result list was drawn %.0fs after its data arrived." if height > first_height or
                  height > RENDERED_MIN_HEIGHT_PX else "The result list was still not drawn after %.0fs — scrolling anyway.",
                  waited)
    page = 0
    while not collector.full and collector.has_next_page is not False and outcome.scrolls < max_scrolls:
        await session.scroll_to_bottom()
        outcome.scrolls += 1
        await session.wait(SCROLL_WAIT_S)
        added = 0
        for body in await session.take_responses():
            page += 1
            added += collector.add(ap.response_batches(body), page=page)
        new_height = await session.page_height()
        if added == 0 and new_height <= height:
            stalled += 1
            if stalled >= STALL_ROUNDS:
                break
        else:
            stalled = 0
        height = max(height, new_height)
    if collector.full:
        outcome.stop = "limit"
    elif collector.has_next_page is False:
        outcome.stop = "end"
    else:
        outcome.stop = "stalled"
        outcome.failure = "stalled"
        _log.warning("%s: the library stopped sending results after %d ads with more announced "
                     "(%d scrolls, %d in a row brought nothing) — partial, not the end of the results.",
                     outcome.url, len(collector.rows), outcome.scrolls, stalled)


def _apply_details(row: Product, fields: dict) -> None:
    for key, value in fields.items():
        setattr(row, key, value)


async def _details(engine: Engine, args, session: PageSession, collector: ap.Collector, outcome: SearchOutcome) -> None:
    """Each ad's own details, in the site's order, until every row has them,
    a rate limit, or an error. A miss never costs the rows already read."""
    if not collector.rows:
        return
    if not getattr(engine, "can_details", False):
        outcome.details = "unsupported"
        _log.info("%s: --details needs a live page; this mode reads the search results only.", outcome.url)
        return
    await session.take_requests()  # anything the search itself sent is not ours
    # The button exists only once the result list is drawn, which can be
    # seconds after its data (RENDER_WAIT_S above); a search that needed no
    # scroll has not waited for that yet.
    # A click can land before the page answers clicks (seen with pyppeteer:
    # the button was there, the click sent nothing), so it is retried.
    requests: List[tuple] = []
    for _attempt in range(DETAILS_CLICK_ATTEMPTS):
        clicked, waited = False, 0.0
        while not clicked and waited < RENDER_WAIT_S:
            clicked = await session.click_text(ap.DETAILS_BUTTON_TEXT)
            if not clicked:
                await session.wait(POLL_S)
                waited += POLL_S
        waited = 0.0
        while clicked and not requests and waited < DETAILS_CLICK_WAIT_S:
            await session.wait(POLL_S)
            waited += POLL_S
            requests = await session.take_requests()
        if requests or not clicked:
            break
    if not requests:
        outcome.details = "no_request"
        _log.warning("%s: the ad details dialog sent no request — details left empty.", outcome.url)
        return
    form = requests[-1][0]
    by_id = {r.ad_id: r for r in collector.rows}
    for post, body in requests:  # the clicked ad's own answer counts
        fields = ap.details_fields(body)
        ad_id = ap.details_ad_id(post)
        if fields and ad_id in by_id:
            _apply_details(by_id.pop(ad_id), fields)
            outcome.details_read += 1
    for row in [r for r in collector.rows if r.ad_id in by_id]:
        meta = collector.ad_meta.get(row.ad_id) or {}
        data = ap.details_request(form, ad_id=row.ad_id, page_id=meta.get("page_id") or row.advertiser_page_id or "",
                                  political=bool(meta.get("political")), aaa_eligible=meta.get("aaa_eligible"))
        if data is None:
            outcome.details = "error"
            return
        await session.wait(DETAILS_DELAY_S)
        try:
            status, body = await session.post_form(data)
        except Exception as exc:  # noqa: BLE001
            outcome.details = "error"
            _log.warning("%s: a details request failed (%s) — %d of %d ads have details.", outcome.url,
                         _redact(str(exc)), outcome.details_read, len(collector.rows))
            return
        if ap.is_rate_limited(body):
            outcome.details = "rate_limited"
            _log.warning("%s: Facebook answered \"Rate limit exceeded\" — %d of %d ads have details; the rest are "
                         "left empty.", outcome.url, outcome.details_read, len(collector.rows))
            return
        fields = ap.details_fields(body) if status == 200 else None
        if fields is None:
            outcome.details = "error"
            _log.warning("%s: a details request answered HTTP %s without details — %d of %d ads have them.",
                         outcome.url, status, outcome.details_read, len(collector.rows))
            return
        _apply_details(row, fields)
        outcome.details_read += 1
    outcome.details = "done"


async def fetch_search(engine: Engine, args, url: str, index: int, proxy_pool) -> SearchOutcome:
    """One search: open it, wait for it to paint, read the embedded batch,
    scroll for the rest. Every failure is a typed outcome, never a raise."""
    outcome = SearchOutcome(url=url)
    proxy = proxy_pool.next() if proxy_pool is not None else None
    if proxy_pool is not None and proxy is None:
        outcome.failure = "proxy_pool_exhausted"
        return outcome
    _log.info("Searching %s (proxy: %s)", url, proxy.masked() if proxy else "(none — direct, or the --cdp-endpoint session's own exit)")
    try:
        session = await engine.open(proxy)
    except Exception as exc:
        _log.error("Browser connection failed — treating this search as failed, not a crash: %s", _redact(str(exc)))
        outcome.failure = "fetch_error"
        return outcome
    collector: Optional[ap.Collector] = None
    try:
        last_error, status = None, None
        for attempt in range(args.retries + 1):
            try:
                status = await session.goto(ap.fetch_url(url))
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 — every remote call must be bounded and reported
                last_error = _redact(str(exc)).splitlines()[0] if str(exc) else type(exc).__name__
                if proxy_pool is not None and proxy is not None and _is_proxy_dead_error(last_error):
                    proxy_pool.report_failure(proxy, dead=True)
                    _log.warning("Proxy reported dead: %s", last_error)
                else:
                    _log.warning("Navigation attempt %d/%d for %s failed: %s", attempt + 1, args.retries + 1, url, last_error)
                if getattr(engine, "fatal", None):
                    break  # a refused key or an empty balance: every retry would fail the same way
                if attempt < args.retries:
                    await engine.sleep(args.retry_delay)
        if last_error is not None:
            _log.error("%s permanently failed to load: %s", url, last_error)
            outcome.failure = "remote_api_error" if getattr(engine, "last_remote_error", False) else "fetch_error"
            return outcome

        state, html = await _wait_for_paint(session)
        if state == "unknown":
            waited = 0.0
            while is_challenge(html) and waited < CHALLENGE_WAIT_S:
                await session.wait(1)
                waited += 1
                html = await session.content()
            if is_challenge(html) and args.solve_captcha != "off":
                result = await _solve_within_budget(engine, session, args, html=html, url=url)
                if result and result.get("action") == "solved":
                    await session.wait(engine.readiness_s)
            state, html = await _wait_for_paint(session)
        outcome.state = state
        if args.dump_html:
            _Path(_dump_path(args.out, index)).write_text(html, encoding="utf-8")

        collector = ap.Collector(region=ap.url_region(url), limit=args.max_results)
        if state == "content":
            collector.add(ap.page_batches(html), page=0)
            for body in await session.take_responses():  # a batch that arrived while the page painted
                collector.add(ap.response_batches(body), page=0)
            await _scroll(engine, session, collector, outcome)
            if getattr(args, "details", False):
                await _details(engine, args, session, collector, outcome)
        elif state == "empty":
            _log.info("%s: the library has no ads for this search.", url)
            collector.has_next_page = False
            outcome.stop = "empty"
        elif state == "login":
            outcome.blocked = True
            _log.warning("%s: facebook.com sent this visitor to a login page — blocked, not empty. "
                         "Slow down (--delay-between-pages) or use another exit (--proxy-file).", url)
        elif state == "loading":
            outcome.failure = "not_painted"
            _log.warning("%s: the library's page loaded but its results never painted in %ds — nothing read. "
                         "Re-run with --dump-html to inspect it.", url, PAINT_WAIT_S)
        elif status == 407:
            # The PROXY's answer, not the site's (measured 2026-10-06: Selenium
            # strips a proxy's password, and the 2Captcha proxy then refuses).
            outcome.failure = "fetch_error"
            _log.warning("%s: the proxy refused this request — HTTP 407, proxy authentication required. "
                         "Selenium cannot send a proxy password: allow this machine's IP in the proxy's settings "
                         "(IP whitelist), or use playwright_scraper.py / puppeteer_scraper.py.", url)
        elif status in BLOCKING_STATUSES:
            outcome.blocked = True
            _log.warning("%s: HTTP %s and not an Ad Library page (%d bytes, title %r) — blocked, not empty.",
                         url, status, len(html or ""), ap.page_title(html))
        else:
            outcome.failure = "fetch_error"
            _log.warning("%s: HTTP %s, not an Ad Library page (%d bytes, title %r) — nothing read. "
                         "Re-run with --dump-html to inspect it.", url, status or "-", len(html or ""), ap.page_title(html))

        outcome.rows = collector.rows
        outcome.total_results = collector.total
        outcome.has_next_page = collector.has_next_page
        outcome.rejected = collector.rejected
        if proxy_pool is not None and proxy is not None:
            if outcome.blocked:
                proxy_pool.report_failure(proxy, dead=True)  # a block is a property of the EXIT (CLAUDE.md §8)
            else:
                proxy_pool.report_success(proxy)
        if getattr(engine, "last_remote_error", False) and not outcome.rows:
            outcome.failure = "remote_api_error"
        _log.info("%s: %d ads (of ~%s), stop=%s", url, len(outcome.rows),
                  outcome.total_results if outcome.total_results is not None else "?", outcome.stop or outcome.failure)
        return outcome
    except Exception as exc:  # noqa: BLE001
        _log.warning("Search failed for %s: %s", url, _redact(str(exc)))
        outcome.failure = "fetch_error"
        if collector is not None:  # a page that died mid-scroll keeps the ads it already gave (partial, not lost)
            outcome.rows, outcome.total_results = collector.rows, collector.total
            outcome.has_next_page, outcome.rejected = collector.has_next_page, collector.rejected
        return outcome
    finally:
        try:
            await session.close()
        except Exception as exc:  # noqa: BLE001
            _log.warning("Session cleanup failed: %s", _redact(str(exc)))


# --------------------------------------------------------------------------- #
# The ONE run loop
# --------------------------------------------------------------------------- #
async def run(engine: Engine, args, *, urls: List[str], proxy_pool, client, started_at: float) -> int:
    """Every search in input order, one browser session each."""
    outcomes: List[SearchOutcome] = []
    consecutive_blocks = 0
    for i, url in enumerate(urls, 1):
        outcome = await fetch_search(engine, args, url, i, proxy_pool)
        outcomes.append(outcome)
        consecutive_blocks = consecutive_blocks + 1 if outcome.blocked else 0
        stop = (outcome.failure == "proxy_pool_exhausted" or getattr(engine, "fatal", None)
                or (proxy_pool is None and consecutive_blocks >= STOP_AFTER_CONSECUTIVE_BLOCKS))
        if stop and i < len(urls):
            if proxy_pool is None and consecutive_blocks >= STOP_AFTER_CONSECUTIVE_BLOCKS:
                _log.error("%d blocked answers in a row from the same exit — stopping instead of asking again. "
                           "Rerun later, or with --proxy-file to rotate exits.", consecutive_blocks)
            outcomes.extend(SearchOutcome(url=u, failure="not_attempted") for u in urls[i:])
            break
        if i < len(urls):
            await engine.sleep(args.delay_between_pages)
    return finish_searches(args, outcomes, engine_name=engine.name, urls=urls, started_at=started_at)


def finish_searches(args, outcomes: List[SearchOutcome], *, engine_name: str, urls: List[str], started_at: float) -> int:
    products: List[Product] = []
    seen = set()
    failed_pages, failures = [], []
    for index, o in enumerate(outcomes, 1):
        for row in o.rows:
            if row.sku not in seen:  # an ad two searches both found is one row, the first search's
                seen.add(row.sku)
                products.append(row)
        if o.failure or o.blocked:
            failed_pages.append(index)
            failures.append({"url": o.url, "reason": o.failure or "blocked"})
    searches = [{"url": o.url, "state": o.state, "rows": len(o.rows), "total_results": o.total_results,
                 "has_next_page": o.has_next_page, "scrolls": o.scrolls, "stop": o.stop or o.failure,
                 "details": o.details, "details_read": o.details_read}
                for o in outcomes]
    totals = [o.total_results for o in outcomes if o.total_results is not None]
    return finish(args, products=products, blocked=any(o.blocked for o in outcomes),
                  remote_api_error=any(o.failure == "remote_api_error" for o in outcomes), engine_name=engine_name,
                  urls=urls, started_at=started_at, pages_completed=sum(1 for o in outcomes if not (o.failure or o.blocked)),
                  failed_pages=failed_pages, failures=failures,
                  capped=any(o.stop in ("limit", "no_scroll") and o.has_next_page for o in outcomes),
                  total_results=sum(totals) if totals else None, searches=searches,
                  rejected_rows=sum(o.rejected for o in outcomes))


_INCOMPLETE_REASONS = ("stalled", "not_painted", "parse_error", "proxy_pool_exhausted")


def finish(args, *, products: List[Product], blocked: bool, remote_api_error: bool, engine_name: str,
           urls: List[str], started_at: float, pages_completed: int, failed_pages: List[int],
           failures=None, capped: bool = False, total_results: Optional[int] = None, searches=None,
           rejected_rows: int = 0) -> int:
    budget = getattr(args, "_solve_budget", None)
    selection = {"mode": "search", "urls": urls, "max_results": args.max_results}
    if getattr(args, "details", False):
        selection["details"] = True
    extra = {"solves_spent": budget.spent if budget is not None else 0, "selection": selection,
             "failed_urls": failures or [], "searches": searches or []}
    return _finish_run(
        products=products, out_path=args.out, fmt=args.format, engine=engine_name, url=urls[0] if urls else "",
        pages_requested=len(urls), pages_completed=pages_completed,
        failed_pages=failed_pages or None, blocked=blocked, remote_api_error=remote_api_error,
        allow_empty=args.allow_empty, started_at=started_at, price_confirmed_pct=None,
        max_results=args.max_results, total_results=total_results, rejected_rows=rejected_rows,
        incomplete_reason=next((f["reason"] for f in (failures or []) if f["reason"] in _INCOMPLETE_REASONS), None),
        capped=capped, extra_meta=extra,
    )


def validate_common(args, *, urls, skipped, print_err) -> Optional[int]:
    """The usage checks every engine makes before launching anything;
    returns an exit code to stop with, or None to go on."""
    from output_writer import EXIT_BAD_USAGE
    if not urls:
        if skipped:
            print_err(f"Error: none of the inputs is an Ad Library search ({skipped} skipped) — nothing left to fetch")
        else:
            print_err("Error: provide --query, --page-id, --url or --urls-file")
        return EXIT_BAD_USAGE
    if args.format not in ("json", "csv"):
        print_err(f"Error: unsupported --format {args.format!r}")
        return EXIT_BAD_USAGE
    # Checked before any browser starts: the file is written only at the end,
    # so a missing directory used to crash (exit 1) after the whole scrape.
    out_dir = _Path(args.out).parent if getattr(args, "out", None) else _Path(".")
    if not out_dir.is_dir():
        print_err(f"Error: the --out directory {str(out_dir)!r} does not exist")
        return EXIT_BAD_USAGE
    return None


def add_search_arguments(p) -> None:
    """The search flags, identical on every engine."""
    p.add_argument("--query", default=None, help="Keyword search, as typed into the library's search box")
    p.add_argument("--page-id", default=None, help="Every ad of one advertiser: its numeric Facebook Page ID (the advertiser_page_id column)")
    p.add_argument("--exact-phrase", action="store_true", help="Match --query as an exact phrase (the library's own option)")
    p.add_argument("--region", default="ALL", help="Two-letter country code the ads were shown in, or ALL (default). Also fills a pasted URL that names none")
    p.add_argument("--active-status", choices=ap.ACTIVE_STATUSES, default="active", help="active (default, the library's own), inactive or all")
    p.add_argument("--details", action="store_true", help="Also read each ad's own details dialog: EU/UK reach by age, gender and country, targeting, payer and beneficiary, the advertiser's Instagram account and followers (one more request per ad, about 2s each)")
    p.add_argument("--ad-type", choices=ap.AD_TYPES, default="all", help="all (default) or political_and_issue_ads (adds spend, impressions and 'Paid for by')")
