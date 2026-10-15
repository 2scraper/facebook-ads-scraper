#!/usr/bin/env python3
"""diff_runs.py — compare two complete Ad Library runs by SKU (the Library ID)
and a set of monitored ad fields.

Refuses to compare two runs that are not both `status == "complete"` in
their `.meta.json` sidecar: a partial run's un-fetched pages would otherwise
read as delisted products, which is a false signal worse than no diff at
all.

An ad has no price, so what is compared is the monitored ad fields below
(`field_changes`), plus which ads appeared or disappeared.

- Two runs must share their scope — the sidecar `url` (host, path and
  query: region, search term, filters) and the input selection. Otherwise
  the diff is refused.
- When either run was capped by `--max-results` (sidecar `capped`), a SKU
  missing from the new run is `left_selection` (it fell out of the top N),
  not `removed`.
- A sidecar carrying `output_sha256` must match the file beside it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List
from urllib.parse import parse_qsl, urlencode, urlparse


# `last_shown_at` / `days_shown` are not monitored by default: for an active
# ad they move every day by construction (the library reports today as the
# last day), so they would mark every row of every rerun as changed.
MONITOR_FIELDS = ("is_active", "versions", "body", "link_url", "cta_text", "display_format",
                  "spend", "impressions", "reach", "advertiser_name", "advertiser_likes")
EXTRA_FIELDS = ("last_shown_at", "days_shown", "platforms_json", "image_urls_json", "video_urls_json")
NUMERIC_FIELDS = {"versions", "advertiser_likes", "days_shown"}
BOOL_FIELDS = {"is_active"}


def field_value(row, field):
    value = row.get(field)
    if value is None or value == "":
        return None
    if field in NUMERIC_FIELDS and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    if field in BOOL_FIELDS and isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def _load_meta(run_path: str) -> dict:
    meta_path = Path(f"{run_path}.meta.json")
    if not meta_path.exists():
        raise SystemExit(f"error: no sidecar found at {meta_path} — was this run produced by finish_run()?")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _load_rows(run_path: str) -> List[dict]:
    path = Path(run_path)
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    if path.suffix == ".csv":
        import csv
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if _load_meta(run_path).get("csv_text_encoding") == "apostrophe-v1":
            rows = [{key: value[1:] if isinstance(value, str) and value.startswith("'") else value
                     for key, value in row.items()} for row in rows]
        return rows
    raise SystemExit(f"error: unsupported run file extension: {path.suffix}")


def _index_by_sku(rows: List[dict]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for row in rows:
        sku = row.get("sku")
        if sku:
            out[sku] = row
    return out


def normalize_scope(url: str) -> str:
    """host + path + sorted query — what makes two runs the same selection."""
    parts = urlparse(url or "")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return f"{parts.netloc.lower()}{parts.path.rstrip('/')}?{query}"


def diff(old_path: str, new_path: str, *, allow_different_scope: bool = False, fields=None) -> dict:
    fields = list(MONITOR_FIELDS if fields is None else fields)
    if any(name not in MONITOR_FIELDS + EXTRA_FIELDS for name in fields):
        raise ValueError("Unsupported monitor field; choose from " + ", ".join(MONITOR_FIELDS + EXTRA_FIELDS))
    old_meta, new_meta = _load_meta(old_path), _load_meta(new_path)
    for label, meta in (("old", old_meta), ("new", new_meta)):
        if meta.get("status") != "complete":
            raise SystemExit(
                f"error: refusing to diff — {label} run status is "
                f"{meta.get('status')!r}, not 'complete'. A partial run's "
                f"un-fetched pages would read as delisted products."
            )
    for label, path, meta in (("old", old_path, old_meta), ("new", new_path, new_meta)):
        expected = meta.get("output_sha256")
        if expected and hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise SystemExit(f"error: {label} run's sidecar does not describe {path} (output_sha256 mismatch).")
    if not allow_different_scope and normalize_scope(old_meta.get("url", "")) != normalize_scope(new_meta.get("url", "")):
        raise SystemExit(
            f"error: refusing to diff runs of different selections: {old_meta.get('url')!r} vs "
            f"{new_meta.get('url')!r}. Pass --allow-different-scope to compare them anyway."
        )
    if not allow_different_scope and old_meta.get("selection") != new_meta.get("selection"):
        raise SystemExit("error: refusing to diff different input selections (or legacy metadata without selection).")
    capped = bool(old_meta.get("capped") or new_meta.get("capped"))

    old_rows, new_rows = _index_by_sku(_load_rows(old_path)), _index_by_sku(_load_rows(new_path))
    old_skus, new_skus = set(old_rows), set(new_rows)

    added = sorted(new_skus - old_skus)
    missing = sorted(old_skus - new_skus)
    # In a capped run (more results than --max-results) a missing ad may only
    # have fallen out of the window: the library re-ranks a search between
    # visits. Only an uncapped run's missing ad is really gone from the search.
    removed = [] if capped else missing
    left_selection = missing if capped else []
    field_changes = []

    for sku in sorted(old_skus & new_skus):
        o, n = old_rows[sku], new_rows[sku]
        changes = {}
        for name in fields:
            before, after = field_value(o, name), field_value(n, name)
            if before != after:
                changes[name] = {"old": before, "new": after}
                if name in NUMERIC_FIELDS and isinstance(before, int) and isinstance(after, int):
                    changes[name]["delta"] = after - before
        if changes:
            field_changes.append({"sku": sku, "fields": changes})

    return {
        "added": added, "removed": removed, "left_selection": left_selection,
        "field_changes": field_changes,
        "old_count": len(old_rows), "new_count": len(new_rows), "capped": capped,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Diff two facebook-ads-scraper runs by sku (Library ID)")
    p.add_argument("old_run")
    p.add_argument("new_run")
    p.add_argument("--fail-on-change", action="store_true",
                    help="Exit 1 when monitored fields or selection membership change")
    p.add_argument("--json", action="store_true", help="Print the diff as JSON instead of a summary")
    p.add_argument("--allow-different-scope", action="store_true",
                    help="Compare runs whose sidecar URLs (region, query, filters) differ")
    p.add_argument("--fields", help="Comma-separated fields to monitor; default: " + ",".join(MONITOR_FIELDS)
                   + "; also available: " + ",".join(EXTRA_FIELDS))
    args = p.parse_args()
    fields = [name.strip() for name in args.fields.split(",") if name.strip()] if args.fields else None
    if fields and any(name not in MONITOR_FIELDS + EXTRA_FIELDS for name in fields):
        p.error("Unknown --fields value; choose from " + ",".join(MONITOR_FIELDS + EXTRA_FIELDS))

    result = diff(args.old_run, args.new_run, allow_different_scope=args.allow_different_scope, fields=fields)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{args.old_run} ({result['old_count']}) -> {args.new_run} ({result['new_count']})")
        print(f"  added:          {len(result['added'])}")
        print(f"  removed:        {len(result['removed'])}")
        if result["capped"]:
            print(f"  left_selection: {len(result['left_selection'])} (a capped top-N run: fell out of the selection, not delisted)")
        print(f"  monitored rows changed: {len(result['field_changes'])}")
        for entry in result["field_changes"]:
            print(f"    {entry['sku']}: " + json.dumps(entry["fields"], ensure_ascii=False))

    if args.fail_on_change and any(result[key] for key in ("field_changes", "added", "removed", "left_selection")):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
