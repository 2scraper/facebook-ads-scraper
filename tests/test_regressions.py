"""Failure and recovery scenarios: exercise the shared flow without network or paid tasks."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ad_parser as ap
import page_flow as flow
import playwright_scraper as playwright
import puppeteer_scraper as puppeteer
import selenium_scraper as selenium

FIX = Path(__file__).parent / 'fixtures'
SEARCH = (FIX / 'fb_ads_search_nike_us_20261005.html').read_text()
SCROLL = (FIX / 'fb_ads_scroll_nike_us_20261005.txt').read_text()
LOADING = (FIX / 'fb_ads_loading_amazon_20261005.html').read_text()
URL = ap.search_url(query='nike', region='US')


class Session:
    def __init__(self, engine):
        self.engine, self.reads = engine, 0

    async def goto(self, target):
        e = self.engine
        e.gotos += 1
        if e.mode == 'navigation_error':
            raise RuntimeError('navigation failed')
        if e.mode == 'recover_once' and e.gotos == 1:
            raise RuntimeError('net::ERR_TIMED_OUT')
        return 403

    async def content(self):
        self.reads += 1
        if self.engine.mode == 'content_races' and self.reads in (2, 3):
            raise RuntimeError('Execution context was destroyed, most likely because of a navigation')
        if self.engine.mode == 'content_races' and self.reads == 1:
            return LOADING
        return SEARCH

    async def current_url(self):
        return URL

    async def wait(self, seconds):
        return None

    async def scroll_to_bottom(self):
        if self.engine.mode == 'scroll_raises':
            raise RuntimeError('Target closed')

    async def page_height(self):
        return 1000

    async def take_responses(self):
        if self.engine.mode == 'bad_body':
            return ['{"data":{"ad_library_main":{"search_results_connection":{"edges":[{"node":{"collated_results":"oops"}}]}}}}',
                    'for (;;);{not json']
        return []

    async def close(self):
        self.engine.closed += 1
        if self.engine.mode == 'close_raises':
            raise RuntimeError('browser has disconnected')


class Engine:
    name = 'fake'
    readiness_s = 0
    can_scroll = True

    def __init__(self, mode):
        self.mode, self.gotos, self.closed, self.opened = mode, 0, 0, 0

    async def open(self, proxy):
        self.opened += 1
        if self.mode == 'open_raises':
            raise RuntimeError('Browser.newContext: Target page, context or browser has been closed')
        return Session(self)

    async def sleep(self, seconds):
        return None

    async def solve_captcha(self, session, *, html, url):
        return None


def run_flow(mode, *extra):
    args = playwright.build_arg_parser().parse_args(['--query', 'nike', '--region', 'US', '--max-results', '6', *extra])
    args._solve_budget = flow.SolveBudget(args.max_solves)
    engine = Engine(mode)
    with tempfile.TemporaryDirectory() as td:
        args.out = str(Path(td) / 'out.json')
        rc = asyncio.run(flow.run(engine, args, urls=[URL], proxy_pool=None, client=None, started_at=0.0))
        meta_p = Path(args.out + '.meta.json')
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else None
    return rc, meta, engine


class FailureScenarios(unittest.TestCase):
    def test_navigation_error_is_bounded_and_reported(self):
        rc, meta, engine = run_flow('navigation_error', '--retries', '2')
        self.assertEqual(rc, 5)
        self.assertEqual(engine.gotos, 3)
        self.assertEqual(engine.closed, 1)

    def test_one_navigation_error_recovers(self):
        rc, meta, engine = run_flow('recover_once')
        self.assertEqual(rc, 0)
        self.assertEqual(meta['product_count'], 6)

    def test_content_racing_a_navigation_is_waited_out(self):
        rc, meta, engine = run_flow('content_races')
        self.assertEqual(rc, 0)

    def test_a_page_that_dies_mid_scroll_keeps_the_ads_it_gave(self):
        rc, meta, engine = run_flow('scroll_raises', '--max-results', '50')
        self.assertEqual(rc, 6)
        self.assertEqual(meta['product_count'], 6)
        self.assertEqual(meta['failed_urls'][0]['reason'], 'fetch_error')
        self.assertEqual(engine.closed, 1)

    def test_malformed_scroll_bodies_are_skipped(self):
        rc, meta, engine = run_flow('bad_body', '--max-results', '50')
        self.assertEqual(rc, 6)
        self.assertEqual(meta['stop_reason'], 'stalled')

    def test_session_close_failure_is_not_a_crash(self):
        rc, meta, engine = run_flow('close_raises')
        self.assertEqual(rc, 0)

    def test_browser_open_failure_is_fetch_error(self):
        rc, meta, engine = run_flow('open_raises', '--allow-empty')
        self.assertEqual(rc, 5)
        self.assertEqual(meta['failed_urls'][0]['reason'], 'fetch_error')


class EngineParity(unittest.TestCase):
    def test_every_engine_parses_the_same_search(self):
        argv = ['--page-id', '15087023444', '--active-status', 'all', '--ad-type', 'political_and_issue_ads', '--exact-phrase']
        parsed = [vars(m.build_arg_parser().parse_args(argv)) for m in (playwright, selenium, puppeteer)]
        self.assertEqual(parsed[0], parsed[1])
        self.assertEqual(parsed[0], parsed[2])


if __name__ == '__main__':
    unittest.main()
