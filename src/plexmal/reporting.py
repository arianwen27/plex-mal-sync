"""Human-readable plans: console table + markdown report. The report is the product."""
from __future__ import annotations

import json
import os
import time

from .util import truncate


def _col(s, n, left=True):
    s = "" if s is None else str(s)
    s = truncate(s, n)
    return s.ljust(n) if left else s.rjust(n)


def _line(item, widths):
    w = widths
    return ("  " + _col(item.get("mal_id"), w["id"]) + " " + _col(item.get("mal_title"), w["title"])
            + " " + _col(item.get("plex_show"), w["show"])
            + " " + _col("" if item.get("season") is None else "S%s" % item["season"], 4, False)
            + " " + _col(_watch(item), 13, False)
            + " " + _col(item.get("new_status") or "", 9)
            + " " + _col(("%.2f" % item["confidence"]) if item.get("confidence") else "", 5, False)
            + " " + _col(item.get("source") or "", 10))


def _watch(item):
    cur, new = item.get("cur_watched"), item.get("new_watched")
    tot = item.get("total_episodes")
    if item.get("action") == "new_entry":
        return f"(new)->{new}"
    if cur == new:
        return f"{cur}"
    return f"{cur}->{new}"


def console(plan, width=150):
    w = {"id": 7, "title": 38, "show": 34}
    hdr = ("  " + _col("MAL id", w["id"]) + " " + _col("MyAnimeList entry", w["title"])
           + " " + _col("Plex show", w["show"]) + " " + _col("seas", 4, False)
           + " " + _col("episodes", 13, False) + " " + _col("status", 9)
           + " " + _col("conf", 5, False) + " " + _col("match src", 10))
    out = []
    st = plan.get("stats", {})

    def section(title, items, note=""):
        if not items:
            return
        out.append("")
        out.append(f"{title}  ({len(items)}){note}")
        out.append(hdr)
        for it in items:
            out.append(_line(it, w))
            for fl in it.get("flags") or []:
                out.append("           ! " + fl)

    out.append("")
    out.append(f"WILL WRITE {st.get('writes', 0)}  "
               f"[new {st.get('new_entries', 0)} / bumps {st.get('bumps', 0)} / status {st.get('status_only', 0)}]"
               f"   review {st.get('review', 0)}   blocked {st.get('blocked', 0)}")
    out.append(f"plan hash: {plan.get('hash')}   (apply with:  apply --confirm {plan.get('hash')})")
    section("WRITES", plan.get("items", []))
    section("NEEDS REVIEW - nothing will be written for these", plan.get("review", []))
    section("BLOCKED BY SAFETY RAILS", plan.get("blocked", []))
    if plan.get("noop"):
        section("ALREADY IN SYNC", plan.get("noop", []))
    if plan.get("skipped"):
        out.append("")
        out.append(f"SKIPPED  ({len(plan['skipped'])})")
        for s in plan["skipped"]:
            out.append(f"  - {_col(s.get('plex_show'), w['show'])} {s.get('why')}")
    return "\n".join(out)


def markdown(plan):
    L = []
    st = plan.get("stats", {})
    L.append("# plex-mal-sync plan")
    L.append("")
    L.append(f"Generated {time.strftime('%Y-%m-%d %H:%M:%S')}  ·  plan hash `{plan.get('hash')}`  "
             f"·  MAL read from `{plan.get('mal_source')}`")
    L.append("")
    L.append(f"**{st.get('writes', 0)} writes** ({st.get('new_entries', 0)} new entries, "
             f"{st.get('bumps', 0)} episode bumps, {st.get('status_only', 0)} status only) · "
             f"**{st.get('review', 0)} need review** · **{st.get('blocked', 0)} blocked**")
    L.append("")
    L.append("Nothing here has been written yet. Counts can only ever go up; entries Plex has "
             "never seen are left alone; score/dates/notes are never touched.")

    def tbl(title, items, cols_head=("MAL id", "MyAnimeList entry", "Plex show", "Seas", "MAL now",
                                      "Will set", "Status", "Conf", "Src")):
        if not items:
            return
        L.append("")
        L.append(f"## {title} ({len(items)})")
        L.append("")
        L.append("| " + " | ".join(cols_head) + " |")
        L.append("|" + "---|" * len(cols_head))
        for it in items:
            cur = it.get("cur_watched")
            L.append("| {} | {} | {} | {} | {} | **{}** | {} | {} | {} |".format(
                it.get("mal_id") or "-",
                truncate(it.get("mal_title"), 52) or "-",
                truncate(it.get("plex_show"), 52) or "-",
                "-" if it.get("season") is None else "S%s" % it["season"],
                "-" if cur is None else cur,
                it.get("new_watched"),
                it.get("new_status") or "-",
                ("%.2f" % it["confidence"]) if it.get("confidence") else "-",
                it.get("source") or "-"))
            for fl in it.get("flags") or []:
                L.append(f"| | ↳ {fl} | | | | | | | |")

    tbl("Writes", plan.get("items", []))
    tbl("Needs review (not written)", plan.get("review", []))
    tbl("Blocked by safety rails", plan.get("blocked", []))
    if plan.get("noop"):
        tbl("Already in sync", plan.get("noop", []))
    if plan.get("skipped"):
        L.append("")
        L.append(f"## Skipped ({len(plan['skipped'])})")
        for s in plan["skipped"]:
            L.append(f"- {s.get('plex_show')} — {s.get('why')}")
    L.append("")
    L.append("### Episodes still in progress (deliberately not counted)")
    L.append("")
    rows = [(i["plex_show"], x) for i in (plan.get("items", []) + plan.get("review", []))
            for x in (i.get("in_progress") or [])]
    if rows:
        L.append("| Plex show | Episode | % watched |")
        L.append("|---|---|---|")
        for show, x in rows[:80]:
            L.append(f"| {truncate(show, 50)} | S{x['season']}E{x['episode']} {truncate(x['title'], 34)} | {x['pct']}% |")
    else:
        L.append("_none_")
    return "\n".join(L)


def write_reports(cfg, plan):
    ts_ = time.strftime("%Y%m%d_%H%M%S")
    md = os.path.join(cfg.reports_dir, f"plan_{ts_}_{plan.get('hash')}.md")
    js = os.path.join(cfg.reports_dir, f"plan_{ts_}_{plan.get('hash')}.json")
    slim = {k: v for k, v in plan.items() if k != "noop"}
    with open(md, "w", encoding="utf-8") as fh:
        fh.write(markdown(plan))
    with open(js, "w", encoding="utf-8") as fh:
        json.dump(slim, fh, indent=2, ensure_ascii=False, default=str)
    return md, js
