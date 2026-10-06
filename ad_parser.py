#!/usr/bin/env python3
"""ad_parser.py — this IS the Meta Ad Library site knowledge.

Written 2026-10-05 from live captures of www.facebook.com/ads/library/,
fetched logged out (no account, no cookies) from a residential IP, direct
and through a residential proxy: keyword searches in the US, DE and
worldwide, one advertiser's ads (`view_all_page_id`), a political-ads
search, a search with no results, and a page captured before its results
had painted. The trimmed captures are in `tests/fixtures/`.

What a logged-out visitor is actually served:

  - **No login is asked for.** 46 page loads from one address in an hour,
    direct, all served the library. There is no login wall and no
    challenge on this route — unlike the rest of facebook.com, which puts
    a "See more on Facebook" login dialog over every page.
  - **The document answers HTTP 403 — with the full page in it.** Every
    capture, direct and proxied, had status 403 on the main document and
    the results in the body. So a 403 here is NOT a block, and the status
    code is the one signal this site cannot be read by (the reverse of the
    family default in page_flow, which this module's `page_state()`
    therefore decides before the status is looked at).
  - **The first batch of results is embedded in the page**, in one
    `<script type="application/json">` block carrying
    `search_results_connection`: up to 30 ads, the connection's `count`
    (the library's own result total, "~24,000 results" — capped at 50,001
    on the broadest searches) and `page_info` (`has_next_page`,
    `end_cursor`). Later batches arrive as `AdLibrarySearchPaginationQuery`
    GraphQL responses — 10 ads each, one per scroll — with the same
    `search_results_connection` shape. There is no `?page=N`: the cursor
    is minted per session, so the only way past the first 30 is to
    scroll the page that was served.
  - **A broad search can be served before it is painted.** The shell
    (title "Ad Library", the `AdLibraryFoundationRoot` module) arrives
    first and the first batch is streamed into the page later; on three
    of 13 popular searches it was still missing 6 seconds after the page
    loaded, and present a few seconds after that. That is "loading", not
    "empty" and not "blocked" (CLAUDE.md §18: which of the three missing
    content is decides the answer).
  - **No results** is said in so many words — "No ads match your search
    criteria" — and the connection's `count` is 0.
  - **An ad** is one `collated_results` entry: the Library ID
    (`ad_archive_id`), the advertiser (`page_id`, `page_name`, and inside
    `snapshot` the Page URL, its categories and its EXACT like count), the
    dates it ran (`start_date` / `end_date`, epoch seconds at midnight
    Pacific time — `end_date` of an active ad is today), the platforms,
    Meta's own ad categories, and the creative in `snapshot`: text
    (`body.text`), link, call to action, `display_format`, `images`,
    `videos` and `cards`. A political ad adds `spend` ("$200K - $250K"),
    `currency`, `impressions_with_index.impressions_text` (">1M"),
    `reach_estimate` and a "Paid for by" `byline`.
  - **A catalogue ad's top-level text is a template.** DPA and DCO ads
    (352 of the 549 ads captured) carry `{{product.name}}` and the like
    in `title`/`body`; the rendered text of each variant is in `cards`.
    No TEXT column ever holds an unrendered template. `link_url` may: an
    advertiser's tracking parameters often carry Meta's URL macros
    (`utm_campaign={{campaign.name}}`, 16 of 500 ads on one live search),
    filled in by Meta at click time — the URL is kept as published.
  - **The same ad can come back twice** — once in the embedded batch and
    again in a scroll batch (39 distinct ads out of 40 on one search) — so
    rows are deduped on the Library ID, first occurrence kept.
  - **The country defaults to the visitor's own.** A URL without
    `country=` was redirected to the exit IP's country (KZ, from a
    Kazakh address). Every URL this tool builds or accepts therefore names
    its country explicitly.
  - **A single-ad link (`?id=…`) is not a lookup.** Opened logged out it
    ran a search in the exit country and returned a DIFFERENT ad of the
    same advertiser, so `?id=` URLs are refused rather than answered with
    the wrong row.

Not collected, on purpose: the advertiser contact details a political ad's
disclosure carries (phone, e-mail, street address) — they are often a
person's — and anything about who saw an ad beyond the ranges the library
prints.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterator, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse

from bs4 import BeautifulSoup

from output_writer import Product

log = logging.getLogger("ad_parser")

BASE_URL = "https://www.facebook.com/ads/library/"
SOURCE = "facebook.com"
MIN_CARD_MATCHES = 1  # a batch with one new ad is progress (CLAUDE.md §5 names the constant)

# The shell every Ad Library page is built from, painted or not — present on
# all 46 captures, absent from every other facebook.com page captured.
SHELL_MARKER = "AdLibraryFoundationRoot"
NO_RESULTS_TEXT = "No ads match your search criteria"
PAGINATION_QUERY = "AdLibrarySearchPaginationQuery"

# Where a facebook.com page sends a visitor it wants logged in. Never seen
# on the Ad Library in any capture; kept so that a login redirect, if it
# ever comes, is reported as a block rather than as an empty search.
LOGIN_PATHS = ("/login", "/checkpoint")

# No captcha vendor appeared on any capture; the generic set in
# captcha_solver matches none of the good pages (smoke_test checks that).
BOT_CHALLENGE_MARKERS: tuple = ()

ACTIVE_STATUSES = ("active", "inactive", "all")
AD_TYPES = ("all", "political_and_issue_ads")
# Every country the library's own country menu offers is two letters; ALL is
# the library's word for "every country".
_REGION_RE = re.compile(r"^(?:[A-Z]{2}|ALL)$")
_PAGE_ID_RE = re.compile(r"^\d{3,20}$")
_TEMPLATE_RE = re.compile(r"\{\{[^}]*\}\}")
_SEARCH_KEYS = ("q", "view_all_page_id")

PACIFIC_OFFSET_S = 7 * 3600  # start/end dates are midnight Pacific: 07:00 or 08:00 UTC


# --------------------------------------------------------------------------- #
# Inputs: a search is built from flags, or taken from a pasted library URL
# --------------------------------------------------------------------------- #
def normalize_region(value: Optional[str]) -> str:
    region = (value or "ALL").strip().upper()
    if not _REGION_RE.match(region):
        raise ValueError(f"--region must be a two-letter country code or ALL (got {value!r})")
    return region


def search_url(*, query: Optional[str] = None, page_id: Optional[str] = None, region: str = "ALL",
               active_status: str = "active", ad_type: str = "all", exact_phrase: bool = False) -> str:
    """The library URL a visitor's own search would produce. One of `query`
    (a keyword search) or `page_id` (every ad of one advertiser)."""
    if bool(query) == bool(page_id):
        raise ValueError("give exactly one of --query or --page-id")
    if active_status not in ACTIVE_STATUSES:
        raise ValueError(f"--active-status must be one of {', '.join(ACTIVE_STATUSES)}")
    if ad_type not in AD_TYPES:
        raise ValueError(f"--ad-type must be one of {', '.join(AD_TYPES)}")
    params = {"active_status": active_status, "ad_type": ad_type, "country": normalize_region(region),
              "media_type": "all"}
    if page_id:
        if not _PAGE_ID_RE.match(str(page_id)):
            raise ValueError(f"--page-id must be the advertiser's numeric Page ID (got {page_id!r})")
        params.update({"search_type": "page", "view_all_page_id": str(page_id)})
    else:
        params.update({"q": query.strip(), "search_type": "keyword_exact_phrase" if exact_phrase else "keyword_unordered"})
    return BASE_URL + "?" + urlencode(params)


def normalize_input(text: str, *, region: str = "ALL") -> Optional[str]:
    """A pasted Ad Library search URL, made explicit: the country it names,
    or `region` when it names none (a URL without one runs in the
    visitor's own country — see the module docstring). None for anything
    that is not a library SEARCH: another page, another host, or a
    single-ad `?id=` link."""
    raw = (text or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlparse(raw)
    host = (parts.hostname or "").lower()
    if host not in ("facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"):
        return None
    if parts.path.rstrip("/") != "/ads/library":
        return None
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    if "id" in params and not any(params.get(k) for k in _SEARCH_KEYS):
        return None
    if not any(params.get(k) for k in _SEARCH_KEYS):
        return None
    params.setdefault("country", normalize_region(region))
    params["country"] = normalize_region(params["country"])
    params.setdefault("active_status", "active")
    params.setdefault("ad_type", "all")
    return BASE_URL + "?" + urlencode(sorted(params.items()))


def refusal_reason(text: str) -> str:
    """Why `normalize_input` refused this input, in the reader's terms."""
    raw = (text or "").strip()
    parts = urlparse(raw if "://" in raw else "https://" + raw)
    params = dict(parse_qsl(parts.query))
    if parts.path.rstrip("/") == "/ads/library" and "id" in params:
        return ("a single-ad link (?id=) — logged out, the library answers it with a search that returns a "
                "different ad; search the advertiser instead (--page-id)")
    if (parts.hostname or "").lower().endswith("facebook.com") and parts.path.rstrip("/") == "/ads/library":
        return "an Ad Library URL with no search in it (no q= and no view_all_page_id=)"
    return "not a facebook.com/ads/library/ search URL"


def fetch_url(url: str) -> str:
    """The address actually opened: plus `locale=en_US`. The page language
    follows the browser — pyppeteer on a Russian-language machine got the
    library in Russian despite --lang=en-US (measured 2026-10-06), with no
    "See ad details" button to click for --details. Rows keep the search
    URL as given."""
    return url + ("&" if "?" in url else "?") + "locale=en_US"


def url_region(url: str) -> Optional[str]:
    return dict(parse_qsl(urlparse(url).query)).get("country")


# --------------------------------------------------------------------------- #
# Reading a page and a GraphQL response
# --------------------------------------------------------------------------- #
def json_blocks(html: str) -> List[Any]:
    """Every `<script type="application/json">` block that decodes. One bad
    block must not hide the others."""
    out = []
    for script in BeautifulSoup(html or "", "html.parser").find_all("script", type="application/json"):
        raw = script.string or script.get_text()
        if "search_results_connection" not in raw:
            continue  # ~50 blocks per page; only one carries results
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def graphql_payloads(body: str) -> List[Any]:
    """A GraphQL response body is one JSON document per line (streamed
    parts), sometimes behind Facebook's `for (;;);` guard."""
    out = []
    for line in (body or "").splitlines():
        line = line.strip()
        if line.startswith("for (;;);"):
            line = line[len("for (;;);"):]
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _find(obj: Any, key: str) -> Iterator[dict]:
    """Every dict (depth-first, document order) that has `key`."""
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if key in node:
                yield node
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))


def _find_key(obj: Any, key: str) -> Iterator[Any]:
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                if k == key:
                    yield v
                stack.append(v)
        elif isinstance(node, list):
            stack.extend(reversed(node))


@dataclass
class Batch:
    """One `search_results_connection`: the ads it carries, in the site's
    order, and what it says about the rest."""
    ads: List[dict] = field(default_factory=list)
    total: Optional[int] = None
    has_next_page: Optional[bool] = None
    end_cursor: Optional[str] = None


def batches_from(payloads: List[Any]) -> List[Batch]:
    out = []
    for payload in payloads:
        for conn in _find_key(payload, "search_results_connection"):
            if not isinstance(conn, dict):
                continue
            batch = Batch(total=_as_int(conn.get("count")))
            info = conn.get("page_info") if isinstance(conn.get("page_info"), dict) else {}
            batch.has_next_page = info.get("has_next_page") if isinstance(info.get("has_next_page"), bool) else None
            batch.end_cursor = info.get("end_cursor") if isinstance(info.get("end_cursor"), str) else None
            for edge in conn.get("edges") or []:
                node = edge.get("node") if isinstance(edge, dict) else None
                for ad in (node or {}).get("collated_results") or []:
                    if isinstance(ad, dict) and isinstance(ad.get("ad_archive_id"), str):
                        batch.ads.append(ad)
            out.append(batch)
    return out


def page_batches(html: str) -> List[Batch]:
    return batches_from(json_blocks(html))


def response_batches(body: str) -> List[Batch]:
    return batches_from(graphql_payloads(body))


def is_results_response(body: str) -> bool:
    """A captured GraphQL body worth parsing: it carries search results.
    Decided on the BODY, so all three engines agree without having to read
    a request header Selenium's log does not always carry."""
    return '"search_results_connection"' in (body or "")


_TITLE_RE = re.compile(r"<title[^>]*>([^<]{0,120})</title>", re.I)


def page_title(html: str) -> Optional[str]:
    m = _TITLE_RE.search(html or "")
    return m.group(1).strip() if m else None


# A login page announces itself in its canonical link even when the
# address it was fetched under does not change — measured 2026-10-06: the
# Scraper API's own pool was served the login page (canonical
# https://fi-fi.facebook.com/login) for a Marketplace search, and with no
# final URL to go by it was misread as an unknown page rather than a block.
_LOGIN_CANONICAL_RE = re.compile(r'<link[^>]+rel="canonical"[^>]+href="https?://[a-z-]*\.?facebook\.com/login', re.I)


def is_login_page(html: str) -> bool:
    return bool(_LOGIN_CANONICAL_RE.search(html or ""))


def page_state(html: str, *, final_url: Optional[str] = None) -> str:
    """What the served page IS, strongest signal first (CLAUDE.md §17: order
    signals by how much they prove): `content` (results embedded),
    `empty` (the library's own no-results sentence, or a result count of
    0), `login` (a login or checkpoint page), `loading` (the library's
    shell with nothing painted yet), or `unknown` (not an Ad Library
    page at all — Chromium's own error page, an interstitial)."""
    batches = page_batches(html)
    if any(b.ads for b in batches):
        return "content"
    if NO_RESULTS_TEXT in (html or "") or any(b.total == 0 for b in batches):
        return "empty"
    path = urlparse(final_url or "").path
    if any(path.startswith(p) for p in LOGIN_PATHS) or is_login_page(html):
        return "login"
    if SHELL_MARKER in (html or ""):
        return "loading"
    return "unknown"


# --------------------------------------------------------------------------- #
# One ad → one row
# --------------------------------------------------------------------------- #
def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _text(value: Any) -> Optional[str]:
    """A string the creative shows, or None — never an unrendered template."""
    if isinstance(value, dict):
        value = value.get("text")
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or _TEMPLATE_RE.search(value):
        return None
    return value


def _date(ts: Any) -> Optional[_dt.date]:
    if not isinstance(ts, int) or isinstance(ts, bool) or ts <= 0:
        return None
    return _dt.datetime.fromtimestamp(ts - PACIFIC_OFFSET_S, _dt.timezone.utc).date()


def _json_list(values: List[Any]) -> Optional[str]:
    return json.dumps(values, ensure_ascii=False) if values else None


def _first_line(text: Optional[str], limit: int = 120) -> Optional[str]:
    if not text:
        return None
    line = text.strip().splitlines()[0].strip()
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


def make_sku(ad_archive_id: str) -> str:
    return f"facebook-ad-{ad_archive_id}"


def ad_permalink(ad_archive_id: str) -> str:
    return f"{BASE_URL}?id={ad_archive_id}"


def _media(snapshot: dict) -> tuple:
    """(image URLs, video URLs) across the creative and its cards, in the
    site's order, each once. A video's preview image counts as an image —
    it is what the card shows before playing."""
    images: List[str] = []
    videos: List[str] = []

    def add(bucket, url):
        if isinstance(url, str) and url.startswith("http") and url not in bucket:
            bucket.append(url)

    sources = list(snapshot.get("images") or []) + list(snapshot.get("videos") or []) + list(snapshot.get("cards") or [])
    for item in sources:
        if not isinstance(item, dict):
            continue
        add(images, item.get("original_image_url") or item.get("resized_image_url"))
        add(images, item.get("video_preview_image_url"))
        add(videos, item.get("video_hd_url") or item.get("video_sd_url"))
    return images, videos


def ad_row(ad: dict, *, region: Optional[str], page: int, position: int, scraped_at: str) -> Optional[Product]:
    """One `collated_results` entry as a row, or None if it carries no
    Library ID."""
    ad_id = ad.get("ad_archive_id")
    if not isinstance(ad_id, str) or not ad_id.isdigit():
        return None
    snap = ad.get("snapshot") if isinstance(ad.get("snapshot"), dict) else {}
    cards = [c for c in snap.get("cards") or [] if isinstance(c, dict)]
    first_card = cards[0] if cards else {}

    body = _text(snap.get("body")) or _text(first_card.get("body"))
    link_title = _text(snap.get("title")) or _text(first_card.get("title"))
    advertiser = ad.get("page_name") if isinstance(ad.get("page_name"), str) else snap.get("page_name")
    images, videos = _media(snap)

    start, end = _date(ad.get("start_date")), _date(ad.get("end_date"))
    days = (end - start).days + 1 if start and end and end >= start else None

    impressions = ad.get("impressions_with_index") if isinstance(ad.get("impressions_with_index"), dict) else {}
    political = snap.get("disclaimer_label") == "PAID_FOR_BY" or "POLITICAL" in (ad.get("categories") or [])
    spend = ad.get("spend") if isinstance(ad.get("spend"), str) and ad.get("spend") else None
    categories = [c for c in ad.get("categories") or [] if isinstance(c, str)]

    return Product(
        sku=make_sku(ad_id),
        source=SOURCE,
        category="ad",
        title=_first_line(body) or _first_line(link_title) or advertiser,
        brand=None,
        price=None,
        currency=None,
        price_source=None,
        product_url=ad_permalink(ad_id),
        image_url=images[0] if images else None,
        scraped_at=scraped_at,
        ad_id=ad_id,
        collation_id=ad.get("collation_id") if isinstance(ad.get("collation_id"), str) else None,
        versions=_as_int(ad.get("collation_count")) or 1,
        is_active=ad.get("is_active") if isinstance(ad.get("is_active"), bool) else None,
        advertiser_name=advertiser,
        advertiser_page_id=str(ad["page_id"]) if ad.get("page_id") else None,
        advertiser_url=snap.get("page_profile_uri") if isinstance(snap.get("page_profile_uri"), str) else None,
        advertiser_likes=_as_int(snap.get("page_like_count")),
        advertiser_categories_json=_json_list(list(dict.fromkeys(c for c in snap.get("page_categories") or [] if isinstance(c, str)))),
        first_shown_at=start.isoformat() if start else None,
        last_shown_at=end.isoformat() if end else None,
        days_shown=days,
        platforms_json=_json_list([p for p in ad.get("publisher_platform") or [] if isinstance(p, str)]),
        display_format=snap.get("display_format") if isinstance(snap.get("display_format"), str) else None,
        body=body,
        link_url=snap.get("link_url") or first_card.get("link_url") or None,
        link_title=link_title,
        link_description=_text(snap.get("link_description")) or _text(first_card.get("link_description")),
        link_caption=_text(snap.get("caption")) or _text(first_card.get("caption")),
        cta_text=_text(snap.get("cta_text")) or _text(first_card.get("cta_text")),
        cta_type=snap.get("cta_type") if isinstance(snap.get("cta_type"), str) else None,
        card_count=len(cards),
        image_urls_json=_json_list(images),
        video_urls_json=_json_list(videos),
        ad_categories_json=_json_list(categories),
        paid_for_by=(snap.get("byline") or None) if political else None,
        spend=spend,
        spend_currency=(ad.get("currency") or None) if spend else None,
        impressions=impressions.get("impressions_text") if isinstance(impressions.get("impressions_text"), str) else None,
        reach=ad.get("reach_estimate") if isinstance(ad.get("reach_estimate"), str) else None,
        region=region,
        page=page,
        position=position,
    )


# --------------------------------------------------------------------------- #
# --details: the ad's own "See ad details" dialog
# --------------------------------------------------------------------------- #
# Measured 2026-10-06, logged out: clicking "See ad details" sends
# AdLibraryV3AdDetailsQuery for that ad (variables adArchiveID, pageID,
# country, sessionID, isAdNonPolitical, isAdNotAAAEligible). Sent again from
# the page with another ad's ids it answered for that ad — 30 of 30 in a
# row at 1.5s apart. The answer adds what the search does not carry: EU and
# UK reach with an age x gender x country breakdown (for ads delivered
# there), the targeted ages / gender / locations, the payer and
# beneficiary, and the advertiser's Instagram account and follower count.
DETAILS_QUERY = "AdLibraryV3AdDetailsQuery"
DETAILS_BUTTON_TEXT = "See ad details"
RATE_LIMIT_TEXT = "Rate limit exceeded"


def is_details_request(post_data: Optional[str]) -> bool:
    return f"fb_api_req_friendly_name={DETAILS_QUERY}" in (post_data or "")


def details_ad_id(post_data: str) -> Optional[str]:
    for key, value in parse_qsl(post_data or "", keep_blank_values=True):
        if key == "variables":
            try:
                return str(json.loads(value).get("adArchiveID") or "") or None
            except ValueError:
                return None
    return None


def details_request(post_data: str, *, ad_id: str, page_id: str, political: bool, aaa_eligible: Optional[bool]) -> Optional[str]:
    """The page's own details form for ANOTHER ad: only its ids (and the two
    flags the page derives from the ad) changed; every other field as sent."""
    out, found = [], False
    for key, value in parse_qsl(post_data or "", keep_blank_values=True):
        if key == "variables":
            try:
                variables = json.loads(value)
            except ValueError:
                return None
            variables.update({"adArchiveID": str(ad_id), "pageID": str(page_id), "isAdNonPolitical": not political})
            if aaa_eligible is not None:
                variables["isAdNotAAAEligible"] = not aaa_eligible
            value, found = json.dumps(variables, separators=(",", ":")), True
        out.append((key, value))
    return urlencode(out) if found else None


def is_rate_limited(body: Optional[str]) -> bool:
    return RATE_LIMIT_TEXT in (body or "")


def _breakdown(transparency: dict) -> list:
    out = []
    for country in transparency.get("age_country_gender_reach_breakdown") or []:
        if not isinstance(country, dict):
            continue
        for row in country.get("age_gender_breakdowns") or []:
            if isinstance(row, dict):
                out.append({"country": country.get("country"), "age": row.get("age_range"),
                            "male": row.get("male"), "female": row.get("female"), "unknown": row.get("unknown")})
    return out


def details_fields(body: str) -> Optional[dict]:
    """The columns --details adds, from one AdLibraryV3AdDetailsQuery answer,
    or None when the answer carries no ad details."""
    payloads = graphql_payloads(body)
    info = next((n["ad_library_page_info"] for p in payloads for n in _find(p, "ad_library_page_info")
                 if isinstance(n.get("ad_library_page_info"), dict)), None)
    by_loc = next((n["transparency_by_location"] for p in payloads for n in _find(p, "transparency_by_location")
                   if isinstance(n.get("transparency_by_location"), dict)), None)
    if info is None and by_loc is None:
        return None
    by_loc = by_loc or {}
    eu = by_loc.get("eu_transparency") if isinstance(by_loc.get("eu_transparency"), dict) else {}
    uk = by_loc.get("uk_transparency") if isinstance(by_loc.get("uk_transparency"), dict) else {}
    target = eu or uk
    page_info = (info or {}).get("page_info") if isinstance((info or {}).get("page_info"), dict) else {}
    payer = next((n["payer_beneficiary_data"] for p in payloads for n in _find(p, "payer_beneficiary_data")
                  if isinstance(n.get("payer_beneficiary_data"), list)), [])
    payer = payer[0] if payer and isinstance(payer[0], dict) else {}
    ages = target.get("age_audience") if isinstance(target.get("age_audience"), dict) else {}
    locations = [l.get("name") for l in target.get("location_audience") or [] if isinstance(l, dict) and l.get("name")]
    breakdown = _breakdown(eu) + [dict(r, country=r["country"]) for r in _breakdown(uk)]
    return {
        "eu_reach": _as_int(eu.get("eu_total_reach")),
        "uk_reach": _as_int(uk.get("total_reach")),
        "reach_breakdown_json": _json_list(breakdown),
        "target_ages": f"{ages['min']}-{ages['max']}" if ages.get("min") is not None and ages.get("max") is not None else None,
        "target_gender": target.get("gender_audience") if isinstance(target.get("gender_audience"), str) else None,
        "target_locations_json": _json_list(locations),
        "payer": payer.get("payer") if isinstance(payer.get("payer"), str) else None,
        "beneficiary": payer.get("beneficiary") if isinstance(payer.get("beneficiary"), str) else None,
        "advertiser_ig_username": page_info.get("ig_username") if isinstance(page_info.get("ig_username"), str) else None,
        "advertiser_ig_followers": _as_int(page_info.get("ig_followers")),
        "advertiser_verification": next((n["page_verification"] for p in payloads for n in _find(p, "page_verification")
                                         if isinstance(n.get("page_verification"), str)), None),
    }


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Collector:
    """Rows in the order the site returned them, deduped on the Library ID
    (first occurrence kept — CLAUDE.md §8: merge in page order, not arrival
    order), with the facts a run's sidecar needs."""

    def __init__(self, *, region: Optional[str], limit: int):
        self.region, self.limit = region, limit
        self.rows: List[Product] = []
        self.seen: set = set()
        self.total: Optional[int] = None
        self.has_next_page: Optional[bool] = None
        self.batches = 0
        self.rejected = 0
        self.ad_meta: dict = {}  # ad id -> what a --details request needs from the ad itself

    @property
    def full(self) -> bool:
        return len(self.rows) >= self.limit

    def add(self, batches: List[Batch], *, page: int, scraped_at: Optional[str] = None) -> int:
        """Add one page's batches; returns how many NEW rows they gave."""
        added = 0
        scraped_at = scraped_at or now_iso()
        for batch in batches:
            self.batches += 1
            if batch.total is not None:
                self.total = batch.total if self.total is None else max(self.total, batch.total)
            if batch.has_next_page is not None:
                self.has_next_page = batch.has_next_page
            for ad in batch.ads:
                if self.full:
                    return added
                ad_id = ad.get("ad_archive_id")
                if ad_id in self.seen:
                    continue
                try:
                    row = ad_row(ad, region=self.region, page=page, position=len(self.rows) + 1, scraped_at=scraped_at)
                except Exception as exc:  # noqa: BLE001 — one malformed ad must not end the run
                    log.warning("Ad %s could not be read (%s) — skipped and counted as rejected.", ad_id, exc)
                    row = None
                if row is None:
                    self.rejected += 1
                    continue
                self.seen.add(ad_id)
                self.rows.append(row)
                self.ad_meta[ad_id] = {"page_id": str(ad.get("page_id") or ""),
                                       "political": "POLITICAL" in (ad.get("categories") or []),
                                       "aaa_eligible": ad.get("is_aaa_eligible") if isinstance(ad.get("is_aaa_eligible"), bool) else None}
                added += 1
        return added
