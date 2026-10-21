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
just that a column is populated.

Run directly: `python3 smoke_test.py`
"""
from __future__ import annotations

import asyncio
import inspect as _inspect
import json
import os
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
# Engine import/CLI hygiene
# --------------------------------------------------------------------------- #
@check("engines import cleanly regardless of installed drivers")
def _():
    for mod in _ENGINES:
        assert hasattr(mod, "build_arg_parser")
        assert hasattr(mod, "run")


@check("each engine imports its driver at MODULE level, guarded by try/except ImportError (an ast walk, not a substring: a driver imported inside a function makes the no-driver skip meaningless)")
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


@check("no module uses a name it never imports, defines or assigns (compileall proves a file PARSES, not that its names RESOLVE)")
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
    # Built from pieces so this file can be scanned too (the
    # check used to exempt its own file, where the phrases sat verbatim).
    anti = "anti" + "detect"
    banned = (
        "cloud" + " browser", anti + " browser", "2scraper " + anti + " browser",
        "gate." + "2prx.com", "--" + anti, anti + "_local_api",
    )
    exempt_names = {"CLAUDE" + ".md"}  # a local, untracked instruction file
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


@check("no shipped file cites a private instruction file or its section numbers (the public cannot open them; say the rule in this repo's own words)")
def _():
    import re as _re
    import subprocess as _sp
    # Assembled from pieces so this check does not match its own source.
    names = ("CLAUDE" + ".md", "claude_" + "2scraper", "2scraper_" + "repo_rules", "play" + "book")
    section = _re.compile("\u00a7" + r"\s?\d|\bSec\.\s?\d|template " + "\u00a7")
    try:
        out = _sp.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
        files = [ROOT / p.decode() for p in out.split(b"\0") if p]
    except (OSError, _sp.CalledProcessError):
        files = [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]
    scanned = 0
    for path in files:
        if "fixtures" in path.parts or not path.is_file():
            continue  # captured site pages, not our text
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        for name in names:
            assert name not in text, f"{path.relative_to(ROOT)}: cites {name!r}"
        m = section.search(text)
        assert not m, f"{path.relative_to(ROOT)}: cites a section number ({m.group(0)!r})"
    assert scanned > 10, f"only {scanned} files scanned — the guard would pass on nothing"


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


@check("all three engines expose the identical --flag set")
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


@check("every top-level module is in the Dockerfile COPY and pyproject py-modules (a module left out breaks the image on every run)")
def _():
    if not (ROOT / "Dockerfile").exists() and not (ROOT / "pyproject.toml").exists():
        return  # the Docker image's own copy of this suite ships neither
    modules = sorted(pth.stem for pth in ROOT.glob("*.py"))
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    listed = set(re.findall(r'"([a-z_]+)"', pyproject.split("py-modules", 1)[1].split("]", 1)[0]))
    for mod in modules:
        assert f"{mod}.py" in docker, f"{mod}.py missing from the Dockerfile COPY"
        assert mod in listed, f"{mod} missing from pyproject py-modules"
    assert listed <= set(modules), f"pyproject lists modules that do not exist: {sorted(listed - set(modules))}"


@check("BOT_CHALLENGE_MARKERS is empty, and the generic markers match NONE of the real captures (count a marker on a good page first)")
def _():
    assert ap.BOT_CHALLENGE_MARKERS == (), "no captcha vendor appeared on any Ad Library capture"
    for name in _PAGES:
        html = _fx(name)
        assert not captcha_solver.detect_from_html(html, ap.BOT_CHALLENGE_MARKERS), f"{name}: a generic marker fires on a real Ad Library page"
        assert not page_flow.is_challenge(html), name


# --------------------------------------------------------------------------- #
# output_writer — exit codes / precedence / dedupe
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


@check("finish_run: rows gathered by a run that did not finish are PARTIAL (6) with the cause in stop_reason — never 5/3 with a file (audit 2026-09-30 got exit 5 AND a written file). Rewrites the old pinned 'exit 5 with products' position deliberately.")
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
    raw = "connect failed: ws://" + "myuser:mysecret@cb.2captcha.com:9222 (5 attempts)"
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


@check("ad_row on the REAL nike/US search: values, not coverage — a VIDEO ad, a DPA, an IMAGE ad, a CAROUSEL, a DCO whose top-level text is a template, an IMAGE ad with no text")
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


@check("fixtures are scrubbed and stay scrubbed: no e-mail address, no disclosure phone/address but the placeholder, no session token assignment (fb_dtsg, lsd, jazoest, __hsi) — guarded by PATTERN, not by the old literals")
def _():
    email = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}")
    contact = re.compile(r'"(?:phone|email|address)"\s*:\s*"(?!REDACTED-not-verbatim")[^"]+"')
    token = re.compile(r'["\']?(?:fb_dtsg|lsd|jazoest|__hsi|DTSGInitialData)["\']?\s*[:=]\s*["{]')
    files = sorted(_FIX.glob("*"))
    assert len(files) == 11, [f.name for f in files]
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
    # Best-effort, not a requirement that a driver be installed: smoke_test.py
    # must run cleanly with ZERO engine drivers
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


@check("diff_runs refuses two different searches, reports a watched-field change on the same ad, reads a capped top-N's missing ad as left_selection, and rejects a sidecar that does not describe its file")
def _():
    dress = "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=US&q=nike"
    jeans = "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=US&q=adidas"
    with tempfile.TemporaryDirectory() as td:
        a = _diff_run(td, "a.json", [_mk_product("s1", is_active=True)], dress)
        b = _diff_run(td, "b.json", [_mk_product("s1", is_active=False)], jeans)
        try:
            diff_runs.diff(a, b)
            raise AssertionError("different searches must be refused")
        except SystemExit as exc:
            assert "different selections" in str(exc)
        r = diff_runs.diff(a, b, allow_different_scope=True)
        assert r["field_changes"] == [{"sku": "s1", "fields": {"is_active": {"old": True, "new": False}}}], r
        assert "changed" not in r and "currency_changed" not in r, "an ad has no price to compare"

        c = _diff_run(td, "c.json", [_mk_product("s1", spend="$100 - $199")], dress)
        e = _diff_run(td, "e.json", [_mk_product("s1", spend="$200 - $299")], dress + "&")
        r = diff_runs.diff(c, e)
        assert r["field_changes"][0]["fields"]["spend"] == {"old": "$100 - $199", "new": "$200 - $299"}, r

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

@check("the credential scanner FINDS a planted key in every shape seen in the family (JSON-quoted, JSON-escaped, 32-hex next to a key word) and ignores placeholders, type hints and Python-name mappings — a scanner that cannot fail is not one")
def _():
    import importlib.util
    scanner = ROOT / ".github" / "ci_checks.py"
    if not (ROOT / ".github").is_dir():
        return
    spec = importlib.util.spec_from_file_location("ci_checks_planted", scanner)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fake32 = "0123456789abcdef" * 2
    for planted in ('"api_' + 'key": "a8f3k2m9q7x1z5b4"', '{\\"api_' + 'key\\": \\"a8f3k2m9q7x1z5b4\\"}',
                    "TWOCAPTCHA_KEY=" + fake32, '"clientKey":"' + fake32 + '"'):
        assert mod.scan_text("planted.txt", planted), f"scanner missed a planted credential: {planted!r}"
    for harmless in ("api_key: Optional[str] = None", "TWOCAPTCHA_KEY=your-key-here", '"TWOCAPTCHA_KEY": "twocaptcha_key",'):
        assert not mod.scan_text("ok.txt", harmless), f"false positive: {harmless!r}"

@check("no workflow imports a local module inline — tests.yml calls ci_checks.py instead (an inline heredoc import is red only on the first push)")
def _():
    import re as _re
    if not (ROOT / ".github").is_dir():
        return  # the Docker image ships no .github/
    local = {p.stem for p in ROOT.glob("*.py")}
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = wf.read_text(encoding="utf-8")
        for m in _re.finditer(r"^\s*(?:from\s+([A-Za-z_]\w*)\s+import|import\s+([A-Za-z_]\w*))", text, _re.M):
            name = m.group(1) or m.group(2)
            assert name not in local, f"{wf.name}: imports local module {name!r} inline"
    assert "ci_checks.py --sample-check" in (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

@check(".gitignore covers every artefact a run writes: .env copies, --dump-html challenge screenshots, *.pageN dumps, live/ — while sample outputs, fixtures and .env.example stay tracked")
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
# page_flow — the ONE fetch loop, driven with a fake engine
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


@check("shared-loop ops set, derived from page_flow's AST (every session.<op> / engine.<op> the loop uses): each engine's session and engine class provides all of them, and so does the Scraper API engine")
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


# Assembled from pieces so the credential scan, which also reads this file,
# does not take the fixture for a real credentialled URL.
_CDP_FIXTURE = "ws://" + "u:p@cb.example:9222"


def _sapi(answers, argv, cdp=_CDP_FIXTURE):
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
    assert meta["capped"] is True and meta["searches"][0]["stop"] == "no_scroll" and client.calls == [(_NIKE_URL, _CDP_FIXTURE)]
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
        a = mod.build_arg_parser().parse_args(base + ["--cdp-endpoint", _CDP_FIXTURE])
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


@check("every engine rejects a negative --retries/--retry-delay/--delay-between-pages/--max-solves/--proxy-block-retries with an argparse error naming the flag (exit 2), and accepts 0 (it counts EXTRA attempts)")
def _():
    import contextlib as _cl
    import io as _io
    for mod in _ENGINES:
        p = mod.build_arg_parser()
        for flag in ("--retries", "--retry-delay", "--delay-between-pages", "--max-solves", "--proxy-block-retries"):
            err = _io.StringIO()
            try:
                with _cl.redirect_stderr(err):
                    p.parse_args(["--query", "x", flag, "-1"])
                raise AssertionError(f"{mod.__name__} accepted {flag} -1")
            except SystemExit as exc:
                assert exc.code == 2, (mod.__name__, flag, exc.code)
            assert "error:" in err.getvalue() and flag in err.getvalue().split("error:", 1)[1], (mod.__name__, flag, err.getvalue())
            assert getattr(p.parse_args(["--query", "x", flag, "0"]), flag[2:].replace("-", "_")) == 0


@check("a served page that STATES its results but whose ads cannot be read is parse_error (exit 5, the stated total kept), never not_painted; the loading shell is still loading")
def _():
    drifted = _fx("search_nike_us").replace('"collated_results"', '"collated_resultz"')
    assert drifted != _fx("search_nike_us")
    assert ap.page_state(drifted) == "parse_error", ap.page_state(drifted)
    assert ap.page_state(_fx("loading_amazon")) == "loading"
    rc, meta, rows, eng = _pflow({"*": {"html": drifted}}, ["--query", "nike"])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and meta is None, rc
    rc, meta, rows, eng = _pflow({"*": {"html": drifted}}, ["--query", "nike", "--allow-empty"])
    assert meta["stop_reason"] == "parse_error" and meta["total_results"] == 24402, meta


@check("FACEBOOK_URL from the environment or .env does not block a typed --query/--page-id; a TYPED --url with --query is still refused")
def _():
    import argparse as _argparse
    a = _argparse.Namespace(query="nike", page_id=None, url="https://www.facebook.com/ads/library/?q=adidas&country=DE",
                            urls_file=None, region="ALL", active_status="active", ad_type="all", exact_phrase=False,
                            _url_source="environment")
    urls, _ = page_flow.resolve_urls(a)
    assert len(urls) == 1 and "q=nike" in urls[0], urls
    os.environ["FACEBOOK_URL"] = "https://www.facebook.com/ads/library/?q=adidas&country=DE"
    try:
        args = env_config.apply_env(playwright_scraper.build_arg_parser().parse_args(["--query", "nike"]), dotenv_path="/nonexistent/.env")
        urls, _ = page_flow.resolve_urls(args)
        assert "q=nike" in urls[0], urls
    finally:
        del os.environ["FACEBOOK_URL"]
    typed = playwright_scraper.build_arg_parser().parse_args(["--query", "nike", "--url", "https://www.facebook.com/ads/library/?q=x"])
    try:
        page_flow.resolve_urls(typed)
        raise AssertionError("a typed --url with --query must be refused")
    except ValueError:
        pass


@check("an atomic rewrite keeps an existing output's mode (0640 stays 0640, 0664 stays 0664); a new file gets 0666 minus the umask")
def _():
    import stat as _stat
    with tempfile.TemporaryDirectory() as td:
        for mode in (0o640, 0o664):
            out = Path(td) / f"o{mode:o}.json"
            out.write_text("[]")
            os.chmod(out, mode)
            output_writer.write_json([_mk_product("a")], str(out))
            assert _stat.S_IMODE(out.stat().st_mode) == mode, oct(out.stat().st_mode)
        for umask in (0o022, 0o002, 0o077):
            old = os.umask(umask)
            try:
                out = Path(td) / f"new{umask:o}.json"
                output_writer.write_json([_mk_product("a")], str(out))
            finally:
                os.umask(old)
            assert _stat.S_IMODE(out.stat().st_mode) == 0o666 & ~umask, (oct(umask), oct(out.stat().st_mode))


@check("a CSV run records csv_cells_escaped (strings only, numbers untouched), and a caller's own sidecar key is never overwritten by housekeeping")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "o.csv")
        rows = [_mk_product("a", title="=HYPERLINK(1)", body="-5", advertiser_likes=-3), _mk_product("b", title="plain")]
        output_writer.finish_run(products=rows, out_path=out, fmt="csv", engine="t", url="u", pages_requested=1,
                                 pages_completed=1, failed_pages=None, blocked=False, remote_api_error=False,
                                 allow_empty=False, started_at=0.0, extra_meta={"csv_text_encoding": "caller"})
        meta = json.loads(Path(out + ".meta.json").read_text())
        assert meta["csv_cells_escaped"] == 2, meta
        assert meta["csv_text_encoding"] == "caller", meta
        text = Path(out).read_text()
        assert "'=HYPERLINK(1)" in text and ",-3," in text


@check("the credential scanner reads smoke_test.py (no blanket exemption) and fails when it scanned no file at all")
def _():
    import importlib.util
    if not (ROOT / ".github").is_dir():
        return
    spec = importlib.util.spec_from_file_location("ci_checks_exempt", ROOT / ".github" / "ci_checks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "smoke_test.py" not in mod.FIXTURE_ALLOWLIST
    real = mod.repository_files
    try:
        mod.repository_files = lambda: []
        assert any(rule == "no_files_scanned" for _, _, rule in mod.scan()), "an empty scan must not pass"
    finally:
        mod.repository_files = real


@check("searches never fetched because the run stopped are unattempted_pages, not failed_pages")
def _():
    many = [ap.search_url(query=f"q{i}", region="US") for i in range(5)]
    rc, meta, rows, eng = _pflow({"*": {"html": _CHROMIUM_ERROR, "status": 429}},
                                 ["--urls-file", str(_urls_file(many)), "--allow-empty"])
    assert rc == output_writer.EXIT_BLOCKED, rc
    assert meta["failed_pages"] == [1, 2, 3] and meta["unattempted_pages"] == [4, 5], meta
    assert meta["stop_reason"] != "not_attempted", meta


def _canary_check_snippets():
    """Every `python3 -c "..."` block in the canary workflow that reads canary_exit."""
    import textwrap
    lines = (ROOT / ".github" / "workflows" / "canary.yml").read_text(encoding="utf-8").splitlines()
    snippets, i = [], 0
    while i < len(lines):
        if lines[i].strip() == 'python3 -c "':
            body = []
            i += 1
            while lines[i].strip() != '"':
                body.append(lines[i])
                i += 1
            text = textwrap.dedent("\n".join(body))
            if "canary_exit" in text:
                # bash would expand these inside the double quotes; this check runs the text as-is
                assert not any(ch in text for ch in "$`\\"), "canary snippet uses a bash-special character"
                snippets.append(text)
        i += 1
    return snippets


@check("every way the canary check step fails prints an ::error:: line: a nonzero exit with or without the proxy secret, and a missing or wrong output file (a red job with no annotation hid a proxy failure on 2026-10-20)")
def _():
    import subprocess
    import sys
    if not (ROOT / ".github").is_dir():
        return  # the Docker image ships no workflows; the repository's own CI runs this check
    snippets = _canary_check_snippets()
    assert snippets, "no canary check step found in .github/workflows/canary.yml"
    with tempfile.TemporaryDirectory() as tmp:
        for snippet in snippets:
            for has_proxy in ("true", "false"):
                for code in ("0", "1", "3", "4", "5", "6"):
                    env = dict(os.environ, HAS_PROXY=has_proxy, canary_exit=code,
                               canary_details_exit=code, CANARY_REGION="US")
                    proc = subprocess.run([sys.executable, "-c", snippet], cwd=tmp, env=env,
                                          capture_output=True, text=True, timeout=60)
                    # code 0 in an empty directory: the sidecar is missing, which must fail too
                    assert proc.returncode != 0, (has_proxy, code, "an empty directory passed")
                    assert "::error::" in proc.stdout, (has_proxy, code, proc.stdout[-300:], proc.stderr[-300:])


# --------------------------------------------------------------------------- #
# Proxy traffic savers: the pre-check and media blocking
# --------------------------------------------------------------------------- #
def _status_proxy(status):
    """A local stand-in for a proxy gateway that answers every request,
    CONNECT included, with `status`. Returns (server, hits)."""
    import http.server
    import socketserver
    import threading
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _answer(self):
            hits.append((self.command, self.path, self.headers.get("Proxy-Authorization") is not None))
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Proxy-Authenticate", 'Basic realm="stub"')
            self.end_headers()
        do_CONNECT = do_GET = do_HEAD = _answer

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, hits


class _NoBrowser:
    """Stands in for every engine's browser entry point: the pre-check must
    stop the run before any of them is reached."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        raise AssertionError("a browser was started")

    def __getattr__(self, name):  # selenium's `webdriver.Chrome`
        return self


class _SeleniumOptions:
    """selenium's Options, for a venv without selenium."""

    def __init__(self):
        self.debugger_address = None

    def add_argument(self, *a):
        pass

    def set_capability(self, *a):
        pass

    def add_experimental_option(self, *a):
        pass


def _engine_with_no_browser(mod):
    import contextlib
    from unittest import mock
    fake = _NoBrowser()
    attr = {"playwright_scraper": "async_playwright", "selenium_scraper": "webdriver",
            "puppeteer_scraper": "pyppeteer_launch"}[mod.__name__]
    patches = contextlib.ExitStack()
    patches.enter_context(mock.patch.object(mod, attr, fake))
    if mod is selenium_scraper:
        patches.enter_context(mock.patch.object(mod, "Options", _SeleniumOptions))
        patches.enter_context(mock.patch.object(mod, "Service", None))
    return fake, patches


def _run_through_stub_proxy(mod, status, argv=()):
    import contextlib
    import io
    server, hits = _status_proxy(status)
    port = server.server_address[1]
    fake, patch = _engine_with_no_browser(mod)
    err = io.StringIO()
    try:
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "run.json")
            Path(out).write_text("[]")  # a previous good run's data stays as it was
            args = mod.build_arg_parser().parse_args(
                ["--query", "nike", "--region", "US", "--out", out,
                 "--proxy", f"http://user:S3cretPw@127.0.0.1:{port}", *argv])
            with patch, contextlib.redirect_stderr(err):
                code = asyncio_run_maybe(mod, args)
            attempt_p = Path(out + output_writer.ATTEMPT_META_SUFFIX)
            attempt = json.loads(attempt_p.read_text()) if attempt_p.exists() else {}
            files = sorted(p.name for p in Path(td).iterdir())
            data = Path(out).read_text()
    finally:
        server.shutdown()
        server.server_close()
    return code, fake.calls, hits, err.getvalue(), attempt, files, data


@check("proxy pre-check: a gateway 407 or 402 ends EVERY browser engine at exit 5 before a browser starts — after ONE CONNECT for facebook.com/robots.txt; the message names FACEBOOK_PROXY and never the password; <out>.latest_attempt.meta.json says failed/proxy_auth_failed; the data is untouched and no <out>.meta.json is written")
def _():
    for mod in _ENGINES:
        for status, words in ((407, "refused the credential"), (402, "balance exhausted")):
            code, started, hits, text, attempt, files, data = _run_through_stub_proxy(mod, status)
            where = (mod.__name__, status)
            assert code == output_writer.EXIT_REMOTE_API_ERROR, (where, code, text[-300:])
            assert started == 0, (where, "browser started")
            assert [h[:2] for h in hits] == [("CONNECT", "www.facebook.com:443")], (where, hits)
            assert words in text and "FACEBOOK_PROXY" in text and "No browser was started" in text, (where, text)
            assert "S3cretPw" not in text and "S3cretPw" not in json.dumps(attempt), where
            assert (attempt.get("status"), attempt.get("stop_reason"), attempt.get("data_updated")) == \
                ("failed", "proxy_auth_failed", False), (where, attempt)
            assert files == ["run.json", "run.json" + output_writer.ATTEMPT_META_SUFFIX] and data == "[]", (where, files)


@check("proxy pre-check: Selenium checks the proxy the way Chrome will use it there — WITHOUT the password it cannot send — and a 407 then says to whitelist the IP; Playwright and pyppeteer send the credential")
def _():
    sent = {}
    for mod in _ENGINES:
        code, _started, hits, text, _a, _f, _d = _run_through_stub_proxy(mod, 407)
        sent[mod.__name__] = hits[0][2]
        if mod is selenium_scraper:
            assert "IP whitelist" in text, text
    assert sent == {"playwright_scraper": True, "selenium_scraper": False, "puppeteer_scraper": True}, sent


@check("proxy pre-check: a 403 through the tunnel, an unreachable proxy or no proxy at all is not a verdict on the proxy — the run goes on to the browser exactly as before")
def _():
    for mod in _ENGINES:
        code, started, hits, _t, attempt, _f, _d = _run_through_stub_proxy(mod, 403)
        # No pre-check verdict. Since --proxy-budget-mb, a proxied run that wrote no data records its own
        # outcome and traffic in the attempt sidecar — never as the pre-check's proxy_auth_failed.
        assert started == 1 and len(hits) == 1, (mod.__name__, started, hits, attempt)
        assert attempt == {} or (attempt.get("stop_reason") != "proxy_auth_failed" and "proxy_bytes" in attempt), \
            (mod.__name__, attempt)
    assert proxy_pool.precheck_proxy("http://user:pass@127.0.0.1:1", "https://x.invalid/robots.txt", 3) is None
    assert proxy_pool.precheck_proxy(None, "https://www.facebook.com/robots.txt") is None
    assert proxy_pool.robots_url(_NIKE_URL) == "https://www.facebook.com/robots.txt"
    pool = proxy_pool.ProxyPool([proxy_pool.parse_proxy_line("h:1:u:p"), proxy_pool.parse_proxy_line("g:2")])
    assert pool.peek() is pool.next() and pool.peek().host == "g", "peek is the proxy the first search will use"
    assert proxy_pool.parse_proxy_line("h:1:user:pass").url() == "http://user:pass@h:1"
    assert proxy_pool.parse_proxy_line("h:1:user:pass").url(with_auth=False) == "http://h:1"


def scraper_api_engine_mod():
    import scraper_api_engine
    return scraper_api_engine


async def _zero():
    return 0


@check("proxy pre-check: never sent over --cdp-endpoint or --scraper-api (no local proxy there), with a --proxy given too")
def _():
    from unittest import mock
    seen = []
    with mock.patch.object(proxy_pool, "precheck_proxy", lambda *a, **k: seen.append(a)), \
            mock.patch.object(page_flow, "run", side_effect=AssertionError("stop here")), \
            mock.patch.object(scraper_api_engine_mod(), "run", lambda *a, **k: _zero()):
        for mod in (playwright_scraper, puppeteer_scraper):
            fake, patch = _engine_with_no_browser(mod)
            with tempfile.TemporaryDirectory() as td, patch:
                for extra in (["--cdp-endpoint", "ws://127.0.0.1:1"], ["--scraper-api"]):
                    args = mod.build_arg_parser().parse_args(["--query", "nike", "--out", str(Path(td) / "o.json"),
                                                              "--proxy", "http://user:pass@127.0.0.1:9", *extra])
                    asyncio_run_maybe(mod, args)
        with tempfile.TemporaryDirectory() as td:
            args = selenium_scraper.build_arg_parser().parse_args(["--query", "nike", "--out", str(Path(td) / "o.json"),
                                                                   "--proxy", "http://user:pass@127.0.0.1:9", "--scraper-api"])
            selenium_scraper.run(args)
    assert seen == [], seen


@check("media: blocked only for a LOCAL browser behind a proxy; --load-media keeps it; never over CDP or with no proxy; the hidden --force-block-media turns it on for a free measurement")
def _():
    import argparse as _argparse
    ns = _argparse.Namespace
    sbm = proxy_pool.should_block_media
    assert sbm(ns(load_media=False), proxy_in_use=True) is True
    assert sbm(ns(load_media=True), proxy_in_use=True) is False
    assert sbm(ns(load_media=False), proxy_in_use=False) is False
    assert sbm(ns(load_media=False), proxy_in_use=True, over_cdp=True) is False
    assert sbm(ns(load_media=False, force_block_media=True), proxy_in_use=False) is True
    for mod in _ENGINES:
        parser = mod.build_arg_parser()
        assert parser.parse_args(["--query", "x"]).load_media is False
        assert "--load-media" in parser.format_help() and "--force-block-media" not in parser.format_help(), mod.__name__


class _Route:
    def __init__(self, kind):
        import argparse as _argparse
        self.request = _argparse.Namespace(resource_type=kind)
        self.done = None

    async def abort(self, reason=None):
        self.done = ("abort", reason)

    async def continue_(self):
        self.done = ("continue",)


@check("media (Playwright): images, media and fonts are aborted, documents, scripts, XHR/fetch and stylesheets go on; the route is installed on a proxied local context only, and the run's sidecar records media_blocked")
def _():
    import argparse as _argparse
    for kind, want in (("image", "abort"), ("media", "abort"), ("font", "abort"), ("document", "continue"),
                       ("script", "continue"), ("xhr", "continue"), ("fetch", "continue"), ("stylesheet", "continue")):
        route = _Route(kind)
        asyncio.run(playwright_scraper._media_route(route))
        assert route.done[0] == want, (kind, route.done)

    class Page:
        def on(self, *a):
            pass

    class Context:
        def __init__(self):
            self.routes = []

        async def route(self, pattern, handler):
            self.routes.append((pattern, handler))

        async def new_page(self):
            return Page()

    class Browser:
        def __init__(self):
            self.contexts = [Context()]
            self.made = []

        async def new_context(self, **kw):
            self.made.append(Context())
            return self.made[-1]

    proxy = proxy_pool.parse_proxy_line("h:1:u:p")
    for argv, use_proxy, cdp, want in (([], True, False, True), (["--load-media"], True, False, False),
                                       ([], False, False, False), ([], True, True, False),
                                       (["--force-block-media"], False, False, True)):
        args = playwright_scraper.build_arg_parser().parse_args(["--query", "x", *argv])
        args.cdp_endpoint = "ws://h:1" if cdp else None
        browser = Browser()
        engine = playwright_scraper._PlaywrightEngine(browser, args, autosolve=False, user_agent=None, client=None)
        session = asyncio.run(engine.open(proxy if use_proxy else None))
        routes = session.context.routes
        assert bool(routes) is want and engine.media_blocked is want, (argv, use_proxy, cdp, routes)
        if want:
            assert routes[0] == ("**/*", playwright_scraper._media_route)
    args = playwright_scraper.build_arg_parser().parse_args(["--query", "nike", "--region", "US", "--max-results", "6"])
    args._solve_budget = page_flow.SolveBudget(0)
    engine = _FakeEngine({"*": {"html": _fx("search_nike_us"), "batches": []}}, None)
    engine.media_blocked = True
    with tempfile.TemporaryDirectory() as td:
        args.out = str(Path(td) / "o.json")
        args.delay_between_pages = 0
        asyncio.run(page_flow.run(engine, args, urls=[_NIKE_URL], proxy_pool=None, client=None, started_at=0.0))
        assert json.loads(Path(args.out + ".meta.json").read_text())["media_blocked"] is True


class _CDPClient:
    def __init__(self):
        self.sent, self.handlers = [], {}

    def on(self, name, fn):
        self.handlers[name] = fn

    async def send(self, cmd, params=None):
        self.sent.append((cmd, params))


def _pyppeteer_page():
    import argparse as _argparse
    sessions = []

    async def create():
        sessions.append(_CDPClient())
        return sessions[-1]
    page = _argparse.Namespace(_client=_CDPClient(), target=_argparse.Namespace(createCDPSession=create))
    return page, sessions


@check("media (pyppeteer): an authenticated proxy's own Fetch interception also fails images/media/fonts (no second Fetch domain on the page); a proxy without a password gets proxy_pool.pyppeteer_block_media; nothing without blocking")
def _():
    async def paused(page, *events):
        for event in events:
            page._client.handlers["Fetch.requestPaused"](event)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    auth_proxy = proxy_pool.parse_proxy_line("h:1:u:p")
    page, sessions = _pyppeteer_page()
    took = asyncio.run(puppeteer_scraper._authenticate_if_needed(page, auth_proxy, block_media=True))
    asyncio.run(paused(page, {"requestId": "i", "resourceType": "Image"}, {"requestId": "d", "resourceType": "Document"},
                       {"requestId": "f", "resourceType": "Font"}))
    sent = page._client.sent
    assert took is True and sessions == []
    assert [c for c, _ in sent].count("Fetch.enable") == 1
    assert ("Fetch.failRequest", {"requestId": "i", "errorReason": "BlockedByClient"}) in sent
    assert ("Fetch.failRequest", {"requestId": "f", "errorReason": "BlockedByClient"}) in sent
    assert ("Fetch.continueRequest", {"requestId": "d"}) in sent

    page, _ = _pyppeteer_page()
    asyncio.run(puppeteer_scraper._authenticate_if_needed(page, auth_proxy, block_media=False))
    asyncio.run(paused(page, {"requestId": "i", "resourceType": "Image"}))
    assert ("Fetch.continueRequest", {"requestId": "i"}) in page._client.sent, "no blocking unless asked"

    class Browser:
        def __init__(self, page):
            self.page = page

        async def newPage(self):
            return self.page
    for proxy, block, want_session in ((proxy_pool.parse_proxy_line("h:1"), True, True),
                                       (auth_proxy, True, False), (None, False, False)):
        page, sessions = _pyppeteer_page()
        asyncio.run(puppeteer_scraper._open_page(Browser(page), proxy=proxy, user_agent=None, autosolve=False,
                                                 block_media=block))
        assert bool(sessions) is want_session, (proxy, block, sessions)
        if want_session:
            patterns = dict(sessions[0].sent)["Fetch.enable"]["patterns"]
            assert sorted(p["resourceType"] for p in patterns) == ["Font", "Image", "Media"]


@check("media (Selenium): Network.setBlockedURLs by extension (images, video, fonts — never scripts or styles) right after a proxied local driver is created; none without a proxy, with --load-media or over --cdp-endpoint")
def _():
    from unittest import mock

    class Driver:
        def __init__(self, *a, **k):
            self.sent = []

        def execute_cdp_cmd(self, cmd, params):
            self.sent.append((cmd, params))

        def quit(self):
            pass

    Opts = _SeleniumOptions
    fake_webdriver = type("W", (), {"Chrome": Driver})
    with mock.patch.object(selenium_scraper, "webdriver", fake_webdriver), \
            mock.patch.object(selenium_scraper, "Options", Opts), mock.patch.object(selenium_scraper, "Service", None):
        for argv, use_proxy, cdp, want in (([], True, False, True), (["--load-media"], True, False, False),
                                           ([], False, False, False), ([], True, True, False)):
            args = selenium_scraper.build_arg_parser().parse_args(["--query", "x", *argv])
            args.cdp_endpoint = "ws://127.0.0.1:9222" if cdp else None
            engine = selenium_scraper._SeleniumEngine(args, user_agent=None, client=None)
            session = asyncio.run(engine.open(proxy_pool.parse_proxy_line("h:1") if use_proxy else None))
            blocked = dict(session.driver.sent).get("Network.setBlockedURLs", {}).get("urls", [])
            assert bool(blocked) is want and engine.media_blocked is want, (argv, use_proxy, cdp, session.driver.sent)
            if want:
                assert all(p in blocked for p in ("*.jpg", "*.webp?*", "*.mp4", "*.woff2"))
                assert not any(p.startswith(("*.js", "*.css")) for p in blocked)


@check("media: every local-browser engine consults should_block_media and calls its own blocker (a source check: the suite runs without a browser binary)")
def _():
    for name, call in (("playwright_scraper", "_media_route"), ("selenium_scraper", "selenium_block_media"),
                       ("puppeteer_scraper", "pyppeteer_block_media")):
        source = (ROOT / f"{name}.py").read_text(encoding="utf-8")
        assert "should_block_media(" in source and f"{call}" in source, name
        assert "page_flow.precheck_proxy(" in source, name


@check("the canary check step passes a real complete run: canary 2026-10-20 got its 100 ads in 8 scroll batches over 5 scrolls, and was failed for scrolling 5 times rather than 7")
def _():
    import subprocess
    import sys
    if not (ROOT / ".github").is_dir():
        return  # the Docker image ships no workflows; the repository's own CI runs this check
    fixture = json.loads((ROOT / "tests" / "fixtures" / "fb_ads_canary_nike_us_20261020.json").read_text(encoding="utf-8"))
    meta, rows = fixture["meta"], fixture["rows"]
    assert meta["searches"][0]["scrolls"] == 5 and max(r["page"] for r in rows) == 8, "fixture changed"
    snippets = _canary_check_snippets()
    assert snippets, "no canary check step found in .github/workflows/canary.yml"
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "canary.json").write_text(json.dumps(rows), encoding="utf-8")
        (Path(tmp) / "canary.json.meta.json").write_text(json.dumps(meta), encoding="utf-8")
        env = dict(os.environ, HAS_PROXY="true", canary_exit="0", CANARY_REGION="US")
        for snippet in snippets:
            proc = subprocess.run([sys.executable, "-c", snippet], cwd=tmp, env=env,
                                  capture_output=True, text=True, timeout=60)
            assert proc.returncode == 0 and "canary: OK" in proc.stdout, proc.stdout[-400:] + proc.stderr[-400:]
        # The same run with its rows stopping before the 7th scroll batch is still red.
        short = [dict(r, page=min(r["page"], 6)) for r in rows]
        (Path(tmp) / "canary.json").write_text(json.dumps(short), encoding="utf-8")
        for snippet in snippets:
            proc = subprocess.run([sys.executable, "-c", snippet], cwd=tmp, env=env,
                                  capture_output=True, text=True, timeout=60)
            assert proc.returncode != 0 and "::error::" in proc.stdout, proc.stdout[-400:]


# --------------------------------------------------------------------------- #
# Proxy traffic meter and --proxy-budget-mb (proxy_meter.py; its own byte
# counts against a stub proxy are in tests/test_proxy_meter.py)
# --------------------------------------------------------------------------- #
import proxy_meter  # noqa: E402

_SECOND_URL = ap.search_url(query="adidas", region="US")


class _MeteredEngine(_FakeEngine):
    """_FakeEngine whose navigations and scrolls cost `cost` proxy bytes each."""

    def __init__(self, by_url, cost):
        super().__init__(by_url)
        self.cost = cost

    async def open(self, proxy):
        session = await super().open(proxy)
        goto, scroll = session.goto, session.scroll_to_bottom

        async def metered_goto(url):
            proxy_meter.current().add(self.cost)
            return await goto(url)

        async def metered_scroll():
            proxy_meter.current().add(self.cost)
            return await scroll()
        session.goto, session.scroll_to_bottom = metered_goto, metered_scroll
        return session


def _metered_run(by_url, urls, *, budget_mb, cost, active=True, argv=()):
    import contextlib
    import io
    args = playwright_scraper.build_arg_parser().parse_args(
        ["--query", "nike", "--region", "US", "--max-results", "100", "--delay-between-pages", "0",
         "--proxy-budget-mb", str(budget_mb), *argv])
    args._solve_budget = page_flow.SolveBudget(0)
    proxy_meter.start(args.proxy_budget_mb, active=active)
    engine = _MeteredEngine(by_url, cost)
    err = io.StringIO()
    try:
        with tempfile.TemporaryDirectory() as td:
            args.out = str(Path(td) / "o.json")
            Path(args.out).write_text("[]")  # a previous run's data
            with contextlib.redirect_stderr(err):
                rc = asyncio.run(page_flow.run(engine, args, urls=urls, proxy_pool=None, client=None, started_at=0.0))
            read = lambda suffix: json.loads(Path(args.out + suffix).read_text()) if Path(args.out + suffix).exists() else None  # noqa: E731
            meta, attempt, rows = read(".meta.json"), read(output_writer.ATTEMPT_META_SUFFIX), read("")
    finally:
        proxy_meter.start(None, active=False)
    return rc, meta, attempt, rows, engine, err.getvalue()


def _nike_with_scrolls(n=6):
    scroll = _fx("scroll_nike_us")
    return {"html": _fx("search_nike_us"), "batches": [[]] + [[_rekey(scroll, i)] for i in range(1, n + 1)]}


@check("proxy budget: once the metered bytes pass --proxy-budget-mb the run stops scrolling and fetches no further search — the rows already read are kept, exit 6, stop_reason proxy_budget_exceeded, the sidecar carries proxy_bytes / proxy_budget_bytes / proxy_budget_exceeded, and one 'Proxy traffic' line is printed")
def _():
    # 1,000-byte budget, 400 bytes per navigation or scroll: over it on the 2nd scroll.
    rc, meta, attempt, rows, engine, err = _metered_run({_NIKE_URL: _nike_with_scrolls(), "*": _nike_with_scrolls()},
                                                        [_NIKE_URL, _SECOND_URL], budget_mb=0.001, cost=400)
    assert rc == output_writer.EXIT_PARTIAL, (rc, err[-500:])
    assert engine.sessions[0].scrolled == 2 and len(engine.sessions) == 1, "no scroll and no search after the budget"
    assert rows and len(rows) > 6, len(rows or [])
    assert meta["status"] == "partial" and meta["stop_reason"] == "proxy_budget_exceeded", meta
    assert (meta["proxy_bytes"], meta["proxy_budget_bytes"], meta["proxy_budget_exceeded"]) == (1200, 1000, True), meta
    assert meta["unattempted_pages"] == [2] and meta["searches"][0]["stop"] == "proxy_budget_exceeded", meta
    assert err.count("Proxy traffic: 0.0 MB of 0.001 MB budget") == 1, err[-500:]
    assert attempt is None, "a run that wrote its rows has nothing to put in the attempt sidecar"


@check("proxy budget: spent before any row was read -> exit 5 (not 4, not 3); the previous data and .meta.json stay, and <out>.latest_attempt.meta.json records stop_reason proxy_budget_exceeded with the byte count")
def _():
    rc, meta, attempt, rows, engine, err = _metered_run({"*": {"html": _fx("loading_amazon")}}, [_NIKE_URL, _SECOND_URL],
                                                        budget_mb=0.001, cost=5000)
    assert rc == output_writer.EXIT_REMOTE_API_ERROR, (rc, err[-400:])
    assert rows == [] and meta is None, "the previous data stays and no .meta.json is written"
    assert (attempt["stop_reason"], attempt["proxy_bytes"], attempt["proxy_budget_exceeded"]) == \
        ("proxy_budget_exceeded", 5000, True), attempt
    assert attempt["data_updated"] is False and len(engine.sessions) == 1, attempt
    assert "Proxy traffic: 0.0 MB of 0.001 MB budget" in err


@check("proxy budget: finish_run puts the budget stop above every other outcome — a run that also met a block or a failed page still ends 6 with rows and 5 without, stop_reason proxy_budget_exceeded")
def _():
    import dataclasses
    rows = [output_writer.Product(**{f.name: None for f in dataclasses.fields(output_writer.Product)
                                     if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING})]
    rows[0].sku = "facebook-ad-1"
    try:
        for products, blocked, failed, want in ((rows, True, [], 6), ([], True, [], 5), (rows, False, [2], 6), ([], False, [], 5)):
            proxy_meter.start(0.001, active=True)
            proxy_meter.current().add(5000)
            with tempfile.TemporaryDirectory() as td:
                out = str(Path(td) / "o.json")
                rc = output_writer.finish_run(products=products, out_path=out, fmt="json", engine="t", url="u",
                                              pages_requested=2, pages_completed=1, failed_pages=failed, blocked=blocked,
                                              remote_api_error=False, allow_empty=False, started_at=0.0)
                path = Path(out + (".meta.json" if products else output_writer.ATTEMPT_META_SUFFIX))
                meta = json.loads(path.read_text())
            assert rc == want and meta["stop_reason"] == "proxy_budget_exceeded", (blocked, failed, rc, meta)
            assert meta["proxy_budget_exceeded"] is True and meta["proxy_bytes"] == 5000, meta
    finally:
        proxy_meter.start(None, active=False)


@check("proxy budget: with no local proxy nothing is metered or stopped — proxy_bytes is null, no 'Proxy traffic' line; --proxy-budget-mb 0 is unlimited; a run under its budget is complete (exit 0) and records its bytes")
def _():
    rc, meta, _a, rows, _e, err = _metered_run({_NIKE_URL: _nike_with_scrolls(2)}, [_NIKE_URL], budget_mb=0.001,
                                               cost=10 ** 9, active=False)
    assert meta["proxy_bytes"] is None and meta["proxy_budget_exceeded"] is False and "Proxy traffic" not in err, meta
    assert meta["stop_reason"] != "proxy_budget_exceeded"
    rc, meta, _a, rows, _e, err = _metered_run({_NIKE_URL: _nike_with_scrolls(2)}, [_NIKE_URL], budget_mb=0, cost=10 ** 9)
    assert meta["proxy_budget_bytes"] is None and meta["proxy_budget_exceeded"] is False and meta["proxy_bytes"] > 10 ** 9
    rc, meta, _a, rows, _e, err = _metered_run({_NIKE_URL: _nike_with_scrolls(2)}, [_NIKE_URL], budget_mb=10, cost=1000,
                                               argv=["--max-results", "6"])
    assert rc == output_writer.EXIT_OK and meta["proxy_bytes"] == 1000 and meta["proxy_budget_bytes"] == 10 ** 7, (rc, meta)
    assert "Proxy traffic: 0.0 MB of 10 MB budget" in err


@check("proxy budget: --details stops sending details requests once the budget is spent (the rows are kept, details left empty)")
def _():
    async def go():
        proxy_meter.start(0.001, active=True)
        proxy_meter.current().add(2000)
        args = playwright_scraper.build_arg_parser().parse_args(["--query", "x", "--details"])
        outcome = page_flow.SearchOutcome(url=_NIKE_URL)
        collector = ap.Collector(region="US", limit=10)
        collector.add(ap.page_batches(_fx("search_nike_us")), page=0)

        class Session:
            posted = 0

            async def take_requests(self):
                return [("av=1&doc_id=1&variables=%7B%7D", "{}")]

            async def click_text(self, text):
                return True

            async def wait(self, s):
                pass

            async def post_form(self, data):
                Session.posted += 1
                return 200, "{}"
        await page_flow._details(type("E", (), {"can_details": True})(), args, Session(), collector, outcome)
        return outcome, Session.posted
    try:
        outcome, posted = asyncio.run(go())
    finally:
        proxy_meter.start(None, active=False)
    assert outcome.details in ("proxy_budget_exceeded", "error") and posted == 0, (outcome.details, posted)


@check("proxy budget: --proxy-budget-mb is on every engine (default 10, 0 = unlimited, a negative or non-number value is a usage error naming the flag); the meter is active only for a LOCAL proxy — never over --cdp-endpoint, --scraper-api or with no proxy")
def _():
    import contextlib
    import io
    for mod in _ENGINES:
        parser = mod.build_arg_parser()
        assert parser.parse_args(["--query", "x"]).proxy_budget_mb == 10, mod.__name__
        assert "--proxy-budget-mb" in parser.format_help(), mod.__name__
        for bad in ("-1", "lots"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                try:
                    parser.parse_args(["--query", "x", "--proxy-budget-mb", bad])
                    raise AssertionError("accepted %s" % bad)
                except SystemExit as exc:
                    assert exc.code == 2
            assert "--proxy-budget-mb" in err.getvalue(), err.getvalue()
        source = (ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8")
        assert source.index("page_flow.start_proxy_meter(") < source.index("page_flow.precheck_proxy("), mod.__name__
    import argparse
    pool = proxy_pool.ProxyPool([proxy_pool.parse_proxy_line("h:1")])
    ns = lambda **k: argparse.Namespace(proxy_budget_mb=10, cdp_endpoint=None, scraper_api=False, **k)  # noqa: E731
    try:
        assert page_flow.start_proxy_meter(ns(), pool).active is True
        assert page_flow.start_proxy_meter(ns(), None).active is False
        assert page_flow.start_proxy_meter(argparse.Namespace(proxy_budget_mb=10, cdp_endpoint="ws://h", scraper_api=False), pool).active is False
        assert page_flow.start_proxy_meter(argparse.Namespace(proxy_budget_mb=10, cdp_endpoint=None, scraper_api=True), pool).active is False
    finally:
        proxy_meter.start(None, active=False)


@check("proxy meter wiring: Playwright counts a proxied LOCAL context (requestfinished + requestfailed) and adds the budget route after the media route; Selenium tees its performance log; pyppeteer opens a counting CDP session; none of it over --cdp-endpoint or with no proxy")
def _():
    import argparse
    from unittest import mock

    class Context:
        def __init__(self):
            self.routes, self.events = [], []

        def on(self, name, fn):
            self.events.append(name)

        async def route(self, pattern, handler):
            self.routes.append(handler)

        async def new_page(self):
            return type("P", (), {"on": lambda *a: None})()

    class Browser:
        def __init__(self):
            self.contexts = [Context()]

        async def new_context(self, **kw):
            return Context()

    proxy = proxy_pool.parse_proxy_line("h:1:u:p")
    try:
        for use_proxy, cdp, argv, want_events, want_routes in (
                (True, False, [], True, 2), (True, False, ["--proxy-budget-mb", "0"], True, 1),
                (True, False, ["--load-media"], True, 1), (False, False, [], False, 0), (True, True, [], False, 0)):
            args = playwright_scraper.build_arg_parser().parse_args(["--query", "x", *argv])
            args.cdp_endpoint = "ws://h:1" if cdp else None
            proxy_meter.start(args.proxy_budget_mb, active=use_proxy and not cdp)
            engine = playwright_scraper._PlaywrightEngine(Browser(), args, autosolve=False, user_agent=None, client=None)
            session = asyncio.run(engine.open(proxy if use_proxy else None))
            context = session.context
            assert (sorted(set(context.events)) == ["requestfailed", "requestfinished"]) is want_events, (argv, context.events)
            assert len(context.routes) == want_routes, (argv, use_proxy, cdp, context.routes)
            if want_routes and args.proxy_budget_mb:
                assert context.routes[-1] == proxy_meter.current().playwright_budget_route_async

        class Driver:
            def __init__(self, *a, **k):
                self.sent = []

            def execute_cdp_cmd(self, cmd, params):
                self.sent.append((cmd, params))

            def get_log(self, kind):
                return []

            def quit(self):
                pass

        with mock.patch.object(selenium_scraper, "webdriver", type("W", (), {"Chrome": Driver})), \
                mock.patch.object(selenium_scraper, "Options", _SeleniumOptions), \
                mock.patch.object(selenium_scraper, "Service", None):
            for use_proxy, cdp, want in ((True, False, True), (False, False, False), (True, True, False)):
                args = selenium_scraper.build_arg_parser().parse_args(["--query", "x"])
                args.cdp_endpoint = "ws://127.0.0.1:9222" if cdp else None
                proxy_meter.start(10, active=use_proxy and not cdp)
                engine = selenium_scraper._SeleniumEngine(args, user_agent=None, client=None)
                session = asyncio.run(engine.open(proxy_pool.parse_proxy_line("h:1") if use_proxy else None))
                assert hasattr(session.driver, "_proxy_meter_poll") is want, (use_proxy, cdp)

        attached = []

        async def fake_attach(self, page):
            attached.append(page)

        async def fake_launch(**kw):
            return "browser"

        async def fake_open_page(browser, **kw):
            return argparse.Namespace(on=lambda *a: None)
        with mock.patch.object(puppeteer_scraper, "_launch", fake_launch), \
                mock.patch.object(puppeteer_scraper, "_open_page", fake_open_page), \
                mock.patch.object(proxy_meter.ProxyMeter, "attach_pyppeteer", fake_attach):
            for use_proxy, remote, want in ((True, None, 1), (False, None, 0), (True, "remote", 0)):
                attached.clear()
                args = puppeteer_scraper.build_arg_parser().parse_args(["--query", "x"])
                proxy_meter.start(10, active=use_proxy and not remote)
                engine = puppeteer_scraper._PyppeteerEngine(args, remote_browser=remote, autosolve=False,
                                                            user_agent=None, client=None)
                asyncio.run(engine.open(proxy if use_proxy else None))
                assert len(attached) == want, (use_proxy, remote, attached)
    finally:
        proxy_meter.start(None, active=False)


@check("canary: the run passes --proxy-budget-mb 10 and is followed by an always-run 'Proxy traffic' step; that step writes '**canary**: proxy traffic X.X MB of 10 MB' to the job summary, is red with '::error::stopped: proxy budget 10 MB exceeded' when the run stopped at the budget, and copes with a missing sidecar")
def _():
    import subprocess
    import sys
    if not (ROOT / ".github").is_dir():
        return  # the Docker image ships no workflows; the repository's own CI runs this check
    text = (ROOT / ".github" / "workflows" / "canary.yml").read_text(encoding="utf-8")
    runs = [line for line in text.replace("\\\n", " ").splitlines() if "_scraper.py" in line and "--out" in line]
    assert runs and all("--proxy-budget-mb 10" in line for line in runs), runs
    step = text.split("- name: Proxy traffic", 1)
    assert len(step) == 2 and text.index("--out canary.json") < text.index("- name: Proxy traffic"), "no summary step"
    assert "if: always()" in step[1].split("- name:", 1)[0], "the summary step must run whatever the run did"
    assert "python3 .github/proxy_traffic_summary.py canary.json canary 10" in step[1].split("- name:", 1)[0]
    script = str(ROOT / ".github" / "proxy_traffic_summary.py")
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.md"
        env = dict(os.environ, GITHUB_STEP_SUMMARY=str(summary))

        def summarise(meta=None, attempt=None):
            for suffix, data in ((".meta.json", meta), (".latest_attempt.meta.json", attempt)):
                path = Path(tmp) / ("canary.json" + suffix)
                path.unlink() if path.exists() else None
                if data is not None:
                    path.write_text(json.dumps(data), encoding="utf-8")
            return subprocess.run([sys.executable, script, "canary.json", "canary", "10"], cwd=tmp, env=env,
                                  capture_output=True, text=True, timeout=60)
        proc = summarise({"proxy_bytes": 4_321_000, "proxy_budget_bytes": 10 ** 7, "proxy_budget_exceeded": False,
                          "finished_at": 2})
        assert proc.returncode == 0 and "::error::" not in proc.stdout, proc.stdout
        assert summary.read_text().splitlines()[-1] == "**canary**: proxy traffic 4.3 MB of 10 MB"
        proc = summarise(None, {"proxy_bytes": 10_200_000, "proxy_budget_bytes": 10 ** 7,
                                "proxy_budget_exceeded": True, "finished_at": 3})
        assert proc.returncode == 1 and "::error::stopped: proxy budget 10 MB exceeded" in proc.stdout, proc.stdout
        assert summary.read_text().splitlines()[-1] == "**canary**: proxy traffic 10.2 MB of 10 MB"
        proc = summarise({"proxy_bytes": 1, "proxy_budget_exceeded": False, "finished_at": 1},
                         {"proxy_bytes": 10_200_000, "proxy_budget_exceeded": True, "finished_at": 5})
        assert proc.returncode == 1, "the newer of the two sidecars is the run's"
        proc = summarise()
        assert proc.returncode == 0 and "not recorded" in summary.read_text().splitlines()[-1], proc.stdout
        proc = summarise({"proxy_bytes": None, "proxy_budget_exceeded": False})
        assert proc.returncode == 0 and "no local proxy" in summary.read_text().splitlines()[-1]
        (Path(tmp) / "canary.json.meta.json").write_text("{not json", encoding="utf-8")
        proc = subprocess.run([sys.executable, script, "canary.json", "canary", "10"], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0 and "unreadable" in proc.stdout, proc.stdout


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
