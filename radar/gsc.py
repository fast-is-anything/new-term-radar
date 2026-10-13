"""Signal ④ (optional): queries your own sites got impressions for, for the first time.

Search Console, fresh data (about a day behind instead of three; the numbers are revised
later, which is fine for spotting). A query is new when it has impressions in the last
RECENT days and none in the BEFORE days ahead of them.

Set in config.json as a list, one entry per site (an empty list turns the signal off):

    "gsc": [
      {"site": "sc-domain:example.com"},
      {"site": "https://blog.example.com/", "token_file": "gsc_blog.json"}
    ]

`token_file` is a Google "authorized user" JSON holding client_id, client_secret and
refresh_token for the scope webmasters.readonly (what google-auth's
Credentials.to_json() writes; see the README). It is read from the config's directory,
or from the environment variable GSC_TOKEN_JSON when the file is missing (for CI). One
token covers every site the authorising account can read, so most setups need only one
file and can leave `token_file` out.

Every site is queried on its own and a site that fails never hides the others: its
reason is reported, the rest of the report is unaffected.

Queries that use search operators (quotes, site:, -word) are dropped: those are tools
probing the results, not people.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date, timedelta

from . import UA

LAG_DAYS = 1
RECENT, BEFORE = 3, 60
MIN_IMPRESSIONS = 5
TOP = 30
ROW_LIMIT = 25000
_OPERATOR = re.compile(r'"|\b(site|inurl|intitle|intext|filetype):|(^|\s)-\w|\*|\bOR\b')


def _token(cfg: dict, entry: dict, cache: dict | None = None) -> str:
    """Access token for one site's entry: its own token file, or GSC_TOKEN_JSON (for CI).
    `cache` is shared by the sites of one run, so a token several sites reuse is fetched once."""
    path = cfg["_dir"] / entry.get("token_file", "gsc_token.json")
    exists = path.exists()
    key = str(path) if exists else "__env__"
    if cache is not None and key in cache:
        return cache[key]
    raw = path.read_text() if exists else os.environ.get("GSC_TOKEN_JSON", "")
    if not raw:
        raise RuntimeError(f"no Search Console token: {path.name} or GSC_TOKEN_JSON")
    tok = json.loads(raw)
    body = urllib.parse.urlencode({"client_id": tok["client_id"], "client_secret": tok["client_secret"],
                                   "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request(tok.get("token_uri") or "https://oauth2.googleapis.com/token", data=body,
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as res:
        token = json.load(res)["access_token"]
    if cache is not None:
        cache[key] = token
    return token


def _first_site(cfg: dict) -> str:
    """The first configured site, for readers that need one property (positions, yours)."""
    entries = cfg.get("gsc") or []
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        return str(entries[0].get("site") or "").strip()
    return ""


def _query(access: str, site: str, start: date, end: date, dimensions: list[str], fresh: bool,
           filters: list[dict] | None = None) -> list[dict]:
    body = {"startDate": str(start), "endDate": str(end), "dimensions": dimensions, "rowLimit": ROW_LIMIT}
    if fresh:
        body["dataState"] = "all"
    if filters:
        body["dimensionFilterGroups"] = [{"filters": filters}]
    url = f"https://www.googleapis.com/webmasters/v3/sites/{urllib.parse.quote(site, safe='')}/searchAnalytics/query"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {access}", "Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as res:
        rows = json.load(res).get("rows", [])
    return [{**dict(zip(dimensions, r["keys"])), "impressions": int(r["impressions"]), "clicks": int(r["clicks"]),
             "position": round(float(r.get("position") or 0), 1)}
            for r in rows if not _OPERATOR.search(r["keys"][0])]


def _re2(text: str) -> str:
    """A literal for Search Console's RE2 filter (Python's re.escape also escapes spaces, which RE2 refuses)."""
    return re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", text)


def positions(cfg: dict, phrases: list[str], today: date, days: int = 28) -> dict[str, dict]:
    """{query: {"position", "impressions", "clicks", "page"}} for these exact queries over the last
    `days` days, all countries and devices, in one call. A query on several of your pages is
    read on the one with the most impressions; impressions and clicks are the query's total."""
    entries = cfg.get("gsc") or []
    site = _first_site(cfg)
    if not site:
        raise RuntimeError("positions needs at least one entry in gsc")
    access = _token(cfg, entries[0])
    end = today - timedelta(days=LAG_DAYS)
    expr = "^(" + "|".join(_re2(p.lower()) for p in phrases) + ")$"
    rows = _query(access, site, end - timedelta(days=days - 1), end, ["query", "page"], True,
                  [{"dimension": "query", "operator": "includingRegex", "expression": expr}])
    out: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: -r["impressions"]):
        q = out.setdefault(r["query"].lower(), {"position": r["position"], "page": r["page"], "impressions": 0, "clicks": 0})
        q["impressions"] += r["impressions"]
        q["clicks"] += r["clicks"]
    return out


def fresh_queries(recent: list[dict], before: list[dict]) -> list[dict]:
    """Queries in `recent` (query, page rows) that `before` never saw, summed per query."""
    seen = {r["query"] for r in before}
    fresh: dict[str, dict] = {}
    for r in recent:
        if r["query"] in seen:
            continue
        f = fresh.setdefault(r["query"], {"query": r["query"], "impressions": 0, "clicks": 0, "page": r["page"]})
        f["impressions"] += r["impressions"]
        f["clicks"] += r["clicks"]
    return sorted(fresh.values(), key=lambda f: -f["impressions"])


def _site(cfg: dict, entry: dict, site: str, start: date, end: date, cache: dict | None = None) -> dict:
    """One site's block: its new queries and the count before the impressions floor is applied."""
    access = _token(cfg, entry, cache)
    recent = _query(access, site, start, end, ["query", "page"], fresh=True)
    before = _query(access, site, start - timedelta(days=BEFORE), start - timedelta(days=1), ["query"], fresh=False)
    fresh = fresh_queries(recent, before)
    return {"site": site, "total_new": len(fresh),
            "queries": [f for f in fresh if f["impressions"] >= MIN_IMPRESSIONS][:TOP]}


def gsc_new(cfg: dict) -> dict:
    """Every site in cfg["gsc"], each on its own: one site failing never hides the others."""
    entries = cfg["gsc"]
    if not entries:
        return {"off": True}
    if not isinstance(entries, list):
        raise RuntimeError('gsc must be a list, e.g. [{"site": "sc-domain:example.com"}]')
    end = date.today() - timedelta(days=LAG_DAYS)
    start = end - timedelta(days=RECENT - 1)
    sites: list[dict] = []
    errors: list[str] = []
    cache: dict[str, str] = {}
    for entry in entries:
        site = str(entry.get("site") or "").strip() if isinstance(entry, dict) else ""
        if not site:
            # Keep it as a block so the report shows the reason instead of silently dropping it.
            msg = f'an entry needs a "site": {repr(entry)[:80]}'
            errors.append(msg)
            sites.append({"site": repr(entry)[:80], "total_new": 0, "queries": [], "error": msg})
            continue
        try:
            sites.append(_site(cfg, entry, site, start, end, cache))
        except Exception as exc:  # noqa: BLE001 - one site's failure must not hide the others
            msg = " ".join(str(exc).split())[:140]
            errors.append(f"{site}: {msg}")
            sites.append({"site": site, "total_new": 0, "queries": [], "error": msg})
    return {"window": f"{start}..{end}", "min_impressions": MIN_IMPRESSIONS,
            "total_new": sum(s["total_new"] for s in sites), "sites": sites,
            "queries": sorted((q for s in sites for q in s["queries"]), key=lambda q: -q["impressions"]),
            "error": "; ".join(errors)}
