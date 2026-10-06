#!/usr/bin/env python3
"""smoke_test.py — one file of plain functions with inline/synthetic-
fixture checks. No pytest, no conftest. `tests/test_smoke.py` wraps this as
a single pytest entry point so `pytest` also works, without a second copy
of the checks.

**What the fixtures are**: every fixture in `tests/fixtures/` is a REAL
logged-out capture of facebook.com/ads/library/ from 2026-10-05 — a US
keyword search, a German one, one advertiser's ads, a political-ads
search, a search with no results, a page captured before its results
painted, a single-ad `?id=` link and one scroll batch (a GraphQL
response) — trimmed to the library's shell and its one results block,
with a few ads kept per page; see each file's header comment. Political
ads' disclosure contacts are replaced with placeholders (the only part
not verbatim). Values are asserted against what those captures hold, not
just that a column is populated (CLAUDE.md §10).

Run directly: `python3 smoke_test.py`
"""
from __future__ import annotations

import asyncio
import inspect as _inspect
import json
import re
import tempfile
from pathlib import Path

import ad_parser as ap
import captcha_solver
import diff_runs
import env_config
import output_writer
import page_flow
import proxy_pool
import puppeteer_scraper
import scraper_api_client
import selenium_scraper

try:
    import playwright_scraper
except Exception as exc:  # pragma: no cover — this import itself must never fail
    raise AssertionError(f"playwright_scraper must import cleanly even without playwright installed: {exc}") from exc

ROOT = Path(__file__).parent

RESULTS = []  # (name, ok, detail)


def check(name):
    """Runs the decorated function IMMEDIATELY (at module-load time) and
    records the outcome — same pattern as every other family member's
    smoke_test.py; every check function is named `_` because only RESULTS
    is ever read, nothing looks a check up by name."""
    def decorator(fn):
        try:
            fn()
            RESULTS.append((name, True, ""))
        except AssertionError as exc:
            RESULTS.append((name, False, str(exc)))
        except Exception as exc:  # a check that crashes is still a failure, not an uncaught traceback
            RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        except SystemExit as exc:  # a CLI helper exiting inside a check would otherwise end the whole suite silently
            RESULTS.append((name, False, f"SystemExit: {exc}"))
        return fn
    return decorator


def asyncio_run_maybe(mod, args):
    """playwright_scraper.run()/puppeteer_scraper.run() are coroutines;
    selenium_scraper.run() is plain sync."""
    result = mod.run(args)
    if _inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


_ENGINES = (playwright_scraper, selenium_scraper, puppeteer_scraper)
_FIX = ROOT / "tests" / "fixtures"


def _fx(name):
    suffix = ".txt" if name.startswith("scroll") else ".html"
    return (_FIX / f"fb_ads_{name}_20261005{suffix}").read_text(encoding="utf-8")


_PAGES = ("search_nike_us", "search_sneaker_de", "page_nike", "political_us", "single_id", "noresults", "loading_amazon")
_NIKE_IDS = ["925321173274919", "2087470841874320", "1550534860016399", "530594316492410", "915491414586324", "921393146922718"]
_SCROLL_IDS = ["3877549952396564", "1523365825941922", "1683603072949110"]
_NIKE_URL = ap.search_url(query="nike", region="US")
_SAPI_ARGV = ["--query", "nike"]
_CHROMIUM_ERROR = ('<html><head><title>www.facebook.com</title></head><body><div id="main-message">'
                   '<h1>This site can’t be reached</h1><div class="error-code">ERR_TIMED_OUT</div></div></body></html>')


# --------------------------------------------------------------------------- #
# Engine import/CLI hygiene (CLAUDE.md §6)
# --------------------------------------------------------------------------- #
@check("engines import cleanly regardless of installed drivers")
def _():
    for mod in _ENGINES:
        assert hasattr(mod, "build_arg_parser")
        assert hasattr(mod, "run")


@check("each engine imports its driver at MODULE level, guarded by try/except ImportError (an ast walk, not a substring: a driver imported inside a function makes the no-driver skip meaningless — CLAUDE.md §10)")
def _():
    import ast as _ast
    drivers = {"playwright_scraper": "playwright", "selenium_scraper": "selenium", "puppeteer_scraper": "pyppeteer"}
    for mod in _ENGINES:
        tree = _ast.parse((ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8"))
        guarded = [n for n in tree.body if isinstance(n, _ast.Try)
                   and any(isinstance(h.type, _ast.Name) and h.type.id == "ImportError" for h in n.handlers)
                   and any(isinstance(s, _ast.ImportFrom) and (s.module or "").split(".")[0] == drivers[mod.__name__]
                           or isinstance(s, _ast.Import) and any(a.name.split(".")[0] == drivers[mod.__name__] for a in s.names)
                           for s in n.body)]
        assert guarded, f"{mod.__name__}: no module-level guarded import of {drivers[mod.__name__]}"


@check("no module uses a name it never imports, defines or assigns (compileall proves a file PARSES, not that its names RESOLVE — CLAUDE.md §10)")
def _():
    import ast as _ast
    import builtins
    for path in sorted(ROOT.glob("*.py")):
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        bound = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.Import, _ast.ImportFrom)):
                bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
            elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                bound.add(node.name)
                a = node.args if not isinstance(node, _ast.ClassDef) else None
                if a is not None:
                    bound |= {x.arg for x in a.args + a.kwonlyargs + a.posonlyargs}
                    bound |= {x.arg for x in (a.vararg, a.kwarg) if x is not None}
            elif isinstance(node, _ast.Name) and isinstance(node.ctx, (_ast.Store, _ast.Del)):
                bound.add(node.id)
            elif isinstance(node, _ast.arg):
                bound.add(node.arg)
            elif isinstance(node, _ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, _ast.alias):
                bound.add((node.asname or node.name).split(".")[0])
        used = {n.id for n in _ast.walk(tree) if isinstance(n, _ast.Name) and isinstance(n.ctx, _ast.Load)}
        assert not (used - bound), f"{path.name}: unresolved names {sorted(used - bound)}"


@check("no forbidden overclaiming wording in any shipped .py/.md/.yml file")
def _():
    # Built from pieces so this file can be scanned too (CLAUDE.md §22: the
    # check used to exempt its own file, where the phrases sat verbatim).
    anti = "anti" + "detect"
    banned = (
        "cloud" + " browser", anti + " browser", "2scraper " + anti + " browser",
        "gate." + "2prx.com", "--" + anti, anti + "_local_api",
    )
    exempt_names = {"CLAUDE.md"}
    venvs = {p.parent for p in ROOT.rglob("pyvenv.cfg")}
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in (".py", ".md", ".html", ".toml", ".cfg", ".yml", ".yaml"):
            continue
        if path.name in exempt_names or path.name.startswith("2scraper"):
            continue
        if ".git" in path.parts or "__pycache__" in path.parts or any(v in path.parents for v in venvs):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for phrase in banned:
            assert phrase not in text, f"{path.relative_to(ROOT)}: contains banned phrase {phrase!r}"


@check("nothing shipped still names the repo this one was bootstrapped from (instagram-scraper's modules, keys or output names)")
def _():
    leftovers = ("page_parser", "run_state", "INSTAGRAM_", "instagram_results", "instagram-scraper", "instagram.com/")
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in (".py", ".md", ".html", ".toml", ".yml", ".txt", ".example", ""):
            continue
        if ".git" in path.parts or "__pycache__" in path.parts or "fixtures" in path.parts or path.name == "smoke_test.py":
            continue
        if any(part.endswith(".egg-info") or part in ("build", "dist") or part.startswith(".venv") for part in path.parts):
            continue  # build artefacts, git-ignored and never shipped
        text = path.read_text(encoding="utf-8", errors="ignore")
        for word in leftovers:
            assert word not in text, f"{path.relative_to(ROOT)}: still says {word!r}"


@check("all three engines expose the identical --flag set (CLAUDE.md §4)")
def _():
    def flag_set(mod):
        return {opt for a in mod.build_arg_parser()._actions for opt in a.option_strings if opt.startswith("--")}

    pw, se, pu = flag_set(playwright_scraper), flag_set(selenium_scraper), flag_set(puppeteer_scraper)
    all_engines = pw | se | pu
    for name, flags in (("playwright_scraper", pw), ("selenium_scraper", se), ("puppeteer_scraper", pu)):
        missing = all_engines - flags
        assert not missing, f"{name} is missing {sorted(missing)} that (an)other engine(s) define — flag sets have drifted apart"


@check("all three engines share the same default output filename stem and the same defaults")
def _():
    for mod in _ENGINES:
        assert mod._default_out("json") == "facebook_ads.json"
        a = mod.build_arg_parser().parse_args(["--query", "nike"])
        assert (a.max_solves, a.delay_between_pages, a.retries, a.max_results, a.region, a.active_status, a.ad_type) == \
            (8, 2.0, 2, 100, "ALL", "active", "all"), mod.__name__


@check("every top-level module is in the Dockerfile COPY and pyproject py-modules (a module left out breaks the image on every run — CLAUDE.md §16)")
def _():
    if not (ROOT / "Dockerfile").exists() and not (ROOT / "pyproject.toml").exists():
        return  # the Docker image's own copy of this suite ships neither (CLAUDE.md §22)
    modules = sorted(pth.stem for pth in ROOT.glob("*.py"))
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    listed = set(re.findall(r'"([a-z_]+)"', pyproject.split("py-modules", 1)[1].split("]", 1)[0]))
    for mod in modules:
        assert f"{mod}.py" in docker, f"{mod}.py missing from the Dockerfile COPY"
        assert mod in listed, f"{mod} missing from pyproject py-modules"
    assert listed <= set(modules), f"pyproject lists modules that do not exist: {sorted(listed - set(modules))}"


@check("BOT_CHALLENGE_MARKERS is empty, and the generic markers match NONE of the real captures (CLAUDE.md §18: count a marker on a good page first)")
def _():
    assert ap.BOT_CHALLENGE_MARKERS == (), "no captcha vendor appeared on any Ad Library capture"
    for name in _PAGES:
        html = _fx(name)
        assert not captcha_solver.detect_from_html(html, ap.BOT_CHALLENGE_MARKERS), f"{name}: a generic marker fires on a real Ad Library page"
        assert not page_flow.is_challenge(html), name


# --------------------------------------------------------------------------- #
# output_writer — exit codes / precedence / dedupe (CLAUDE.md §9)
# --------------------------------------------------------------------------- #
@check("exit codes and STATUS_BY_EXIT match the family contract exactly")
def _():
    expected = {0: "complete", 1: "crashed", 2: "bad_usage", 3: "blocked", 4: "empty", 5: "remote_api_error", 6: "partial"}
    assert output_writer.STATUS_BY_EXIT == expected


def _mk_product(sku, price=None, **kw):
    defaults = dict(
        sku=sku, source="facebook.com", category="ad", title="An example ad text",
        brand=None, price=price, currency=None, price_source=None,
        product_url=f"https://www.facebook.com/ads/library/?id={sku}",
        image_url=None, scraped_at="2026-10-05T00:00:00Z",
        ad_id=sku, is_active=True, advertiser_name="Nike", last_shown_at="2026-10-05",
    )
    defaults.update(kw)
    return output_writer.Product(**defaults)


@check("finish_run: rows gathered by a run that did not finish are PARTIAL (6) with the cause in stop_reason — never 5/3 with a file (CLAUDE.md §25; audit 2026-09-30 got exit 5 AND a written file). Rewrites the old pinned 'exit 5 with products' position deliberately.")
def _():
    cases = (
        (dict(blocked=True, remote_api_error=True), "remote_api_error"),
        (dict(blocked=False, remote_api_error=True), "remote_api_error"),
        (dict(blocked=True, remote_api_error=False), "blocked"),
        (dict(blocked=False, remote_api_error=False, failed_pages=[3]), "failed_pages"),
        (dict(blocked=False, remote_api_error=False, rejected_rows=2), "rejected_rows"),
    )
    for kw, reason in cases:
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "out.json")
            kw = {"failed_pages": None, **kw}
            code = output_writer.finish_run(
                products=[_mk_product("1")], out_path=out, fmt="json", engine="test", url="u",
                pages_requested=3, pages_completed=2, allow_empty=False, started_at=0.0, **kw,
            )
            assert code == output_writer.EXIT_PARTIAL, (kw, code)
            assert Path(out).exists(), "already-collected products must still be written out"
            meta = json.loads(Path(f"{out}.meta.json").read_text())
            assert meta["status"] == "partial" and meta["stop_reason"] == reason, (kw, meta)
            if reason == "rejected_rows":
                assert meta["rejected_rows"] == 2
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "z.json")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=0,
            failed_pages=None, blocked=False, remote_api_error=True, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_REMOTE_API_ERROR and not Path(out).exists(), "5 promises no file"

@check("finish_run precedence: blocked+zero-products respects --allow-empty for WHETHER to write, never for the STATUS")
def _():
    with tempfile.TemporaryDirectory() as td:
        out_a = str(Path(td) / "a.json")
        code = output_writer.finish_run(
            products=[], out_path=out_a, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=[2],
            blocked=True, remote_api_error=False, allow_empty=True, started_at=0.0,
        )
        assert code == output_writer.EXIT_BLOCKED
        assert Path(out_a).exists(), "--allow-empty means a zero-product outcome DOES get written"
        meta = json.loads(Path(f"{out_a}.meta.json").read_text())
        assert meta["status"] == "blocked", "--allow-empty must never launder this into 'complete'"

        out_b = str(Path(td) / "b.json")
        code = output_writer.finish_run(
            products=[], out_path=out_b, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=[2],
            blocked=True, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_BLOCKED
        assert not Path(out_b).exists(), "without --allow-empty, a zero-product outcome writes nothing"


@check("finish_run: zero products without --allow-empty writes neither file nor sidecar")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_ZERO_PRODUCTS
        assert not Path(out).exists()
        assert not Path(f"{out}.meta.json").exists()


@check("finish_run: partial (failed pages, some products) writes output and reports EXIT_PARTIAL")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("a")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=2, pages_completed=1, failed_pages=[2],
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_PARTIAL
        assert Path(out).exists()
        meta = json.loads(Path(f"{out}.meta.json").read_text())
        assert meta["status"] == "partial"


@check("finish_run: a clean run with products writes output and reports EXIT_OK/complete")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("a"), _mk_product("b")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_OK
        data = json.loads(Path(out).read_text())
        assert len(data) == 2


@check("merge_pages dedupes by sku, last-write-wins, in fetch order not arrival order")
def _():
    batch1 = [_mk_product("a", title="A v1"), _mk_product("b", title="B")]
    batch2 = [_mk_product("a", title="A v2"), _mk_product("c", title="C")]  # "a" edited between runs
    merged = output_writer.merge_pages([batch1, batch2])
    skus = [p.sku for p in merged]
    assert skus == ["a", "b", "c"], f"expected batch-order with new items appended, got {skus}"
    a = next(p for p in merged if p.sku == "a")
    assert a.title == "A v2", "later batch's value must win for a repeated sku"


@check("write_csv writes a header even for zero rows")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.csv")
        output_writer.write_csv([], out)
        text = Path(out).read_text()
        assert text.strip() != ""
        assert "sku" in text.splitlines()[0]


# --------------------------------------------------------------------------- #
# proxy_pool — parsing, redaction, dead-marking (family-shared, no site knowledge)
# --------------------------------------------------------------------------- #
@check("proxy_pool rejects a malformed proxy string with ProxyParseError")
def _():
    try:
        proxy_pool.load_proxies("not a proxy!!", None)
        raise AssertionError("expected ProxyParseError")
    except proxy_pool.ProxyParseError:
        pass


@check("proxy_pool parses a credentialed proxy and masks it in logs")
def _():
    proxies = proxy_pool.load_proxies("http://user:secretpass@host.example:8080", None)
    assert len(proxies) == 1
    p = proxies[0]
    assert p.has_auth
    masked = p.masked()
    assert "secretpass" not in masked
    assert "host.example" in masked


@check("proxy_pool.redact_credentials strips login:password out of an arbitrary string")
def _():
    raw = "connect failed: ws://myuser:mysecret@cb.2captcha.com:9222 (5 attempts)"
    redacted = proxy_pool.redact_credentials(raw)
    assert "mysecret" not in redacted
    assert "myuser" not in redacted


# --------------------------------------------------------------------------- #
# captcha_solver — generic + widget-specific detection (family-shared)
# --------------------------------------------------------------------------- #
@check("captcha_solver.detect_from_html finds generic bot-challenge markers")
def _():
    assert captcha_solver.detect_from_html("<html>please complete the g-recaptcha below</html>")
    assert not captcha_solver.detect_from_html("<html><body>ordinary page, no widgets</body></html>")


@check("captcha_solver.identify_widget extracts a Turnstile sitekey")
def _():
    html = '<div class="cf-turnstile" data-sitekey="0x4AAA_example"></div>'
    signal = captcha_solver.identify_widget(html)
    assert signal is not None
    assert signal.captcha_type == captcha_solver.CaptchaType.CLOUDFLARE_TURNSTILE
    assert signal.sitekey == "0x4AAA_example"


# --------------------------------------------------------------------------- #
# env_config — FACEBOOK_* keys, placeholder detection, precedence
# --------------------------------------------------------------------------- #
@check("env_config.ENV_KEYS matches .env.example exactly, in both directions")
def _():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = {line.split("=", 1)[0] for line in example.splitlines() if "=" in line and not line.startswith("#")}
    assert documented == set(env_config.ENV_KEYS), (documented, set(env_config.ENV_KEYS))


@check("env_config uses FACEBOOK_ prefixed keys, not a leftover key of the repo this was bootstrapped from")
def _():
    for key in env_config.ENV_KEYS:
        assert key == "TWOCAPTCHA_KEY" or key.startswith("FACEBOOK_"), f"unexpected env key {key!r}"


@check("env_config._is_placeholder treats a braced {...} fragment as unset")
def _():
    assert env_config._is_placeholder("")
    assert env_config._is_placeholder(None)
    assert env_config._is_placeholder("{login}-zone-scraping_browser:{password}@cb.2captcha.com")
    assert not env_config._is_placeholder("a-real-looking-value-123")


@check("env_config.apply_env never overrides an explicitly-set CLI flag")
def _():
    import os as _os
    ns = __import__("argparse").Namespace(proxy="http://explicit:pass@host:1")
    _os.environ["FACEBOOK_PROXY"] = "http://from-env:pass@host:2"
    try:
        env_config.apply_env(ns, dotenv_path="/nonexistent/.env")
        assert ns.proxy == "http://explicit:pass@host:1"
    finally:
        del _os.environ["FACEBOOK_PROXY"]


# --------------------------------------------------------------------------- #
# ad_parser — inputs
# --------------------------------------------------------------------------- #
@check("search_url: a keyword search and an advertiser's ads, ALWAYS with an explicit country (a URL without one runs in the exit IP's country — measured KZ); bad region / page id / both inputs refused")
def _():
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(ap.search_url(query=" nike ", region="us")).query)
    assert q == {"active_status": ["active"], "ad_type": ["all"], "country": ["US"], "media_type": ["all"],
                 "q": ["nike"], "search_type": ["keyword_unordered"]}, q
    q = parse_qs(urlparse(ap.search_url(query="air max", exact_phrase=True)).query)
    assert q["search_type"] == ["keyword_exact_phrase"] and q["country"] == ["ALL"]
    q = parse_qs(urlparse(ap.search_url(page_id="15087023444", active_status="all", ad_type="political_and_issue_ads")).query)
    assert q["view_all_page_id"] == ["15087023444"] and q["search_type"] == ["page"] and "q" not in q
    assert q["active_status"] == ["all"] and q["ad_type"] == ["political_and_issue_ads"]
    for kw in (dict(query="x", region="USA"), dict(query="x", region="1"), dict(page_id="nike"), dict(query="x", page_id="1234"),
               dict(), dict(query="x", active_status="paused"), dict(query="x", ad_type="housing")):
        try:
            ap.search_url(**kw)
            raise AssertionError(f"accepted {kw}")
        except ValueError:
            pass


@check("normalize_input: a pasted library search keeps its own filters and gains a country only when it names none; another page, another host, an empty search and a single-ad ?id= link are refused WITH the reason")
def _():
    url = ap.normalize_input("facebook.com/ads/library/?q=nike&country=de", region="US")
    assert ap.url_region(url) == "DE" and "q=nike" in url, url
    url = ap.normalize_input("https://www.facebook.com/ads/library?view_all_page_id=15087023444", region="FR")
    assert ap.url_region(url) == "FR" and "active_status=active" in url
    assert ap.normalize_input("https://m.facebook.com/ads/library/?q=x") == ap.normalize_input("https://web.facebook.com/ads/library/?q=x")
    for bad in ("https://www.facebook.com/nike", "https://example.com/ads/library/?q=nike", "https://www.facebook.com/ads/library/",
                "https://www.facebook.com/ads/library/?id=3877549952396564", "", "nike", "https://www.facebook.com/marketplace/?q=x"):
        assert ap.normalize_input(bad) is None, bad
    assert "different ad" in ap.refusal_reason("https://www.facebook.com/ads/library/?id=3877549952396564")
    assert "no search" in ap.refusal_reason("https://www.facebook.com/ads/library/")
    assert "not a facebook.com" in ap.refusal_reason("https://example.com/")


@check("the ?id= refusal is pinned on the REAL capture: ?id=3877549952396564 was answered with ONE ad, 1382287983621487 — a different ad of the same advertiser")
def _():
    batch = ap.page_batches(_fx("single_id"))[0]
    assert [a["ad_archive_id"] for a in batch.ads] == ["1382287983621487"] and batch.total == 1 and batch.has_next_page is False


# --------------------------------------------------------------------------- #
# ad_parser — the REAL captures
# --------------------------------------------------------------------------- #
@check("page_state on every REAL capture: results = content (under HTTP 403!), the no-results page = empty, the unpainted shell = loading; Chromium's own error page = unknown; a login redirect = login, never empty")
def _():
    expected = {"search_nike_us": "content", "search_sneaker_de": "content", "page_nike": "content", "political_us": "content",
                "single_id": "content", "noresults": "empty", "loading_amazon": "loading"}
    for name, state in expected.items():
        assert ap.page_state(_fx(name)) == state, (name, ap.page_state(_fx(name)))
    assert ap.page_state(_CHROMIUM_ERROR) == "unknown"
    assert ap.page_state("<html><title>Log in</title></html>", final_url="https://www.facebook.com/login/?next=x") == "login"
    assert ap.page_state(_fx("noresults"), final_url="https://www.facebook.com/login/") == "empty", "the library's own words outrank the URL"
    assert ap.page_title(_fx("loading_amazon")) == "Ad Library"


@check("the results block: count, has_next_page and cursor as served (24,402 ads for nike/US; 0 and no next page for the empty search); one undecodable block does not hide the good one")
def _():
    b = ap.page_batches(_fx("search_nike_us"))
    assert len(b) == 1 and len(b[0].ads) == 6 and b[0].total == 24402 and b[0].has_next_page is True and b[0].end_cursor
    assert [a["ad_archive_id"] for a in b[0].ads] == _NIKE_IDS, "the site's order"
    assert [(x.total, x.has_next_page, len(x.ads)) for x in ap.page_batches(_fx("noresults"))] == [(0, False, 0)]
    assert ap.page_batches(_fx("loading_amazon")) == []
    broken = '<script type="application/json">{"search_results_connection": {oops</script>' + _fx("search_nike_us")
    assert len(ap.page_batches(broken)) == 1


@check("a scroll batch (REAL AdLibrarySearchPaginationQuery body): one JSON document per line, Facebook's for (;;); guard tolerated, junk lines skipped; is_results_response decides on the BODY")
def _():
    body = _fx("scroll_nike_us")
    assert ap.is_results_response(body) and not ap.is_results_response('{"data":{"viewer":null}}')
    b = ap.response_batches(body)
    assert [a["ad_archive_id"] for x in b for a in x.ads] == _SCROLL_IDS and b[0].has_next_page is True
    guarded = "for (;;);" + body.strip() + "\n\nnot json\n{\"extensions\":{}}\n"
    assert [a["ad_archive_id"] for x in ap.response_batches(guarded) for a in x.ads] == _SCROLL_IDS


def _rows(name, region="US"):
    text = _fx(name)
    batches = ap.response_batches(text) if name.startswith("scroll") else ap.page_batches(text)
    c = ap.Collector(region=region, limit=1000)
    c.add(batches, page=0, scraped_at="2026-10-05T00:00:00Z")
    return {r.ad_id: r for r in c.rows}


@check("ad_row on the REAL nike/US search: values, not coverage (CLAUDE.md §10) — a VIDEO ad, a DPA, an IMAGE ad, a CAROUSEL, a DCO whose top-level text is a template, an IMAGE ad with no text")
def _():
    rows = _rows("search_nike_us")
    r = rows["925321173274919"]
    assert (r.sku, r.source, r.category, r.product_url) == ("facebook-ad-925321173274919", "facebook.com", "ad",
                                                           "https://www.facebook.com/ads/library/?id=925321173274919")
    assert (r.brand, r.price, r.currency, r.price_source) == (None, None, None, None)
    assert (r.advertiser_name, r.advertiser_page_id, r.advertiser_url) == ("Nordstrom Rack", "89516513179", "https://www.facebook.com/NordstromRack/")
    assert r.advertiser_likes == 2518793 and json.loads(r.advertiser_categories_json) == ["Clothing (Brand)"]
    assert (r.first_shown_at, r.last_shown_at, r.days_shown) == ("2026-08-03", "2026-10-05", 64)
    assert (r.display_format, r.versions, r.collation_id, r.is_active) == ("VIDEO", 2, "1022906600523558", True)
    assert json.loads(r.platforms_json) == ["FACEBOOK", "INSTAGRAM", "MESSENGER"]
    assert r.title == r.body == "Buy online and pick up in store for free! Get up to 70% off Nike, Vince, Madewell, adidas and more."
    assert (r.link_title, r.link_caption, r.cta_text, r.cta_type, r.card_count) == ("Fast & Free Pickup", "Nordstromrack.com", "Learn more", "LEARN_MORE", 0)
    assert len(json.loads(r.video_urls_json)) == 1 and r.image_url == json.loads(r.image_urls_json)[0], "a video's preview is its image"
    assert json.loads(r.ad_categories_json) == ["UNKNOWN"] and r.spend is None and r.paid_for_by is None and r.region == "US"

    dpa = rows["2087470841874320"]
    assert dpa.display_format == "DPA" and dpa.card_count == 5 and dpa.advertiser_likes == 6728649 and dpa.days_shown == 76
    assert len(json.loads(dpa.image_urls_json)) >= 2, "a catalogue ad has one image per card"
    car = rows["530594316492410"]
    assert car.display_format == "CAROUSEL" and car.card_count == 5 and car.first_shown_at == "2024-10-29" and car.days_shown == 707
    assert car.title.endswith("…") and len(car.title) == 120, "the title is the first line, capped at 120"
    dco = rows["915491414586324"]
    assert dco.display_format == "DCO" and dco.body.startswith("More brands than the eye can see"), "the first card's text, not '{{product.brand}}'"
    assert dco.link_title is None and json.loads(dco.platforms_json) == ["INSTAGRAM"]
    bare = rows["921393146922718"]
    assert bare.body is None and bare.title == "Neon Love Theme", "no text: the link title, never empty"
    assert rows["1550534860016399"].video_urls_json is None and rows["1550534860016399"].display_format == "IMAGE"


@check("no TEXT column of any row of any REAL capture holds an unrendered template ({{product.name}}) — catalogue ads (352 of 549 captured) carry them at the top level; link_url keeps Meta's URL macros as published (pinned: filled in at click time, cutting them would make another link)")
def _():
    raw = _fx("search_nike_us") + _fx("search_sneaker_de")
    assert "{{product." in raw, "the captures do carry templates — this check must have something to catch"
    for name in ("search_nike_us", "search_sneaker_de", "page_nike", "political_us", "single_id", "scroll_nike_us"):
        for row in _rows(name).values():
            for key, value in vars(row).items():
                assert key == "link_url" or not (isinstance(value, str) and "{{" in value), (name, row.ad_id, key, value)
    macro = "https://www.shiekh.com/new.html?utm_source=Paid+Social&utm_medium={{placement}}&utm_campaign={{campaign.name}}"
    assert ap.ad_row({"ad_archive_id": "8932462163548008", "snapshot": {"link_url": macro}}, region="US", page=0,
                     position=1, scraped_at="x").link_url == macro, "measured live 2026-10-05: the URL as the library publishes it"
    jd = _rows("search_sneaker_de")["4638101539846452"]
    assert jd.body.startswith("Entdecke freshe Styles") and jd.link_caption == "jdsports.de", "German, from the first card"


@check("a political ad on the REAL capture: spend and impressions are RANGE strings in their own columns (never a price), 'Paid for by' is the disclaimer, and the advertiser's contact details are NOT collected")
def _():
    rows = _rows("political_us")
    r = rows["1086114250492099"]
    assert (r.spend, r.spend_currency, r.impressions, r.reach) == ("$5K - $6K", "USD", ">1M", ">1M")
    assert r.paid_for_by == "Office of the Texas Secretary of State" and r.advertiser_name == "VoteTexas"
    assert json.loads(r.ad_categories_json) == ["POLITICAL"] and r.price is None and r.currency is None
    assert (r.first_shown_at, r.last_shown_at, r.days_shown) == ("2026-09-08", "2026-10-05", 28)
    mi = rows["1971756050186491"]
    assert mi.paid_for_by == "Michigan Department of State" and mi.versions == 3 and mi.advertiser_likes == 77275
    for name in ("phone", "email", "address", "fev"):
        assert not any(name in f for f in output_writer.PRODUCT_FIELD_NAMES), f"{name}: a disclosure contact is often a person's"


@check("dates are MIDNIGHT PACIFIC epochs: 07:00 UTC (PDT) and 08:00 UTC (PST) both land on the right calendar day; zero/negative/bool are None")
def _():
    import datetime as dt
    assert ap._date(1788850800) == dt.date(2026, 9, 8)          # 2026-09-08 07:00 UTC
    assert ap._date(1798790400) == dt.date(2027, 1, 1)          # 2027-01-01 08:00 UTC
    for bad in (0, -5, True, None, "1788850800"):
        assert ap._date(bad) is None, bad


@check("the advertiser endpoint (view_all_page_id) on the REAL capture: every row is that advertiser's, with its EXACT like count (the Page itself shows only '39M')")
def _():
    rows = _rows("page_nike", region="ALL")
    assert set(rows) == {"1702938977100376", "1559738104701476"}
    for r in rows.values():
        assert r.advertiser_page_id == "15087023444" and r.advertiser_likes == 39506703 and r.region == "ALL"
    assert rows["1559738104701476"].title == "Encuentra tu mejor estilo.", "the first line of a multi-line body"
    assert ap.page_batches(_fx("page_nike"))[0].total == 14864


@check("Collector: rows in the site's order, deduped on the Library ID (first kept), capped at --max-results, a malformed ad counted as rejected and never a crash; the largest announced total wins")
def _():
    embedded, scroll = ap.page_batches(_fx("search_nike_us")), ap.response_batches(_fx("scroll_nike_us"))
    c = ap.Collector(region="US", limit=100)
    assert c.add(embedded, page=0) == 6 and c.add(embedded, page=1) == 0, "the same ad again is not a new row"
    assert c.add(scroll, page=1) == 3 and [r.ad_id for r in c.rows] == _NIKE_IDS + _SCROLL_IDS
    assert [r.position for r in c.rows] == list(range(1, 10)) and c.rows[-1].page == 1 and c.total == 24402
    c = ap.Collector(region="US", limit=4)
    c.add(embedded, page=0)
    assert len(c.rows) == 4 and c.full
    bad = ap.Batch(ads=[{"ad_archive_id": "not-digits"}, {"ad_archive_id": "123", "snapshot": "oops", "start_date": "x"}])
    c = ap.Collector(region=None, limit=10)
    assert c.add([bad], page=0) == 1 and c.rejected == 1 and c.rows[0].sku == "facebook-ad-123"


@check("--details helpers on the REAL captured AdLibraryV3AdDetailsQuery: the page's own form is recognised; the request for another ad changes ONLY its ids and the two flags the page derives from it; the answer gives EU/UK reach (109,709 / 6,846), the age x gender x country breakdown, targeting, payer/beneficiary and the advertiser's Instagram (2,539,601 followers)")
def _():
    from urllib.parse import parse_qsl
    form = (_FIX / "fb_ads_details_form_20261006.txt").read_text(encoding="utf-8").splitlines()[-1]
    answer = (_FIX / "fb_ads_details_answer_20261006.txt").read_text(encoding="utf-8")
    assert ap.is_details_request(form) and ap.details_ad_id(form) == "1089391903605576"
    other = ap.details_request(form, ad_id="4638101539846452", page_id="669832573062940", political=True, aaa_eligible=True)
    before, after = dict(parse_qsl(form)), dict(parse_qsl(other))
    assert {k for k in before if before[k] != after[k]} == {"variables"}
    vb, va = json.loads(before["variables"]), json.loads(after["variables"])
    assert (va["adArchiveID"], va["pageID"], va["isAdNonPolitical"], va["isAdNotAAAEligible"]) == ("4638101539846452", "669832573062940", False, False)
    assert {k: v for k, v in va.items() if k not in ("adArchiveID", "pageID", "isAdNonPolitical", "isAdNotAAAEligible")} == \
        {k: v for k, v in vb.items() if k not in ("adArchiveID", "pageID", "isAdNonPolitical", "isAdNotAAAEligible")}
    d = ap.details_fields(answer)
    assert (d["eu_reach"], d["uk_reach"], d["target_ages"], d["target_gender"]) == (109709, 6846, "18-65", "All")
    assert (d["payer"], d["beneficiary"], d["advertiser_ig_username"], d["advertiser_ig_followers"], d["advertiser_verification"]) == \
        ("Stadium Goods", "Stadium Goods", "stadiumgoods", 2539601, "BLUE_VERIFIED")
    rows = json.loads(d["reach_breakdown_json"])
    assert rows[0] == {"country": "AT", "age": "55-64", "male": 198, "female": 181, "unknown": 5} and {r["country"] for r in rows} >= {"AT", "GB"}
    assert json.loads(d["target_locations_json"]) == ["Worldwide"]
    assert ap.details_fields('{"data":{"viewer":null}}') is None and ap.details_request("a=1", ad_id="1", page_id="2", political=False, aaa_eligible=None) is None
    assert ap.fetch_url(_NIKE_URL) == _NIKE_URL + "&locale=en_US"


@check("Product field order: family-common fields first, Ad Library fields after; brand/price/currency/price_source always None on a real row")
def _():
    expected_head = [
        "sku", "source", "category", "title", "brand", "price", "currency",
        "price_source", "product_url", "image_url", "scraped_at",
    ]
    assert output_writer.PRODUCT_FIELD_NAMES[: len(expected_head)] == expected_head
    tail = output_writer.PRODUCT_FIELD_NAMES[len(expected_head):]
    for name in ("ad_id", "collation_id", "versions", "is_active", "advertiser_name", "advertiser_page_id", "advertiser_url",
                 "advertiser_likes", "advertiser_categories_json", "first_shown_at", "last_shown_at", "days_shown",
                 "platforms_json", "display_format", "body", "link_url", "link_title", "link_description", "link_caption",
                 "cta_text", "cta_type", "card_count", "image_urls_json", "video_urls_json", "ad_categories_json",
                 "paid_for_by", "spend", "spend_currency", "impressions", "reach", "region", "page", "position"):
        assert name in tail, f"{name} missing from Product's site-specific tail"
    for row in _rows("search_nike_us").values():
        assert (row.brand, row.price, row.currency, row.price_source) == (None, None, None, None)


@check("fixtures are scrubbed and stay scrubbed: no e-mail address, no disclosure phone/address but the placeholder, no session token assignment (fb_dtsg, lsd, jazoest, __hsi) — guarded by PATTERN, not by the old literals (CLAUDE.md §10)")
def _():
    email = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}")
    contact = re.compile(r'"(?:phone|email|address)"\s*:\s*"(?!REDACTED-not-verbatim")[^"]+"')
    token = re.compile(r'["\']?(?:fb_dtsg|lsd|jazoest|__hsi|DTSGInitialData)["\']?\s*[:=]\s*["{]')
    files = sorted(_FIX.glob("*"))
    assert len(files) == 10, [f.name for f in files]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for name, pattern in (("e-mail", email), ("contact", contact), ("token", token)):
            m = pattern.search(text)
            assert m is None, f"{path.name}: {name} {m.group(0)[:60]!r}"
    assert "REDACTED-not-verbatim" in _fx("political_us"), "the placeholder is what the political fixture says it carries"


# --------------------------------------------------------------------------- #
# CLI validation — bad usage never crashes, never writes output
# --------------------------------------------------------------------------- #
def _urls_file(lines):
    f = Path(tempfile.mkdtemp()) / "urls.txt"
    f.write_text("\n".join(lines), encoding="utf-8")
    return f


@check("each engine resolves --query/--page-id into ONE search URL; --url wins over --urls-file; a non-search line is skipped (never fetched) with its reason; duplicates collapse")
def _():
    for mod in _ENGINES:
        args = mod.build_arg_parser().parse_args(["--query", "nike", "--region", "us"])
        assert mod._resolve_urls(args) == ([_NIKE_URL], 0), mod.__name__
        args = mod.build_arg_parser().parse_args(["--urls-file", str(_urls_file([
            "# a comment", "https://www.facebook.com/ads/library/?q=nike&country=US", "https://www.facebook.com/ads/library/?id=1",
            "facebook.com/ads/library/?country=US&q=nike", "https://www.facebook.com/nike"]))])
        urls, skipped = mod._resolve_urls(args)
        assert len(urls) == 1 and skipped == 2, (mod.__name__, urls, skipped)


@check("each engine: no input, --query together with --url, a bad --region or --page-id, an input with nothing usable, and an --out directory that does not exist (measured: it used to crash AFTER the whole scrape) are all EXIT_BAD_USAGE before anything launches, and write nothing")
def _():
    for mod in _ENGINES:
        for argv in ([], ["--query", "x", "--url", "https://www.facebook.com/ads/library/?q=x"], ["--query", "x", "--region", "USA"],
                     ["--page-id", "nike"], ["--url", "https://www.facebook.com/ads/library/?id=123"],
                     ["--query", "x", "--out", "/nonexistent_dir_8812/o.json"]):
            with tempfile.TemporaryDirectory() as td:
                out = str(Path(td) / "out.json")
                args = mod.build_arg_parser().parse_args(["--out", out, *argv])  # a later --out in argv wins
                code = asyncio_run_maybe(mod, args)
                assert code == output_writer.EXIT_BAD_USAGE, (mod.__name__, argv, code)
                assert not Path(out).exists()


@check("each engine: a malformed --proxy is EXIT_BAD_USAGE, not a crash, and writes nothing")
def _():
    # Best-effort, not a requirement that a driver be installed: CLAUDE.md
    # §6 requires smoke_test.py to run cleanly with ZERO engine drivers
    # present. "This particular engine wasn't skipped" is asserted per
    # engine by tests.yml's own engine-smoke jobs.
    _IMPORT_ERROR_ATTR = {
        "playwright_scraper": "_PLAYWRIGHT_IMPORT_ERROR",
        "selenium_scraper": "_SELENIUM_IMPORT_ERROR",
        "puppeteer_scraper": "_PYPPETEER_IMPORT_ERROR",
    }
    for mod in _ENGINES:
        if getattr(mod, _IMPORT_ERROR_ATTR[mod.__name__], None) is not None:
            continue
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "out.json")
            args = mod.build_arg_parser().parse_args(["--query", "nike", "--proxy", "not a proxy!!", "--out", out])
            code = asyncio_run_maybe(mod, args)
            assert code == output_writer.EXIT_BAD_USAGE, f"{mod.__name__}: a malformed --proxy must exit 2, got {code}"
            assert not Path(out).exists(), f"{mod.__name__}: a bad-usage run must never write output"


@check("selenium_scraper refuses a credentialed --cdp-endpoint with EXIT_BAD_USAGE")
def _():
    args = selenium_scraper.build_arg_parser().parse_args([
        "--query", "nike", "--cdp-endpoint", "ws://user:pass@cb.2captcha.com:9222",
    ])
    assert selenium_scraper.run(args) == output_writer.EXIT_BAD_USAGE


@check("each engine rejects --max-results 0 at the argparse level")
def _():
    for mod in _ENGINES:
        try:
            mod.build_arg_parser().parse_args(["--query", "nike", "--max-results", "0"])
            raise AssertionError(f"{mod.__name__}: expected argparse to reject --max-results 0")
        except SystemExit:
            pass


# --------------------------------------------------------------------------- #
# diff_runs / scraper_api_client — sanity only (family-shared, no site knowledge)
# --------------------------------------------------------------------------- #
@check("diff_runs reports added/removed/changed between two real finish_run() outputs")
def _():
    with tempfile.TemporaryDirectory() as td:
        old_out, new_out = str(Path(td) / "old.json"), str(Path(td) / "new.json")
        output_writer.finish_run(
            products=[_mk_product("a", title="A v1"), _mk_product("b", title="B")],
            out_path=old_out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=1,
            failed_pages=None, blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        output_writer.finish_run(
            products=[_mk_product("a", title="A v1"), _mk_product("c", title="C")],
            out_path=new_out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=1,
            failed_pages=None, blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        result = diff_runs.diff(old_out, new_out)
        assert result["added"] == ["c"]
        assert result["removed"] == ["b"]


@check("diff_runs refuses to compare a non-'complete' run")
def _():
    with tempfile.TemporaryDirectory() as td:
        old_out, new_out = str(Path(td) / "old.json"), str(Path(td) / "new.json")
        output_writer.finish_run(
            products=[_mk_product("a")], out_path=old_out, fmt="json", engine="test", url="u",
            pages_requested=2, pages_completed=1, failed_pages=[2],
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        output_writer.finish_run(
            products=[_mk_product("a")], out_path=new_out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        try:
            diff_runs.diff(old_out, new_out)
            raise AssertionError("expected a refusal — old run is 'partial', not 'complete'")
        except SystemExit:
            pass


@check("scraper_api_client.TwoCaptchaClient._require_key rejects a missing/empty key")
def _():
    client = scraper_api_client.TwoCaptchaClient("")
    try:
        client._require_key()
        raise AssertionError("expected TwoCaptchaAuthError")
    except scraper_api_client.TwoCaptchaAuthError:
        pass


@check("scraper_api_client honors --captcha-api override, not the module-level API_BASE")
def _():
    client = scraper_api_client.TwoCaptchaClient("fakekey", api_base="https://mock.example.test")
    assert client.api_base == "https://mock.example.test"
    assert client.api_base != scraper_api_client.API_BASE


def _diff_run(td, name, rows, url, **kw):
    out = str(Path(td) / name)
    kw.setdefault("allow_empty", False)
    output_writer.finish_run(products=rows, out_path=out, fmt="json", engine="t", url=url, pages_requested=1,
                             pages_completed=1, failed_pages=None, blocked=False, remote_api_error=False,
                             started_at=0.0, **kw)
    return out


@check("diff_runs refuses different selections, never calls a currency switch a price change (even at the same number), reads a capped top-N's missing SKU as left_selection, and rejects a sidecar that does not describe its file (audit 2026-09-30)")
def _():
    dress, jeans = "https://us.shein.com/pdsearch/dress/", "https://us.shein.com/pdsearch/jeans/"
    with tempfile.TemporaryDirectory() as td:
        a = _diff_run(td, "a.json", [_mk_product("s1", 9.93, currency="USD")], dress)
        b = _diff_run(td, "b.json", [_mk_product("s1", 19.93, currency="EUR")], jeans)
        try:
            diff_runs.diff(a, b)
            raise AssertionError("different selections must be refused")
        except SystemExit as exc:
            assert "different selections" in str(exc)
        r = diff_runs.diff(a, b, allow_different_scope=True)
        assert not r["changed"] and len(r["currency_changed"]) == 1

        c = _diff_run(td, "c.json", [_mk_product("s1", 10.0, currency="USD")], dress)
        e = _diff_run(td, "e.json", [_mk_product("s1", 10.0, currency="EUR")], dress + "?")
        r = diff_runs.diff(c, e)
        assert r["currency_changed"] and not r["changed"], "same number, other currency must still be reported"

        f = _diff_run(td, "f.json", [_mk_product("s1"), _mk_product("s2")], dress, max_results=2)
        g = _diff_run(td, "g.json", [_mk_product("s1"), _mk_product("s3")], dress, max_results=2)
        r = diff_runs.diff(f, g)
        assert r["capped"] and r["left_selection"] == ["s2"] and r["removed"] == [] and r["added"] == ["s3"]
        h = _diff_run(td, "h.json", [_mk_product("s1"), _mk_product("s2")], dress, max_results=50)
        i = _diff_run(td, "i.json", [_mk_product("s1")], dress, max_results=50)
        r = diff_runs.diff(h, i)
        assert r["removed"] == ["s2"] and not r["capped"], "an uncapped run's missing SKU really is removed"

        Path(g).write_text("[]", encoding="utf-8")
        try:
            diff_runs.diff(f, g)
            raise AssertionError("a sidecar whose hash does not match must be refused")
        except SystemExit as exc:
            assert "output_sha256" in str(exc)

@check("the credential scanner FINDS a planted key in every shape seen in the family (JSON-quoted, JSON-escaped, 32-hex next to a key word) and ignores placeholders, type hints and Python-name mappings — a scanner that cannot fail is not one (CLAUDE.md §24/§25)")
def _():
    import importlib.util
    scanner = ROOT / ".github" / "ci_checks.py"
    if not (ROOT / ".github").is_dir():
        return
    spec = importlib.util.spec_from_file_location("ci_checks_planted", scanner)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fake32 = "0123456789abcdef" * 2
    for planted in ('"api_key": "a8f3k2m9q7x1z5b4"', '{\\"api_key\\": \\"a8f3k2m9q7x1z5b4\\"}',
                    "TWOCAPTCHA_KEY=" + fake32, '"clientKey":"' + fake32 + '"'):
        assert mod.scan_text("planted.txt", planted), f"scanner missed a planted credential: {planted!r}"
    for harmless in ("api_key: Optional[str] = None", "TWOCAPTCHA_KEY=your-key-here", '"TWOCAPTCHA_KEY": "twocaptcha_key",'):
        assert not mod.scan_text("ok.txt", harmless), f"false positive: {harmless!r}"

@check("no workflow imports a local module inline — tests.yml calls ci_checks.py instead (CLAUDE.md §26: an inline heredoc import is red only on the first push)")
def _():
    import re as _re
    if not (ROOT / ".github").is_dir():
        return  # the Docker image ships no .github/ (CLAUDE.md §22)
    local = {p.stem for p in ROOT.glob("*.py")}
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = wf.read_text(encoding="utf-8")
        for m in _re.finditer(r"^\s*(?:from\s+([A-Za-z_]\w*)\s+import|import\s+([A-Za-z_]\w*))", text, _re.M):
            name = m.group(1) or m.group(2)
            assert name not in local, f"{wf.name}: imports local module {name!r} inline"
    assert "ci_checks.py --sample-check" in (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

@check(".gitignore covers every artefact a run writes (CLAUDE.md §22/§26): .env copies, --dump-html challenge screenshots, *.pageN dumps, live/ — while sample outputs, fixtures and .env.example stay tracked")
def _():
    import subprocess as _sp
    if not (ROOT / ".git").exists():
        return
    must_ignore = [".env", ".env.bak", ".env.local", "facebook_ads_debug_1.html",
                   "out.json.page3", "live/x.html", "facebook_ads.json", "run.json"]
    must_keep = [".env.example", "sample_output.json", "sample_output.csv", "tests/fixtures/fb_ads_search_nike_us_20261005.html",
                 "tests/fixtures/fb_ads_scroll_nike_us_20261005.txt"]
    for path in must_ignore:
        assert _sp.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0, f"not ignored: {path}"
    for path in must_keep:
        assert _sp.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode != 0, f"wrongly ignored: {path}"


@check("sidecar records the selection, total_results, solves_spent, max_results/capped and the output hash; a capped run's missing ad is left_selection, an uncapped run's is removed; last_shown_at is not monitored by default (it moves daily by construction)")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "o.json")
        code = output_writer.finish_run(
            products=[_mk_product("1")], out_path=out, fmt="json", engine="t", url="u", pages_requested=1,
            pages_completed=1, failed_pages=None, blocked=False, remote_api_error=False, allow_empty=False,
            started_at=0.0, total_results=24402, max_results=1, extra_meta={"solves_spent": 0},
        )
        meta = json.loads(Path(out + ".meta.json").read_text())
        assert code == output_writer.EXIT_OK and meta["total_results"] == 24402 and meta["capped"] is True and len(meta["output_sha256"]) == 64
        a = _diff_run(td, "a.json", [_mk_product("1"), _mk_product("2")], _NIKE_URL, max_results=2)
        b = _diff_run(td, "b.json", [_mk_product("1", last_shown_at="2026-10-06"), _mk_product("3")], _NIKE_URL, max_results=2)
        r = diff_runs.diff(a, b)
        assert r["left_selection"] == ["2"] and r["removed"] == [] and r["field_changes"] == [], r
        c = _diff_run(td, "c.json", [_mk_product("1"), _mk_product("2")], _NIKE_URL, max_results=50)
        d = _diff_run(td, "d.json", [_mk_product("1", is_active=False, spend="$1K - $2K")], _NIKE_URL, max_results=50)
        r = diff_runs.diff(c, d)
        assert r["removed"] == ["2"] and r["field_changes"][0]["fields"].keys() == {"is_active", "spend"}, r
    assert "last_shown_at" not in diff_runs.MONITOR_FIELDS and "last_shown_at" in diff_runs.EXTRA_FIELDS


# --------------------------------------------------------------------------- #
# page_flow — the ONE fetch loop (CLAUDE.md §26), driven with a fake engine
# --------------------------------------------------------------------------- #
_SESSION = {"playwright_scraper": "_PlaywrightSession", "selenium_scraper": "_SeleniumSession", "puppeteer_scraper": "_PyppeteerSession"}
_ENGINE = {"playwright_scraper": "_PlaywrightEngine", "selenium_scraper": "_SeleniumEngine", "puppeteer_scraper": "_PyppeteerEngine"}


@check("the fetch loop exists ONCE: no engine parses or finishes a run itself, each calls page_flow.run and page_flow.resolve_urls; the CDP connect goes through connect_with_retry; pyppeteer disconnects instead of closing the remote browser")
def _():
    for mod in _ENGINES:
        src = (ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8")
        for fragment in ("finish_run(", "page_batches(", "response_batches(", "Collector(", "page_state("):
            assert fragment not in src, f"{mod.__name__}: loop logic {fragment!r} outside page_flow"
        assert src.count("page_flow.run(") == 1 and mod._resolve_urls is page_flow.resolve_urls, mod.__name__
        assert "ap.is_results_response(" in src, f"{mod.__name__}: GraphQL bodies must be filtered by the shared rule"
    for mod in (playwright_scraper, puppeteer_scraper):
        assert "scraper_api_client.connect_with_retry(" in (ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8")
    assert "reuse_default and browser.contexts" in (ROOT / "playwright_scraper.py").read_text(encoding="utf-8")
    pup = (ROOT / "puppeteer_scraper.py").read_text(encoding="utf-8")
    assert "_bounded(browser.disconnect()" in pup and "_release(remote_browser, remote=True)" in pup
    assert "get_event_loop().run_until_complete" not in pup


@check("§26 ops set, derived from page_flow's AST (every session.<op> / engine.<op> the loop uses): each engine's session and engine class provides all of them, and so does the Scraper API engine")
def _():
    import ast as _ast
    import scraper_api_engine
    tree = _ast.parse((ROOT / "page_flow.py").read_text(encoding="utf-8"))
    ops = {"session": set(), "engine": set()}
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute) and isinstance(node.value, _ast.Name) and node.value.id in ops:
            ops[node.value.id].add(node.attr)
    assert {"goto", "content", "current_url", "wait", "scroll_to_bottom", "page_height", "take_responses", "close"} <= ops["session"], ops
    assert {"open", "sleep", "solve_captcha", "readiness_s", "name"} <= ops["engine"], ops
    pairs = [(getattr(m, _SESSION[m.__name__]), getattr(m, _ENGINE[m.__name__]), m.__name__) for m in _ENGINES]
    pairs.append((scraper_api_engine._ScraperApiSession, scraper_api_engine.ScraperApiEngine, "scraper_api_engine"))
    detail_ops = {"click_text", "take_requests", "post_form"}
    assert detail_ops <= ops["session"], ops
    for sc, ec, name in pairs:
        need = ops["session"] if getattr(ec, "can_details", False) else ops["session"] - detail_ops
        assert not [o for o in need if not hasattr(sc, o)], (name, [o for o in need if not hasattr(sc, o)])
        assert not [o for o in ops["engine"] | {"can_scroll"} if not hasattr(ec, o)], name
    assert scraper_api_engine.ScraperApiEngine.can_details is False
    assert all(getattr(getattr(m, _ENGINE[m.__name__]), "can_details") is True for m in _ENGINES)
    assert scraper_api_engine.ScraperApiEngine.can_scroll is False
    assert all(getattr(getattr(m, _ENGINE[m.__name__]), "can_scroll") is True for m in _ENGINES)


def _rekey(body, n):
    """The REAL scroll batch, its ad ids re-keyed so batch n is new ads."""
    return re.sub(r'"ad_archive_id":"(\d+)"', lambda m: f'"ad_archive_id":"9{n:02d}{m.group(1)[-9:]}"', body)


def _canonical(url):
    """The address page_flow opened, minus the locale it adds (ap.fetch_url)."""
    assert url.endswith("locale=en_US"), f"opened without locale=en_US: {url}"
    return url[: -len("locale=en_US") - 1]


class _FakeSession:
    """Answers one search the way the real library does: `pages` is what
    content() returns over time (each wait() moves one step on, so a
    loading shell can paint), `batches` is one list of GraphQL bodies per
    take_responses() call — the first call is the one made right after the
    page painted, then one per scroll."""

    def __init__(self, engine, script):
        self.engine, self.script, self.closed = engine, dict(script), False
        self.pages = list(script.get("pages") or [script.get("html", "")])
        self.batches = list(script.get("batches") or [])
        # A drawn result list is a tall document; `render_after` keeps it one
        # blank viewport (720px) for that many waits, the way the live page
        # sometimes is long after its data arrived (page_flow.RENDER_WAIT_S).
        self.render_after = script.get("render_after", 0)
        self.step, self.height, self.scrolled, self.blind_scrolls = 0, 3000, 0, 0
        self.clicks, self.posted = [], []

    async def goto(self, url):
        self.engine.asked.append(url)
        step = self.script.get("goto")
        if isinstance(step, Exception):
            raise step
        return self.script.get("status", 403)  # the library's own status for a good page

    async def content(self):
        return self.pages[min(self.step, len(self.pages) - 1)]

    async def current_url(self):
        return self.script.get("final_url", self.engine.asked[-1])

    async def wait(self, seconds):
        self.step += 1

    def _drawn(self):
        return self.step >= self.render_after

    async def scroll_to_bottom(self):
        self.scrolled += 1
        if not self._drawn():
            self.blind_scrolls += 1

    async def page_height(self):
        return self.height if self._drawn() else 720

    async def take_responses(self):
        if self.scrolled and not self._drawn():
            return []  # a scroll on a blank page loads nothing
        bodies = self.batches.pop(0) if self.batches else []
        if bodies:
            self.height += 500
        return bodies

    async def click_text(self, text):
        self.clicks.append(text)
        if self.script.get("details_form") and self.step >= self.script.get("button_after", 0):
            self._clicked = True
            return True
        return False

    async def take_requests(self):
        if getattr(self, "_clicked", False) and not getattr(self, "_taken", False):
            self._taken = True
            return [(self.script["details_form"], self.script["details_answer"])]
        return []

    async def post_form(self, data):
        self.posted.append(data)
        answers = self.script.get("details_posts") or [(200, self.script.get("details_answer", ""))]
        return answers[min(len(self.posted) - 1, len(answers) - 1)]

    async def close(self):
        self.closed = True


class _FakeEngine:
    name = "fake"
    readiness_s = 0
    can_scroll = True
    can_details = True

    def __init__(self, by_url, solve=None):
        self.by_url, self.sessions, self.slept, self.asked, self._solve = by_url, [], [], [], solve
        self.fetched = []

    async def open(self, proxy):
        engine = self

        class _S(_FakeSession):
            async def goto(self, url):
                engine.fetched.append(url)
                url = _canonical(url)
                script = engine.by_url.get(url, engine.by_url.get("*", {}))
                _FakeSession.__init__(self, engine, script)
                return await _FakeSession.goto(self, url)
        sess = _S(self, {})
        self.sessions.append(sess)
        return sess

    async def sleep(self, seconds):
        self.slept.append(seconds)

    async def solve_captcha(self, session, *, html, url):
        return self._solve() if self._solve else None


def _pflow(by_url, argv, *, solve=None, proxy_pool=None):
    args = playwright_scraper.build_arg_parser().parse_args([*argv, "--delay-between-pages", "0"])
    args._solve_budget = page_flow.SolveBudget(args.max_solves)
    urls, _skipped = page_flow.resolve_urls(args)
    engine = _FakeEngine(by_url, solve)
    with tempfile.TemporaryDirectory() as td:
        args.out = str(Path(td) / "o.json")
        rc = asyncio.run(page_flow.run(engine, args, urls=urls, proxy_pool=proxy_pool, client=None, started_at=0.0))
        meta_p = Path(args.out + ".meta.json")
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else None
        rows = json.loads(Path(args.out).read_text()) if Path(args.out).exists() else None
    assert all(s.closed for s in engine.sessions), "every opened session must be closed"
    return rc, meta, rows, engine


@check("page_flow END TO END on the REAL captures: the embedded batch then one batch per scroll, in the site's order, until --max-results (exit 0, capped); HTTP 403 with results is content, never a block; the sidecar names the search and the library's own total")
def _():
    scroll = _fx("scroll_nike_us")
    nike = {"html": _fx("search_nike_us"), "batches": [[], [_rekey(scroll, 1)], [_rekey(scroll, 2)], [_rekey(scroll, 3)]]}
    rc, meta, rows, eng = _pflow({_NIKE_URL: nike}, ["--query", "nike", "--region", "US", "--max-results", "10"])
    assert rc == output_writer.EXIT_OK and len(rows) == 10, (rc, rows and len(rows))
    assert [r["ad_id"] for r in rows[:6]] == _NIKE_IDS and [r["page"] for r in rows] == [0] * 6 + [1] * 3 + [2]
    assert [r["position"] for r in rows] == list(range(1, 11)) and eng.sessions[0].scrolled == 2
    assert meta["status"] == "complete" and meta["capped"] is True and meta["total_results"] == 24402
    assert meta["selection"] == {"mode": "search", "urls": [_NIKE_URL], "max_results": 10}
    assert meta["searches"][0]["stop"] == "limit" and meta["searches"][0]["state"] == "content"


@check("page_flow at the END of the results: has_next_page false stops scrolling — complete, not capped, not stalled; a batch that arrived while the page painted is read before the first scroll")
def _():
    scroll = _fx("scroll_nike_us")
    last = _rekey(scroll, 1).replace('"has_next_page":true', '"has_next_page":false')
    assert '"has_next_page":false' in last
    rc, meta, rows, eng = _pflow({"*": {"html": _fx("search_nike_us"), "batches": [[_rekey(scroll, 7)], [last]]}}, ["--query", "nike"])
    assert rc == output_writer.EXIT_OK and len(rows) == 12 and eng.sessions[0].scrolled == 1, (rc, len(rows or []))
    assert meta["capped"] is False and meta["searches"][0]["stop"] == "end" and meta["searches"][0]["has_next_page"] is False
    rc, meta, rows, eng = _pflow({"*": {"html": _fx("single_id")}}, ["--page-id", "15087023444"])
    assert rc == output_writer.EXIT_OK and len(rows) == 1 and eng.sessions[0].scrolled == 0, "has_next_page false in the page itself"


@check("page_flow when the library STOPS sending with more announced: three scrolls in a row with no new ad and no growth (not one) — partial (6), stop_reason stalled, rows kept")
def _():
    rc, meta, rows, eng = _pflow({"*": {"html": _fx("search_nike_us")}}, ["--query", "nike", "--max-results", "50"])
    assert rc == output_writer.EXIT_PARTIAL and len(rows) == 6 and meta["stop_reason"] == "stalled", (rc, meta and meta["stop_reason"])
    assert eng.sessions[0].scrolled == page_flow.STALL_ROUNDS
    scroll = _fx("scroll_nike_us")
    rc, meta, rows, eng = _pflow({"*": {"html": _fx("search_nike_us"), "batches": [[], [], [], [_rekey(scroll, 4)], [], [], []]}},
                                 ["--query", "nike", "--max-results", "50"])
    assert len(rows) == 9 and eng.sessions[0].scrolled == 6, "a batch after two empty rounds resets the count"


@check("page_flow when the result list is drawn LATE (measured 2026-10-06: 4 of 8 political-search loads were a blank 720px page 7.5s after the data arrived, and a run that scrolled at once ended stalled at 30): scrolling waits for the drawn list, bounded, and reads on")
def _():
    scroll = _fx("scroll_nike_us")
    late = {"html": _fx("political_us"), "render_after": 12, "batches": [[], [_rekey(scroll, 1)], [_rekey(scroll, 2)]]}
    rc, meta, rows, eng = _pflow({"*": late}, ["--query", "election", "--max-results", "8"])
    assert rc == output_writer.EXIT_OK and len(rows) == 8 and eng.sessions[0].blind_scrolls == 0, (rc, len(rows or []), eng.sessions[0].blind_scrolls)
    never = {"html": _fx("political_us"), "render_after": 10 ** 6}
    rc, meta, rows, eng = _pflow({"*": never}, ["--query", "election", "--max-results", "8"])
    assert rc == output_writer.EXIT_PARTIAL and meta["stop_reason"] == "stalled" and len(rows) == 2, "bounded: a list that never draws is still reported"
    assert eng.sessions[0].step <= page_flow.PAINT_WAIT_S + page_flow.RENDER_WAIT_S + page_flow.STALL_ROUNDS + 2


@check("page_flow on the REAL no-results and unpainted pages: no results is empty (exit 4, nothing written, never retried); a shell that paints late is waited for (bounded) and read; one that never paints is not_painted — not empty, not blocked")
def _():
    rc, meta, rows, eng = _pflow({"*": {"html": _fx("noresults")}}, ["--query", "zzqx no such ad 8812"])
    assert rc == output_writer.EXIT_ZERO_PRODUCTS and meta is None and len(eng.asked) == 1
    late = {"pages": [_fx("loading_amazon")] * 5 + [_fx("search_nike_us")]}
    rc, meta, rows, eng = _pflow({"*": late}, ["--query", "amazon", "--max-results", "6"])
    assert rc == output_writer.EXIT_OK and len(rows) == 6
    rc, meta, rows, eng = _pflow({"*": {"html": _fx("loading_amazon")}}, ["--query", "amazon"])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and meta is None and eng.sessions[0].step >= page_flow.PAINT_WAIT_S, (rc, eng.sessions[0].step)
    with tempfile.TemporaryDirectory():
        rc, meta, rows, eng = _pflow({"*": {"html": _fx("loading_amazon")}}, ["--query", "amazon", "--allow-empty"])
        assert meta["stop_reason"] == "not_painted", meta


@check("page_flow on blocks: a login redirect and a non-library page under 403 are blocked (exit 3, nothing written); the same page under no status is fetch_error; without a pool the run STOPS after 3 blocks in a row and reports the rest as not_attempted; rows already read are kept as partial (6)")
def _():
    rc, meta, rows, eng = _pflow({"*": {"html": "<html><title>Log in to Facebook</title></html>", "final_url": "https://www.facebook.com/login/"}},
                                 ["--query", "nike"])
    assert rc == output_writer.EXIT_BLOCKED and meta is None
    rc, meta, rows, eng = _pflow({"*": {"html": _CHROMIUM_ERROR, "status": 403}}, ["--query", "nike"])
    assert rc == output_writer.EXIT_BLOCKED
    rc, meta, rows, eng = _pflow({"*": {"html": "<html><body></body></html>", "status": 407}}, ["--query", "nike", "--allow-empty"])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and meta["failed_urls"][0]["reason"] == "fetch_error", "a proxy's 407 is not the site blocking us"
    rc, meta, rows, eng = _pflow({"*": {"html": _CHROMIUM_ERROR, "status": None}}, ["--query", "nike", "--allow-empty"])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and meta["failed_urls"][0]["reason"] == "fetch_error", meta
    many = [ap.search_url(query=f"q{i}", region="US") for i in range(6)]
    rc, meta, rows, eng = _pflow({"*": {"html": _CHROMIUM_ERROR, "status": 429}}, ["--urls-file", str(_urls_file(many))])
    assert rc == output_writer.EXIT_BLOCKED and len(eng.asked) == page_flow.STOP_AFTER_CONSECUTIVE_BLOCKS, eng.asked
    rc, meta, rows, eng = _pflow({_NIKE_URL: {"html": _fx("search_nike_us"), "batches": [[]] + [[]] * 3}, "*": {"html": _CHROMIUM_ERROR, "status": 403}},
                                 ["--urls-file", str(_urls_file([_NIKE_URL] + many)), "--max-results", "6"])
    reasons = [f["reason"] for f in meta["failed_urls"]]
    assert rc == output_writer.EXIT_PARTIAL and len(rows) == 6 and meta["stop_reason"] == "blocked", (rc, meta["stop_reason"])
    assert reasons.count("not_attempted") == 6 - page_flow.STOP_AFTER_CONSECUTIVE_BLOCKS and reasons.count("blocked") == 3, reasons
    rc, meta, rows, eng = _pflow({"*": {"goto": RuntimeError("net::ERR_PROXY_CONNECTION_FAILED")}}, ["--query", "nike", "--retries", "1"])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and meta is None and len(eng.asked) == 2, (rc, eng.asked)


@check("page_flow across searches: an ad two searches both found is ONE row, the first search's; searches run in input order")
def _():
    de = ap.normalize_input("https://www.facebook.com/ads/library/?q=sneaker&country=DE")
    rc, meta, rows, eng = _pflow({_NIKE_URL: {"html": _fx("search_nike_us")}, de: {"html": _fx("search_nike_us")}},
                                 ["--urls-file", str(_urls_file([_NIKE_URL, de])), "--max-results", "6"])
    assert eng.asked == [_NIKE_URL, de] and len(rows) == 6 and {r["region"] for r in rows} == {"US"}, (eng.asked, len(rows))
    assert [s["rows"] for s in meta["searches"]] == [6, 6]


@check("page_flow and the paid-solve budget: a page carrying a generic challenge marker (not the library's own shell) gets at most --max-solves solves across the WHOLE run")
def _():
    challenge = '<html><div class="g-recaptcha" data-sitekey="x"></div></html>'
    solves = []
    urls = [ap.search_url(query=f"q{i}", region="US") for i in range(3)]
    rc, meta, rows, eng = _pflow({"*": {"html": challenge, "status": 403}},
                                 ["--urls-file", str(_urls_file(urls)), "--max-solves", "1"],
                                 solve=lambda: solves.append(1) or {"action": "warning_solver_error"})
    assert len(solves) == 1 and rc == output_writer.EXIT_BLOCKED, (len(solves), rc)
    assert not page_flow.is_challenge(challenge.replace("<html>", "<html>AdLibraryFoundationRoot")), "the library's own page is never a challenge"


class _FakeScrapeClient:
    """scraper_api_client.TwoCaptchaClient.scrape_url, scripted: each call
    takes the next answer — a (target status, body) pair or an exception."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def scrape_url(self, url, *, data_format, timeout, cdp_url):
        self.calls.append((_canonical(url), cdp_url))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return scraper_api_client.ScrapeResult(target_status=status, headers={}, body=body)


def _sapi(answers, argv, cdp="ws://u:p@cb.example:9222"):
    import scraper_api_engine
    args = playwright_scraper.build_arg_parser().parse_args([*argv, "--delay-between-pages", "0"])
    args._solve_budget = page_flow.SolveBudget(args.max_solves)
    urls, _skipped = page_flow.resolve_urls(args)
    client = _FakeScrapeClient(answers)
    engine = scraper_api_engine.ScraperApiEngine(client, cdp)
    slept = []

    async def _sleep(seconds):  # record, don't wait
        if engine.fatal is None:
            slept.append(seconds)
    engine.sleep = _sleep
    with tempfile.TemporaryDirectory() as td:
        args.out = str(Path(td) / "o.json")
        rc = asyncio.run(page_flow.run(engine, args, urls=urls, proxy_pool=None, client=None, started_at=0.0))
        meta_p = Path(args.out + ".meta.json")
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else None
        rows = json.loads(Path(args.out).read_text()) if Path(args.out).exists() else None
    return rc, meta, rows, client, slept


@check("--scraper-api END TO END on the REAL captures (a fake Scraper API client): one call per search, routed through the CDP profile when one is set and the default pool when not; the embedded batch only, marked capped (stop no_scroll) when more are announced; no results is exit 4; a refused key is exit 5 with no further calls; a transient error is retried")
def _():
    rc, meta, rows, client, _ = _sapi([(403, _fx("search_nike_us"))], ["--query", "nike", "--region", "US"])
    assert rc == output_writer.EXIT_OK and len(rows) == 6 and meta["engine"] == "scraper_api", (rc, meta)
    assert meta["capped"] is True and meta["searches"][0]["stop"] == "no_scroll" and client.calls == [(_NIKE_URL, "ws://u:p@cb.example:9222")]
    rc, meta, rows, client, _ = _sapi([(403, _fx("single_id"))], ["--query", "nike"], cdp=None)
    assert rc == output_writer.EXIT_OK and meta["capped"] is False and client.calls[0][1] is None, "everything there was is complete"
    rc, meta, rows, client, _ = _sapi([(403, _fx("noresults"))], ["--query", "zzqx"])
    assert rc == output_writer.EXIT_ZERO_PRODUCTS and len(client.calls) == 1, "no results is final, never retried"
    refused = scraper_api_client.TwoCaptchaAuthError("Scraper API: invalid/missing TWOCAPTCHA_KEY")
    urls = [ap.search_url(query=f"q{i}", region="US") for i in range(3)]
    rc, meta, rows, client, slept = _sapi([refused], ["--urls-file", str(_urls_file(urls))])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and len(client.calls) == 1 and slept == [], (rc, len(client.calls), slept)
    flaky = scraper_api_client.TwoCaptchaError("Scraper API returned HTTP 502: bad gateway")
    rc, meta, rows, client, _ = _sapi([flaky, (403, _fx("search_nike_us"))], ["--query", "nike"])
    assert rc == output_writer.EXIT_OK and len(client.calls) == 2, "a transient Scraper API error is retried"


@check("--scraper-api usage, all three engines, with NO engine driver needed: no key is exit 2 before any call; Selenium does not refuse a credentialed endpoint in this mode (2Captcha reads it, not chromedriver)")
def _():
    for mod in _ENGINES:
        base = ["--query", "nike", "--scraper-api", "--out", str(Path(tempfile.mkdtemp()) / "o.json")]
        a = mod.build_arg_parser().parse_args(base + ["--cdp-endpoint", "ws://u:p@cb.example:9222"])
        rc = mod.run(a) if mod is selenium_scraper else asyncio.run(mod.run(a))
        assert rc == output_writer.EXIT_BAD_USAGE, (mod.__name__, rc)
    src = (ROOT / "selenium_scraper.py").read_text(encoding="utf-8")
    assert src.index("if args.scraper_api:") < src.index("_cdp_endpoint_has_credentials(args.cdp_endpoint):\n")


def _details_script(**kw):
    return {"html": _fx("search_sneaker_de"), "details_form": (_FIX / "fb_ads_details_form_20261006.txt").read_text(encoding="utf-8").splitlines()[-1],
            "details_answer": (_FIX / "fb_ads_details_answer_20261006.txt").read_text(encoding="utf-8"), **kw}


@check("page_flow --details END TO END (measured 2026-10-06: one click on 'See ad details' sends the page's own details request; sent again with another ad's ids it answers for that ad, 30 of 30 at 1.5s apart): the clicked ad's own answer is used, every other ad gets one request with ITS ids, spaced, and the columns land on the rows; the click is retried until the list is drawn")
def _():
    from urllib.parse import parse_qsl
    rc, meta, rows, eng = _pflow({"*": _details_script(button_after=3)}, ["--query", "sneaker", "--region", "DE", "--max-results", "3", "--details"])
    s = eng.sessions[0]
    assert rc == output_writer.EXIT_OK and len(rows) == 3 and all(r["eu_reach"] == 109709 for r in rows), (rc, rows and [r["eu_reach"] for r in rows])
    assert len(s.clicks) >= 4 and len(s.posted) == 2, (len(s.clicks), len(s.posted))
    ids = [json.loads(dict(parse_qsl(d))["variables"])["adArchiveID"] for d in s.posted]
    assert ids == [r["ad_id"] for r in rows[1:]], "one request per OTHER ad, with its own id, in the site's order"
    assert meta["searches"][0]["details"] == "done" and meta["searches"][0]["details_read"] == 3 and meta["selection"]["details"] is True
    rc, meta, rows, eng = _pflow({"*": {"html": _fx("search_nike_us")}}, ["--query", "nike", "--max-results", "3"])
    assert eng.sessions[0].clicks == [] and rows[0]["eu_reach"] is None, "without --details nothing is clicked"


@check("page_flow --details endings never cost a row: 'Rate limit exceeded' stops the requests and keeps the rows (details rate_limited); no request after the click = no_request; an HTTP error = error; the Scraper API (no live page) = unsupported — the run itself stays complete")
def _():
    limited = _details_script(details_posts=[(200, '{"errors":[{"message":"Rate limit exceeded"}]}')])
    rc, meta, rows, eng = _pflow({"*": limited}, ["--query", "sneaker", "--region", "DE", "--max-results", "3", "--details"])
    assert rc == output_writer.EXIT_OK and len(rows) == 3 and meta["searches"][0]["details"] == "rate_limited"
    assert rows[0]["eu_reach"] == 109709 and rows[1]["eu_reach"] is None and len(eng.sessions[0].posted) == 1
    rc, meta, rows, eng = _pflow({"*": {"html": _fx("search_sneaker_de")}}, ["--query", "sneaker", "--max-results", "3", "--details"])
    assert rc == output_writer.EXIT_OK and meta["searches"][0]["details"] == "no_request" and len(rows) == 3
    rc, meta, rows, eng = _pflow({"*": _details_script(details_posts=[(500, "")])}, ["--query", "sneaker", "--max-results", "3", "--details"])
    assert rc == output_writer.EXIT_OK and meta["searches"][0]["details"] == "error" and meta["searches"][0]["details_read"] == 1
    rc, meta, rows, client, _ = _sapi([(403, _fx("search_sneaker_de"))], ["--query", "sneaker", "--details"])
    assert rc == output_writer.EXIT_OK and meta["searches"][0]["details"] == "unsupported"


@check("pyppeteer answers proxy auth over CDP Fetch, never page.authenticate() — measured 2026-10-04 on a sibling repo: current Chromium has no Network.setRequestInterception, so every proxied URL failed")
def _():
    src = (ROOT / "puppeteer_scraper.py").read_text(encoding="utf-8")
    assert "await page.authenticate(" not in src
    for op in ('"Fetch.enable"', '"Fetch.authRequired"', '"Fetch.continueWithAuth"', '"Fetch.requestPaused"', '"Fetch.continueRequest"'):
        assert op in src, op


@check("fingerprint_client.user_agent_from reads the LIVE response shape (userAgent.userAgent, measured 2026-10-04) and the documented one (userAgent.value); nothing applied when neither is there")
def _():
    import fingerprint_client as fc
    live = {"id": 6472645, "country": "US", "userAgent": {
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        "fullVersion": "152.0.7977.64", "platform": "Windows", "platformVersion": "10.0.0", "mobile": False}}
    assert fc.user_agent_from(live).startswith("Mozilla/5.0 (Windows NT 10.0"), "the live shape — reading only `value` made --fingerprint a no-op"
    assert fc.user_agent_from({"userAgent": {"value": "UA-doc"}}) == "UA-doc"
    for empty in ({}, {"userAgent": {}}, {"userAgent": {"userAgent": ""}}, None):
        assert fc.user_agent_from(empty) is None, empty
    for path in ("playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py"):
        assert 'log.info("Fingerprint applied: user agent %s", user_agent)' in (ROOT / path).read_text(encoding="utf-8"), path


@check("pyppeteer cleanup is BOUNDED: a page or browser whose close never returns does not hang the run (measured 2026-10-06: 20+ minutes stuck after a proxy dropped the connection); a local Chromium that will not close is killed")
def _():
    class _Never:
        killed = False

        def __init__(self):
            self.process = self

        async def close(self):
            await asyncio.sleep(3600)

        async def disconnect(self):
            await asyncio.sleep(3600)

        def kill(self):
            _Never.killed = True

    old = puppeteer_scraper.CLOSE_TIMEOUT_S
    puppeteer_scraper.CLOSE_TIMEOUT_S = 0.05
    try:
        import time as _time
        t0 = _time.monotonic()
        browser = _Never()
        asyncio.run(puppeteer_scraper._release(browser, remote=False))
        asyncio.run(puppeteer_scraper._release(browser, remote=True))
        session = puppeteer_scraper._PyppeteerSession.__new__(puppeteer_scraper._PyppeteerSession)
        session.page, session.browser, session.owns_browser = _Never(), _Never(), True
        asyncio.run(session.close())
        assert _time.monotonic() - t0 < 2 and _Never.killed
    finally:
        puppeteer_scraper.CLOSE_TIMEOUT_S = old


@check("a login page served under the address asked for (no redirect to go by — measured with the Scraper API's pool, canonical https://fi-fi.facebook.com/login) is a BLOCK (exit 3), never an unknown page; the canonical test fires on none of 66 real captures")
def _():
    login = ('<!DOCTYPE html><html id="facebook" lang="fi"><head><link rel="canonical" href="https://fi-fi.facebook.com/login" />'
             '<title>Facebook</title></head><body><form id="login_form"></form></body></html>')
    assert ap.page_state(login) == "login" and ap.is_login_page(login)
    for name in _PAGES:
        assert not ap.is_login_page(_fx(name)), name
    rc, meta, rows, eng = _pflow({"*": {"html": login, "status": 200}}, ["--query", "nike"])
    assert rc == output_writer.EXIT_BLOCKED and meta is None, rc


@check("an EMPTY Scraper API answer (no page, no target status — measured 2026-10-06) is the service failing: retried, and when it persists the run ends as remote_api_error (exit 5), never as an unreadable page")
def _():
    rc, meta, rows, client, _ = _sapi([(None, "")], _SAPI_ARGV + ["--retries", "1"])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and len(client.calls) == 2, (rc, len(client.calls))


def run() -> int:
    """All @check-decorated functions above already ran at import time
    (that's the point — see the `check()` docstring) and self-registered
    into RESULTS. This just reports them."""
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"smoke_test: {passed}/{len(RESULTS)} checks passed")
    for name, detail in failed:
        print(f"  FAIL: {name}\n        {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    import sys
    sys.exit(run())
