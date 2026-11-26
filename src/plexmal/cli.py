"""Command line interface."""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time

from . import audit, engine, malapi, reporting, selftest
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

    p = sub.add_parser("status", help="what is configured and reachable")
    p.add_argument("--json", action="store_true",
                   help="one JSON object on stdout (no secrets in it); diagnostics on stderr")

    p = sub.add_parser("sections", help="list Plex libraries and accounts")
    p.add_argument("--json", action="store_true", help="one JSON object on stdout")
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
    p.add_argument("--json", action="store_true",
                   help="the plan itself as one JSON object on stdout (the same object that is "
                        "saved to data/reports/); the human table is not printed")

    p = sub.add_parser("apply", help="write a plan you have read (requires its hash)")
    p.add_argument("--confirm", default="", help="the plan hash from the report")
    p.add_argument("--force", action="store_true", help="apply without the hash check")
    p.add_argument("--mal-export", dest="mal_export")
    p.add_argument("--max-calls", type=int, default=0, help="stop after N API writes")
    p.add_argument("--no-resolve", action="store_true",
                   help="build the plan exactly as `plan --no-resolve` did (no matcher calls). "
                        "Use this with the hash you actually read.")
    p.add_argument("--skip-audit", action="store_true",
                   help="do NOT re-check every write against MAL's own record first. Avoid: "
                        "this is what stops a write aimed at the wrong entry.")

    p = sub.add_parser("audit", help="re-check a saved plan (and an apply afterwards) against "
                                     "MAL's own records. Writes nothing.")
    p.add_argument("--plan", default="", help="plan JSON (default: the newest saved plan)")
    p.add_argument("--backup", default="", metavar="PATH",
                   help="also diff a pre-apply backup ('auto' = newest) against the live list")
    p.add_argument("--json", action="store_true",
                   help="findings and the list diff as one JSON object on stdout. Exit code is "
                        "still 1 when something is blocked.")

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

    p = sub.add_parser("selftest", help="check the safety rails (season guard, audit refusals). "
                                      "Writes nothing, needs no Plex or MAL login.")
    p.add_argument("--online", action="store_true",
                   help="also re-check the audit rules against the real MAL API (read-only)")
    p.add_argument("--json", action="store_true",
                   help="one JSON object on stdout: {ok, checks, failures[]}. The pass/fail "
                        "listing goes to stderr.")

    sub.add_parser("reports", help="list saved reports")

    a = ap.parse_args(argv)
    cfg = Cfg(a.config)
    log = Log(os.path.join(cfg.logs_dir, "run.log"), quiet=a.quiet)
    store = Store(cfg.cache_db)

    try:
        return {"status": cmd_status, "sections": cmd_sections, "scan": cmd_scan,
                "resolve": cmd_resolve, "plan": cmd_plan, "apply": cmd_apply, "audit": cmd_audit,
                "undo": cmd_undo, "selftest": cmd_selftest,
                "login": cmd_login, "watch": cmd_watch, "reports": cmd_reports}[a.cmd](a, cfg, store, log)
    except PlexError as e:
        log.error(str(e))
        return 2
    except KeyboardInterrupt:
        log.warn("interrupted")
        return 130


# ------------------------------------------------------------------ commands

def _jout(payload):
    """One JSON object on stdout. Diagnostics live on stderr (see util.Log)."""
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


def _want_json(a):
    return bool(getattr(a, "json", False))


def _err(a, msg, code=2):
    """A --json caller must get a parseable stdout, so the reason goes to stderr and an
    ok:false object to stdout. Non-zero exit either way - that is the real signal."""
    print(msg, file=sys.stderr if _want_json(a) else sys.stdout)
    if _want_json(a):
        _jout({"ok": False, "command": a.cmd, "error": msg})
    return code


SAFETY_KEYS = ("never_decrease", "min_confidence", "max_new_entries_per_run",
               "max_episode_bump", "max_writes_per_run", "status_policy", "skip_adult")


def _status_data(cfg, store):
    """Health of the whole setup. Carries no secret VALUES, only set/unset + expiry."""
    tok = Plex(cfg, Log(None, quiet=True)).token
    m = cfg.mal
    toks = {}
    if os.path.exists(cfg.tokens):
        try:
            toks = json.load(open(cfg.tokens, encoding="utf-8"))
        except (OSError, ValueError):
            toks = {"unreadable": True}
    return {
        "ok": True, "command": "status", "config_path": cfg.path,
        "plex": {"db_path": cfg.plex.get("db_path"),
                 "db_exists": os.path.exists(cfg.plex.get("db_path") or ""),
                 "url": cfg.plex.get("url"), "libraries": cfg.plex.get("libraries") or [],
                 "accounts": cfg.plex.get("accounts") or [],
                 "api_token_found": bool(tok),
                 "token_source": "config.toml" if cfg.plex.get("token") else "auto-detected"},
        "mal": {"client_id_set": bool(m.get("client_id")),
                "client_secret_set": bool(m.get("client_secret")),
                "token_saved": bool(toks), "authorised": bool(toks.get("access_token")),
                "expires_at": toks.get("expires_at"),
                "expires_human": ts(toks.get("expires_at")) if toks.get("expires_at") else None},
        "export_path": m.get("export_path") or None,
        "cache": {"path": cfg.cache_db, "episode_rows": store.episode_count(),
                  "last_apply_run": store.get_meta("last_apply_run") or None},
        "safety": {k: cfg.sync.get(k) for k in SAFETY_KEYS},
        "paths": {"data": cfg.data_dir, "reports": cfg.reports_dir, "backups": cfg.backups_dir,
                  "logs": cfg.logs_dir, "overrides": cfg.overrides_path, "tokens": cfg.tokens},
    }


def cmd_status(a, cfg, store, log):
    if _want_json(a):
        _jout(_status_data(cfg, store))
        return 0
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
    secs = px.sections()
    from .sources import connect_db
    con, cleanup = connect_db(cfg, log)
    try:
        names = sorted((r["name"] for r in con.execute("SELECT name FROM accounts WHERE name<>''")), key=str.lower)
    finally:
        con.close()
        if cleanup:
            import shutil
            shutil.rmtree(cleanup, ignore_errors=True)
    if _want_json(a):
        _jout({"ok": True, "command": "sections", "sections": secs, "account_count": len(names),
               "accounts": names,
               "note": "list the user's own Plex username(s) in [plex] accounts; everyone else "
                       "on the server is ignored"})
        return 0
    for s in secs:
        print(f"  #{s['key']:<4} {s['type']:<6} {s['title']}")
    print(f"\n  {len(names)} Plex accounts. Yours must be listed in [plex] accounts:")
    print("  " + ", ".join(names[:40]))
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
    if _want_json(a):
        # the same object that goes to data/reports/*.json, so either source is authoritative
        d = {k: v for k, v in plan.items() if k != "noop" or a.show_noop}
        d.update({"ok": True, "command": "plan", "report_md": md, "report_json": js,
                  "mal_entries": len(entries), "plex_show_facts": len(facts),
                  "next": (f"apply --confirm {plan['hash']}" if plan["items"]
                           else "nothing to write")})
        _jout(d)
        return 0
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
    md, js = reporting.write_reports(cfg, plan)
    if plan.get("hash") != a.confirm and not a.force:
        # the plan moved under the caller (new plays, or a mapping changed). The human table
        # goes to stderr; stdout still gets one JSON object so a caller never has to parse a
        # table to find out that nothing was written.
        print("The plan has changed since you read it (new episodes, or a mapping changed).",
              file=sys.stderr)
        print(f"New hash: {plan.get('hash')}  with {plan['stats']['writes']} writes", file=sys.stderr)
        print(reporting.console(plan), file=sys.stderr)
        print(f"\nRe-read it, then:  python plex_mal.py apply --confirm {plan.get('hash')}",
              file=sys.stderr)
        _jout({"ok": False, "command": "apply", "written": 0, "error": "hash_mismatch",
               "reason": "the plan changed after it was read; nothing was written",
               "asked_for": a.confirm, "new_hash": plan.get("hash"), "stats": plan["stats"],
               "plan_json": js, "report_md": md,
               "next": f"plan --no-resolve --json  (then apply --confirm {plan.get('hash')})"})
        return 3
    try:
        res = engine.apply_plan(cfg, store, log, plan, confirm=a.confirm or plan.get("hash"),
                                force=a.force, max_calls=a.max_calls or None,
                                skip_audit=a.skip_audit)
    except SystemExit as e:
        # apply_plan refuses: audit blockers, no token, or a hash mismatch it caught itself.
        # Nothing was written; say so in the same shape a caller can always expect.
        _jout({"ok": False, "command": "apply", "written": 0, "error": "refused",
               "reason": str(e), "plan_hash": plan.get("hash"), "stats": plan["stats"]})
        return 1
    res = dict(res)
    res.update({"ok": not res.get("failed"), "command": "apply", "plan_hash": plan.get("hash"),
                "report_md": md, "report_json": js})
    _jout(res)
    return 0 if not res.get("failed") else 1


def cmd_audit(a, cfg, store, log):
    """Re-check a saved plan against what MyAnimeList itself says about each entry.

    Read-only. Run it before `apply` if you want the plan's targets proven from MAL's
    side, and with --backup auto afterwards to see exactly what moved.
    """
    import glob
    path = a.plan or ""
    if not path:
        cands = sorted(glob.glob(os.path.join(cfg.reports_dir, "plan_*.json")))
        if not cands:
            return _err(a, "no saved plan found - run  python plex_mal.py plan  first")
        path = cands[-1]
    try:
        plan = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as e:
        return _err(a, "cannot read %s: %s" % (path, e))
    items = [i for i in plan.get("items") or []
             if i.get("action") in ("new_entry", "bump", "status_only")]
    m = malapi.MAL(cfg, log)
    if not m.ready():
        return _err(a, "not authorised with MyAnimeList - run:  python plex_mal.py login")
    live = m.my_list()
    findings = audit.check_writes(cfg, m, log, items, live=live)
    blocked = any(f[0] == "block" for f in findings)
    payload = {"ok": True, "command": "audit", "plan_file": path, "plan_hash": plan.get("hash"),
               "writes": len(items), "live_entries": len(live), "blocked": blocked,
               "findings": [{"severity": s, "mal_id": i, "message": g} for s, i, g in findings],
               "backup": None, "diff": None}

    bpath = a.backup or ""
    if bpath == "auto":
        cands = sorted(glob.glob(os.path.join(cfg.backups_dir, "mal_list_*.json")))
        if not cands:
            print("no backups found in", cfg.backups_dir,
                  file=sys.stderr if _want_json(a) else sys.stdout)
            bpath = ""
        else:
            bpath = cands[-1]
    if bpath:
        try:
            snap = json.load(open(bpath, encoding="utf-8"))
        except (OSError, ValueError) as e:
            return _err(a, "cannot read backup %s: %s" % (bpath, e))
        rows = snap.get("list") if isinstance(snap, dict) else snap
        before = {}
        for e in (rows.values() if isinstance(rows, dict) else rows or []):
            mid = e.get("mal_id") or e.get("id")
            if mid is not None and str(mid).isdigit():
                before[int(mid)] = e
        d = audit.diff(before, live)
        def _row(mid):
            b, l = before.get(mid) or {}, live.get(mid) or {}
            return {"mal_id": mid, "title": l.get("title") or b.get("title"),
                    "was_watched": b.get("watched"), "was_status": b.get("status"),
                    "watched": l.get("watched"), "status": l.get("status")}
        payload["backup"] = {"path": bpath, "taken_at": (snap.get("backup_at") if isinstance(snap, dict) else None),
                             "entries": len(before)}
        payload["diff"] = {"before": d["before"], "after": d["after"],
                           "untouched": d["untouched"],
                           "added": [_row(m) for m in d["added"]],
                           "changed": [_row(m) for m in d["changed"]],
                           "removed": [_row(m) for m in d["removed"]]}
        payload["unexplained"] = sorted(audit.moved_ids(d) - {i.get("mal_id") for i in items})
    if _want_json(a):
        _jout(payload)
        return 1 if blocked else 0
    print("plan    :", os.path.basename(path), "| hash", plan.get("hash"),
          "|", len(items), "write(s)")
    print("MAL list:", len(live), "entries")
    lines = audit.format_findings(findings)
    print("\n".join(lines) if lines else
          "\nclean: every write matches MAL's own record of that entry")
    if bpath:
        print("\nbackup  :", os.path.basename(bpath),
              "| taken", ts(snap.get("backup_at")) if isinstance(snap, dict) else "",
              "|", len(before), "entries")
        print("moved   : %d -> %d entries | +%d new, %d changed, %d removed, %d untouched" % (
              d["before"], d["after"], len(d["added"]), len(d["changed"]),
              len(d["removed"]), d["untouched"]))
        for mid in d["added"][:40]:
            print("   NEW     %-8s %-45s %s/%s" % (mid, str(live[mid].get("title"))[:45],
                                                  live[mid].get("watched"),
                                                  live[mid].get("status")))
        for mid in d["changed"][:40]:
            print("   CHANGED %-8s %-45s %s/%s -> %s/%s" % (
                mid, str(live[mid].get("title"))[:45], before[mid].get("watched"),
                before[mid].get("status"), live[mid].get("watched"),
                live[mid].get("status")))
        for mid in d["removed"][:40]:
            print("   REMOVED %-8s %-45s was %s/%s" % (
                mid, str(before[mid].get("title"))[:45], before[mid].get("watched"),
                before[mid].get("status")))
        if d["removed"]:
            print("\nWARNING: entries left your list that we never asked to remove.")
    return 1 if blocked else 0


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


def cmd_selftest(a, cfg, store, log):
    if _want_json(a):
        # selftest prints its own listing; keep stdout clean for the one JSON object
        import contextlib
        before = len(selftest.CHECKS)
        with contextlib.redirect_stdout(sys.stderr):
            fails = selftest.run(cfg, log, online=a.online)
        checks = selftest.CHECKS[before:]
        failures = selftest.FAILURES
        _jout({"ok": not fails, "command": "selftest", "online": bool(a.online),
               "checks": len(checks), "passed": len(checks) - len(failures),
               "failures": list(failures)})
        if fails:
            log.error(f"selftest: {fails} check(s) failed - do not apply anything until this is fixed")
        return 1 if fails else 0
    fails = selftest.run(cfg, log, online=a.online)
    if fails:
        log.error(f"selftest: {fails} check(s) failed - do not apply anything until this is fixed")
    return 1 if fails else 0


def cmd_reports(a, cfg, store, log):
    for p in sorted(glob.glob(os.path.join(cfg.reports_dir, "*")))[-15:]:
        print(f"  {ts(os.path.getmtime(p))}  {p}")
    return 0
