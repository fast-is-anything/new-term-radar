"""Do you rank for it already? The difference between a new page and an update.

For the rows that would be acted on, this looks for your own sites (`yours.domains`, else the
domain of the Search Console property in `gsc.site`) in two places, neither bought here:

- the page-one check's ten results (radar/competition.py; US desktop by default): your
  position there and the page;
- Search Console, when `gsc` is set: your average position, impressions and clicks for the
  exact query over the last `days` days, all countries and devices, one call for all the rows.

The action follows: on page one by either = update that page; on pages two or three in Search
Console (an average of IMPROVE_MAX or better) = improve that page; otherwise a new page. A page
further down is still listed under the table, but a homepage at an average of 89 with one
impression (seen on 2026-10-20) is not the page to build on.
A row neither source read gets no action: not being looked for is not the same as not ranking.
Subdomains of your domains count as yours.
"""
from __future__ import annotations

import re
from datetime import date

from . import gsc

PAGE_ONE = 10
IMPROVE_MAX = 30


def site_domain(site: str) -> str:
    """sc-domain:example.com, https://www.example.com/ or example.com -> example.com"""
    s = re.sub(r"^sc-domain:", "", site.strip().lower())
    return re.sub(r"^www\.", "", re.sub(r"^https?://", "", s).split("/")[0])


def own_domains(cfg: dict) -> list[str]:
    listed = (cfg.get("yours") or {}).get("domains") or []
    site = gsc._first_site(cfg)
    return [site_domain(d) for d in (listed or ([site] if site else [])) if d]


def is_mine(domain: str, mine: list[str]) -> bool:
    d = re.sub(r"^www\.", "", (domain or "").lower())
    return any(d == m or d.endswith("." + m) for m in mine)


def on_page_one(row: dict, mine: list[str]) -> dict | None:
    hit = next((x for x in (row.get("competition") or {}).get("results") or [] if is_mine(x["domain"], mine)), None)
    return {"position": hit["position"], "url": hit["url"]} if hit else None


def action(serp: dict | None, console: dict | None) -> str:
    if serp or (console and console["position"] <= PAGE_ONE):
        return "update"
    return "improve" if console and console["impressions"] and console["position"] <= IMPROVE_MAX else "new"


def _console(rows: list[dict], cfg: dict, today: date) -> tuple[dict, bool, list[str]]:
    """(Search Console positions by query, whether they were read, errors)"""
    if not gsc._first_site(cfg):
        return {}, False, []
    try:
        return gsc.positions(cfg, [r["phrase"] for r in rows], today, cfg["yours"]["days"]), True, []
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        return {}, False, [f"Search Console positions: {exc}"]


def assess(rows: list[dict], cfg: dict, today: date) -> tuple[int, list[str]]:
    """Adds `yours` = {"serp", "console", "action"} to the rows either source read. (cost, errors)"""
    mine = own_domains(cfg)
    if not (cfg.get("yours") or {}).get("enabled") or not mine or not rows:
        return 0, []
    console, read, errors = _console(rows, cfg, today)
    for r in rows:
        if not r.get("competition") and not read:
            continue
        serp, cons = on_page_one(r, mine), console.get(r["phrase"].lower())
        r["yours"] = {"serp": serp, "console": cons, "action": action(serp, cons)}
    return 0, errors
