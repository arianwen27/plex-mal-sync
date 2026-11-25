"""Command line interface."""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time

from . import engine, malapi, reporting
from .config import Cfg
from .sources import Plex, PlexError
from .store import Store
from .util import Log, now, ts, tsn


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None):
    _utf8()
    ap = argparse.ArgumentParser(prog="plex_mal", description="Plex watch history -> MyAnimeList sync")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "config.toml"))
    ap.add_argument("-q", "--quiet", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="what is configured and reachable")
    sub.add_parser("sections", help="list Plex libraries and accounts")
    p = sub.add_parser("scan", help="journal Plex watch history into the local cache")
    p.add_argument("--force", action="store_true", help="re-read every show, ignoring the cache")

    p = sub.add_parser("resolve", help="map Plex shows to MAL entries (no MAL writes)")
    p.add_argument("--title", help="only this show")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--redo", action="store_true", help="re-resolve even if already mapped")

    p = sub.add_parser("plan", help="show exactly what WOULD be written (writes nothing)")
    p.add_argument("--mal-export", dest="mal_export", help="read the list from a MAL export file instead of the API")
    p.add_argument("--no-resolve", action="store_true", help="do not call title matchers (use cached mappings only)")
    p.add_argument("--show-noop", action="store_true", help="also list entries already in sync")
    p.add_argument("--force-scan", action="store_true")

    p = sub.add_parser("apply", help="write a plan you have read (requires its hash)")
    p.add_argument("--confirm", default="", help="the plan hash from the report")
    p.add_argument("--force", action="store_true", help="apply without the hash check")
    p.add_argument("--mal-export", dest="mal_export")
    p.add_argument("--max-calls", type=int, default=0, help="stop after N API writes")
    p.add_argument("--no-resolve", action="store_true",
                   help="build the plan exactly as `plan --no-resolve` did (no matcher calls). "
                        "Use this with the hash you actually read.")

    p = sub.add_parser("undo", help="restore values from the last apply")
    p.add_argument("--yes", action="store_true", help="actually do it (default is a dry run)")

    p = sub.add_parser("login", help="OAuth authorisation against MyAnimeList")
    p.add_argument("--manual", action="store_true", help="paste the redirect URL interactively")
    p.add_argument("--paste", default="", help="finish an in-progress flow: paste the redirect URL or code")
    p.add_argument("--url-only", action="store_true", help="just print the URL, do not listen for the callback")
    p.add_argument("--paste-file", default="", metavar="PATH",
                   help="finish an in-progress flow: read the redirect URL from a file (no shell quoting)")
    p.add_argument("--timeout", type=int, default=900, help="seconds to wait for the browser callback")
    p.add_argument("--fresh", action="store_true", help="throw away the authorisation in progress and start over")

    p = sub.add_parser("watch", help="loop: scan + plan (optionally apply) every N minutes")
    p.add_argument("--interval", type=int, default=15, help="minutes")
    p.add_argument("--apply", action="store_true", help="apply each plan automatically (caps still enforced)")
    p.add_argument("--once", action="store_true")

    sub.add_parser("reports", help="list saved reports")

    a = ap.parse_args(argv)
    cfg = Cfg(a.config)
    log = Log(os.path.join(cfg.logs_dir, "run.log"), quiet=a.quiet)
    store = Store(cfg.cache_db)

    try:
        return {"status": cmd_status, "sections": cmd_sections, "scan": cmd_scan,
                "resolve": cmd_resolve, "plan": cmd_plan, "apply": cmd_apply, "undo": cmd_undo,
                "login": cmd_login, "watch": cmd_watch, "reports": cmd_reports}[a.cmd](a, cfg, store, log)
    except PlexError as e:
        log.error(str(e))
        return 2
    except KeyboardInterrupt:
        log.warn("interrupted")
        return 130


# ------------------------------------------------------------------ commands

def cmd_status(a, cfg, store, log):
    print("config:", cfg.path)
    print("  plex db :", cfg.plex.get("db_path"), "exists" if os.path.exists(cfg.plex.get("db_path") or "") else "MISSING")
    print("  plex url:", cfg.plex.get("url"), "| libraries:", cfg.plex.get("libraries"),
          "| accounts:", cfg.plex.get("accounts"))
    tok, src = Plex(cfg, Log(None, quiet=True)).token, ""
    print("  plex api:", "reachable with token" if tok else "no token found")
    m = cfg.mal
    print("  mal     : client_id", "set" if m.get("client_id") else "NOT SET",
          "| secret", "set" if m.get("client_secret") else "-",
          "| token", "saved" if os.path.exists(cfg.tokens) else "not authorised")
    if os.path.exists(cfg.tokens):
        t = json.load(open(cfg.tokens, encoding="utf-8"))
        print("           expires:", ts(t.get("expires_at")))
    print("  export  :", m.get("export_path") or "-")
    print("  cache   :", cfg.cache_db, f"({store.episode_count()} episode rows)")
    last = store.get_meta("last_apply_run")
    print("  last apply:", last or "never")
    print("  safety  :", json.dumps({k: cfg.sync[k] for k in
                                     ("never_decrease", "min_confidence", "max_new_entries_per_run",
                                      "max_episode_bump", "max_writes_per_run", "status_policy", "skip_adult")}))
    return 0


def cmd_sections(a, cfg, store, log):
    px = Plex(cfg, log)
    for s in px.sections():
        print(f"  #{s['key']:<4} {s['type']:<6} {s['title']}")
    from .sources import connect_db
    con, cleanup = connect_db(cfg, log)
    try:
        names = sorted((r["name"] for r in con.execute("SELECT name FROM accounts WHERE name<>''")), key=str.lower)
        print(f"\n  {len(names)} Plex accounts. Yours must be listed in [plex] accounts:")
        print("  " + ", ".join(names[:40]))
    finally:
        con.close()
        if cleanup:
            import shutil
            shutil.rmtree(cleanup, ignore_errors=True)
    return 0


def cmd_scan(a, cfg, store, log):
    facts = engine.scan(cfg, store, log, force=a.force)
    watched = [f for f in facts if f["completed_total"] > 0]
    print(f"\n{'plex show':<46} {'eps':>6} {'total':>6}  source      last watched")
    for f in watched[:60]:
        print(f"{f['plex_title'][:44]:<46} {f['completed_total']:>6} {f['total_eps']:>6}  "
              f"{f['structure']:<10}  {ts(f['last_viewed_at'])}")
    if len(watched) > 60:
        print(f"  ... {len(watched) - 60} more")
    print(f"\ntotal: {sum(f['completed_total'] for f in watched)} completed episodes "
          f"across {len(watched)} shows")
    return 0


def cmd_resolve(a, cfg, store, log):
    facts = engine.scan(cfg, store, log)
    rl = __import__("plexmal.util", fromlist=["Ratelimit"]).Ratelimit(cfg.sync.get("resolve_gap_seconds", 1.0))
    from . import matchmaking
    matchmaking.load_cache(json.loads(store.get_meta("rescache", "{}") or "{}"))
    todo = []
    for f in facts:
        if f["completed_total"] <= 0:
            continue
        if a.title and a.title.lower() not in f["plex_title"].lower():
            continue
        units = sorted([u for u in f["units"] if u["completed"] > 0], key=lambda u: u["season"])
        seasons = [None] if len(units) <= 1 else [u["season"] for u in units]
        comp = {u["season"]: u["completed"] for u in units}
        for s in seasons:
            mk = engine.mkey(f["plex_title"], s)
            m = store.mapping(mk)
            if m and m.get("state") in ("auto", "manual") and not a.redo:
                continue
            todo.append((f, s, (f["completed_total"] if s is None else comp.get(s, 0)),
                         [u["season"] for u in units]))
    if a.limit:
        todo = todo[:a.limit]
    log.info(f"{len(todo)} show/season unit(s) to resolve")
    ok = bad = 0
    used = set()
    for f, season, watched, allseasons in todo:
        res = matchmaking.resolve_season(cfg, log, rl, f, season, watched, allseasons, sibling_ids=sorted(used))
        cand = res.get("cand") or {}
        mk = engine.mkey(f["plex_title"], season)
        store.set_mapping(mk, gp_guid=f["gp_guid"], plex_title=f["plex_title"], season=season,
                          mal_id=cand.get("mal_id"), anilist_id=cand.get("anilist_id"),
                          mal_title=(cand.get("titles") or [None])[0], source=cand.get("source"),
                          confidence=res.get("score"),
                          evidence=json.dumps({"reason": res.get("reason"), "adult": cand.get("adult"),
                                               "episodes": cand.get("episodes")}),
                          alternatives=json.dumps(res.get("alternatives") or []), state=res["state"])
        flag = "AUTO " if res["state"] == "auto" else "REVIEW"
        if res["state"] == "auto":
            ok += 1
            used.add(cand.get("mal_id"))
        else:
            bad += 1
        print(f"  [{flag}] {f['plex_title'][:38]:<40} {('S%s' % season) if season else 'ALL':<4} "
              f"{str(cand.get('mal_id') or '-'):<8} {(cand.get('titles') or ['-'])[0][:34]:<36} "
              f"{res.get('score', 0):.2f} {res.get('reason', '')[:38]}")
    store.set_meta("rescache", json.dumps(matchmaking.dump_cache()))
    print(f"\nauto-mapped {ok}, needs review {bad}")
    print("review file:", write_review(cfg, store))
    return 0


def write_review(cfg, store):
    rows = [m for m in store.mappings() if m["state"] in ("needs_review", None)]
    p = os.path.join(cfg.reports_dir, "mapping_review.md")
    L = ["# Show -> MyAnimeList mappings that need a human", "",
         "Fix these in `overrides.toml`, e.g.:", "", "```toml", '[mappings]',
         '"Show Title|S2" = 12345', "```", "",
         "| Plex show | Seas | best guess MAL id | best guess title | conf | why unsure | alternatives |",
         "|---|---|---|---|---|---|---|"]
    for m in rows:
        alts = []
        try:
            alts = json.loads(m.get("alternatives") or "[]")
        except Exception:
            pass
        why = ""
        try:
            why = (json.loads(m.get("evidence") or "{}") or {}).get("reason", "")
        except Exception:
            pass
        L.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            m["plex_title"], "-" if m["season"] is None else "S%s" % m["season"],
            m["mal_id"] or "-", (m["mal_title"] or "-")[:40],
            "%.2f" % (m["confidence"] or 0), why,
            "; ".join(f"{a.get('mal_id')} {str(a.get('title'))[:28]} ({a.get('score')})" for a in alts[:3])))
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    return p


def cmd_plan(a, cfg, store, log):
    facts = engine.scan(cfg, store, log, force=a.force_scan)
    # --no-resolve only skips the title matchers; the live MAL list is the comparison
    # baseline, so it must still be read here. Tying the two together made a perfectly
    # valid login look like "not authorised".
    entries, src = engine.load_mal_entries(cfg, store, log, export=a.mal_export, live=True)
    log.info(f"MAL list: {len(entries)} entries (from {src})")
    plan = engine.build_plan(cfg, store, log, facts, entries,
                            do_resolve=not a.no_resolve, show_noop=a.show_noop)
    plan["mal_source"] = src
    md, js = reporting.write_reports(cfg, plan)
    print(reporting.console(plan))
    print("\nreport:", md)
    print("json  :", js)
    if plan["items"]:
        print(f"\nNothing written. To write these:  python plex_mal.py apply --confirm {plan['hash']}")
    return 0


def cmd_apply(a, cfg, store, log):
    facts = engine.scan(cfg, store, log)
    entries, src = engine.load_mal_entries(cfg, store, log, export=a.mal_export)
    plan = engine.build_plan(cfg, store, log, facts, entries, do_resolve=not a.no_resolve)
    plan["mal_source"] = src
    if plan.get("hash") != a.confirm and not a.force:
        print("The plan has changed since you read it (new episodes, or a mapping changed).")
        print(f"New hash: {plan.get('hash')}  with {plan['stats']['writes']} writes")
        print(reporting.console(plan))
        print(f"\nRe-read it, then:  python plex_mal.py apply --confirm {plan.get('hash')}")
        return 3
    md, js = reporting.write_reports(cfg, plan)
    res = engine.apply_plan(cfg, store, log, plan, confirm=a.confirm or plan.get("hash"),
                            force=a.force, max_calls=a.max_calls or None)
    print(json.dumps(res, indent=2))
    return 0 if not res.get("failed") else 1


def cmd_undo(a, cfg, store, log):
    engine.undo_last(cfg, store, log, dry=not a.yes)
    if not a.yes:
        print("\n(dry run - add --yes to restore those values on MyAnimeList)")
    return 0


def cmd_login(a, cfg, store, log):
    m = malapi.MAL(cfg, log)
    pasted = a.paste or ""
    if getattr(a, "paste_file", ""):
        try:
            with open(a.paste_file, "r", encoding="utf-8", errors="replace") as fh:
                pasted = fh.read().strip()
        except OSError as e:
            print("cannot read %s: %s" % (a.paste_file, e))
            return 2
        if not pasted:
            print("%s is empty" % a.paste_file)
            return 2
        os.remove(a.paste_file)  # the URL holds a one-time code - don't leave it lying around
    if m.ready() and not (pasted or a.url_only):
        try:
            me = m.whoami()
            print("already authorised as:", (me or {}).get("name"), "| id", (me or {}).get("id"))
            print("(to re-authorise, delete data/mal_tokens.json and run login again)")
            return 0
        except Exception:
            log.warn("saved token does not work - running the browser flow again")
    got = m.login(manual=a.manual, paste=(pasted or None), url_only=a.url_only,
                  timeout=a.timeout, reuse=not a.fresh)
    if got is None:
        return 0
    try:
        me = m.whoami()
        print("authorised as:", (me or {}).get("name"), "| id", (me or {}).get("id"))
    except Exception as e:
        print("token saved, but whoami failed:", e)
    return 0


def cmd_watch(a, cfg, store, log):
    log.info(f"watch loop: every {a.interval} min (apply={a.apply})")
    while True:
        try:
            facts = engine.scan(cfg, store, log)
            entries, src = engine.load_mal_entries(cfg, store, log)
            plan = engine.build_plan(cfg, store, log, facts, entries, do_resolve=True)
            plan["mal_source"] = src
            if plan["items"]:
                md, js = reporting.write_reports(cfg, plan)
                log.info(f"{plan['stats']['writes']} change(s) -> {os.path.basename(md)}")
                if a.apply:
                    engine.apply_plan(cfg, store, log, plan, confirm=plan["hash"], force=True)
            else:
                log.info("up to date")
        except Exception as e:
            log.error(f"loop error: {type(e).__name__}: {e}")
        if a.once:
            return 0
        time.sleep(max(60, a.interval * 60))


def cmd_reports(a, cfg, store, log):
    for p in sorted(glob.glob(os.path.join(cfg.reports_dir, "*")))[-15:]:
        print(f"  {ts(os.path.getmtime(p))}  {p}")
    return 0
