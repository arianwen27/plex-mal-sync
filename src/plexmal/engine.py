"""The engine: scan Plex -> map to MAL -> build a plan -> apply it (carefully).

Counting rule (matches how the Plex UI behaves):
  an episode counts when Plex recorded a *completed* play, i.e. either
    * the library history table has a row for that exact episode (matched by plex guid), or
    * the Plex API reports viewCount >= 1 for that episode.
  An episode with only a resume offset is 'in progress' and never counts.
Both sources have blind spots on real libraries, so they are unioned, and each episode
remembers which source proved it so the report can show the evidence.
"""
from __future__ import annotations

import hashlib
import json
import time

from . import malapi, matchmaking
from .sources import Plex, PlexError
from .util import Ratelimit, norm_title, now, truncate, ts, tsn


def mkey(title, season=None):
    return norm_title(title) + ("|ALL" if season is None else "|S%d" % season)


def override_lookup(ov_map, show_title, season):
    cands = [show_title, norm_title(show_title)]
    if season is None:
        # "|ALL" was documented for shows MAL keeps in a single entry, but was never
        # actually tried for season-less units, so such an override was ignored. It is
        # deliberately NOT tried for a numbered season: that would collapse every season
        # of a multi-season show onto the same entry.
        cands += [f"{show_title}|ALL", f"{norm_title(show_title)}|ALL"]
    else:
        cands += [f"{show_title}|S{season}", f"{norm_title(show_title)}|S{season}"]
    for c in cands:
        for k, v in (ov_map or {}).items():
            if str(k).strip().lower() == str(c).strip().lower():
                try:
                    return int(v)
                except Exception:
                    return None
    return None


def _ignored(f, ov):
    want = {norm_title(x) for x in (ov.get("ignore") or [])}
    return norm_title(f["plex_title"]) in want


# ------------------------------------------------------------------ scan

def scan(cfg, store, log, force=False):
    """Read Plex (history + API), journal it, and return per-show watch facts."""
    libs = cfg.plex.get("libraries") or []
    accts = cfg.plex.get("accounts") or []
    if not libs:
        raise PlexError("set [plex] libraries in config.toml (e.g. libraries = ['Anime'])")
    if not accts:
        raise PlexError("set [plex] accounts in config.toml - whose watches count as yours")

    px = Plex(cfg, log)
    secs = px.resolve_sections()
    sec_ids = [int(s["key"]) for s in secs]

    hist, show_meta, accmap = px.db_history(sec_ids, accts)
    log.info(f"history table: {len(hist)} completed-play rows "
             f"({len({h['guid'] for h in hist})} distinct episodes) for "
             f"{', '.join(sorted(accmap))}")
    if not hist:
        log.warn("history table returned nothing for those accounts/libraries - "
                 "double-check [plex] accounts / libraries before writing anything")

    for r in hist:
        store.record_episode(r["guid"], r["gp_guid"], r["section_id"], r["account_id"], r["account_name"],
                             r["season"], r["episode"], r["ep_title"], r["mtype"],
                             r["viewed_at"], r["view_type"])
    store.commit()

    hist_by_gp = {}
    for r in hist:
        hist_by_gp.setdefault(r["gp_guid"], []).append(r)

    cache = json.loads(store.get_meta("facts", "{}") or "{}")
    facts, api_seen = {}, set()
    for s in secs:
        entries = px.show_entries(s["key"])
        log.info(f"'{s['title']}': {len(entries)} show entries in Plex")
        for ent in entries:
            gp = ent.get("guid") or f"rk:{ent.get('ratingKey')}"
            api_seen.add(gp)
            last = ent.get("lastViewedAt") or 0
            cached = cache.get(gp)
            reuse = (cached and not force and cached.get("last_viewed_at") == last
                     and cached.get("api_leaf") == ent.get("leafCount"))
            if reuse:
                facts[gp] = cached
                continue
            if not px.token:
                # no API: history-only facts
                facts[gp] = _fact_from_history(gp, ent, s, hist_by_gp.get(gp, []), show_meta.get(gp))
                cache[gp] = facts[gp]
                continue
            eps, complete = px.episodes_of_show(s["key"], ent)
            facts[gp] = _fact_from_api(gp, ent, s, eps, complete, hist_by_gp.get(gp, []), show_meta.get(gp))
            cache[gp] = facts[gp]
    # shows known only from history (removed from the library, or hidden from the API)
    for gp, rows in hist_by_gp.items():
        if gp in facts:
            continue
        sm = show_meta.get(gp) or {}
        facts[gp] = _fact_from_history(gp, {"guid": gp, "title": sm.get("plex_title") or gp,
                                            "ratingKey": None, "leafCount": 0, "lastViewedAt": 0},
                                       {"key": str(rows[0]["section_id"]), "title": "?"},
                                       rows, sm, orphan=True)
    store.set_meta("facts", json.dumps(cache))
    out = sorted(facts.values(), key=lambda f: -(f.get("last_viewed_at") or 0))
    watched = [f for f in out if f["completed_total"] > 0]
    log.info(f"scan: {len(out)} shows known, {len(watched)} with completed episodes "
             f"({sum(f['completed_total'] for f in watched)} episodes total)")
    return out


def _units_and_counts(units, api_eps, hrows):
    by_key = {}

    def U(season, stitle):
        season = season if season is not None else 1
        k = (season, stitle or "")
        if k not in units:
            units[k] = {"season": season, "season_title": stitle, "total": 0, "completed": 0,
                        "in_progress": [], "ev_api": 0, "ev_db": 0, "ev_both": 0, "api_extra": 0}
        return units[k]

    for e in api_eps:
        u = U(e.get("season"), e.get("season_title"))
        u["total"] += 1
        key = e.get("guid") or f"{e.get('season')}:{e.get('episode')}"
        by_key[key] = (u, e)
        if (e.get("view_count") or 0) >= 1:
            u["completed"] += 1
            u["ev_api"] += 1
        elif e.get("view_offset"):
            u["in_progress"].append({"season": e.get("season"), "episode": e.get("episode"),
                                     "title": truncate(e.get("ep_title"), 60), "pct": round(e.get("pct") or 0)})

    db_extra = 0
    for h in hrows:
        key = h.get("guid") or f"{h['season']}:{h['episode']}"
        if key in by_key:
            u, e = by_key[key]
            if (e.get("view_count") or 0) >= 1:
                u["ev_both"] += 1
            else:
                u["completed"] += 1
                u["ev_db"] += 1
                u["in_progress"] = [x for x in u["in_progress"]
                                    if not (x["season"] == e.get("season") and x["episode"] == e.get("episode"))]
        else:
            u = U(h.get("season"), None)
            u["total"] += 1
            u["completed"] += 1
            u["ev_db"] += 1
            db_extra += 1
    return units, db_extra


def _base_fact(gp, ent, sec, smeta):
    return {"gp_guid": gp, "rk": ent.get("ratingKey"), "section_key": str(sec.get("key")),
            "section_title": sec.get("title"), "plex_title": ent.get("title") or gp,
            "original_title": (smeta or {}).get("original_title"),
            "year": (smeta or {}).get("year"), "total_eps": 0, "completed_total": 0,
            "units": [], "in_progress": [], "complete_enum": True, "structure": "api",
            "last_viewed_at": ent.get("lastViewedAt") or 0, "api_view": ent.get("viewCount"),
            "api_leaf": ent.get("leafCount"), "db_rows": 0, "scanned_at": now(),
            "in_library": True}


def _fact_from_api(gp, ent, sec, eps, complete, hrows, smeta):
    f = _base_fact(gp, ent, sec, smeta)
    units, db_extra = _units_and_counts({}, eps, hrows)
    f["units"] = sorted(units.values(), key=lambda u: (u["season"], str(u.get("season_title"))))
    f["in_progress"] = [x for u in f["units"] for x in u["in_progress"]]
    f["total_eps"] = int(ent.get("leafCount") or 0) or sum(u["total"] for u in f["units"])
    f["completed_total"] = sum(u["completed"] for u in f["units"])
    f["complete_enum"] = bool(complete)
    f["structure"] = "api" if (complete and not db_extra) else ("api+db" if complete else "db(partial_api)")
    f["db_rows"] = len(hrows)
    f["last_viewed_at"] = max([f["last_viewed_at"]] + [h.get("viewed_at") or 0 for h in hrows])
    f["db_only_eps"] = db_extra
    return f


def _fact_from_history(gp, ent, sec, hrows, smeta, orphan=False):
    f = _base_fact(gp, ent, sec, smeta)
    f["structure"] = "db_only"
    f["complete_enum"] = False
    f["in_library"] = not orphan
    units, _ = _units_and_counts({}, [], hrows)
    f["units"] = sorted(units.values(), key=lambda u: (u["season"], str(u.get("season_title"))))
    f["total_eps"] = int(ent.get("leafCount") or 0) or sum(u["total"] for u in f["units"])
    f["completed_total"] = sum(u["completed"] for u in f["units"])
    f["db_rows"] = len(hrows)
    f["last_viewed_at"] = max([h.get("viewed_at") or 0 for h in hrows] or [0])
    return f


# -------------------------------------------------------------- plan

def load_mal_entries(cfg, store, log, export=None, live=True):
    """Current state of the user's MAL list: {mal_id: entry} + a source label."""
    if export:
        return malapi.load_export(export, log), f"export:{export}"
    m = malapi.MAL(cfg, log)
    if live and m.ready():
        return m.my_list(), "mal_api"
    if cfg.mal.get("export_path"):
        return malapi.load_export(cfg.mal["export_path"], log), "export:" + cfg.mal["export_path"]
    raise PlexError("cannot read your MAL list: not authorised and no [mal] export_path set.\n"
                    "  either:  python plex_mal.py login\n"
                    "  or:      python plex_mal.py plan --mal-export <path to MAL export .zip/.json/.csv>")


def build_plan(cfg, store, log, facts, mal_entries, do_resolve=True, show_noop=False):
    S = cfg.sync
    ov = cfg.overrides()
    rl = Ratelimit(S.get("resolve_gap_seconds", 1.0))
    matchmaking.load_cache(json.loads(store.get_meta("rescache", "{}") or "{}"))
    mal_client = [None]

    def canonical(mid):
        """MAL's own record of an entry. Used for the episode count, which decides both
        'completed vs watching' and the season-grouping cap, so it must not come from a
        title matcher. Cached in the local store to keep API calls down."""
        info = store.mal_cache(mid)
        if info and info.get("num_episodes"):
            return info
        if mal_client[0] is None:
            probe = malapi.MAL(cfg, log)
            mal_client[0] = probe if probe.ready() else False
        if mal_client[0] is False:
            return None
        try:
            j = mal_client[0].anime(mid) or {}
        except Exception as e:
            log.warn(f"MAL lookup for {mid} failed: {e}")
            return None
        if j:
            store.cache_mal(mid, j.get("title"), j.get("num_episodes"), j.get("status"),
                            None, None, bool(j.get("nsfw")), json.dumps(j)[:400])
        return j or None
    plan = {"created": now(), "items": [], "review": [], "blocked": [], "noop": [],
            "skipped": [], "mal_source": None, "stats": {}}

    for f in facts:
        if f["completed_total"] <= 0:
            plan["skipped"].append({"plex_show": f["plex_title"], "why": "no completed episodes in Plex"})
            continue
        if _ignored(f, ov):
            plan["skipped"].append({"plex_show": f["plex_title"], "why": "in overrides [ignore]"})
            continue
        if not f.get("in_library", True):
            plan["skipped"].append({"plex_show": f["plex_title"],
                                    "why": "watch history exists but the show is no longer in the Plex library"})
            continue

        units = [u for u in f["units"] if u["completed"] > 0]
        units.sort(key=lambda u: u["season"])
        single_named = norm_title(f["plex_title"]) in {norm_title(x) for x in (ov.get("single_entry") or [])}
        if single_named or len(units) <= 1 or not S.get("season_chains", True):
            todo = [(None, f["completed_total"], f["total_eps"])]
        else:
            todo = [(u["season"], u["completed"], u["total"]) for u in units]

        used_ids = set()
        claimed = {}          # mal_id -> the item that is writing to it this run
        for season, watched, unit_total in todo:
            mk = mkey(f["plex_title"], season)
            item = {"mkey": mk, "gp_guid": f["gp_guid"], "plex_show": f["plex_title"],
                    "plex_year": f.get("year"), "season": season, "plex_watched": watched,
                    "plex_unit_total": unit_total, "structure": f.get("structure"),
                    "in_progress": [x for x in f.get("in_progress", [])
                                    if season is None or x["season"] == season],
                    "flags": [], "confidence": 0.0, "source": None, "mal_id": None,
                    "mal_title": None, "total_episodes": None, "cur_watched": None,
                    "cur_status": None, "action": None, "new_watched": None, "new_status": None}

            # ---- 1. manual override wins
            mid = override_lookup(ov.get("mappings"), f["plex_title"], season)
            if mid:
                item["mal_id"], item["source"], item["confidence"] = mid, "override", 1.0
            else:
                # ---- 2. previously resolved mapping
                st = store.mapping(mk)
                if st and st.get("mal_id") and st.get("state") in ("auto", "manual"):
                    item.update(mal_id=st["mal_id"], mal_title=st["mal_title"], source=st["source"],
                                confidence=st["confidence"] or 0.0)
                elif st and st.get("state") == "ignored":
                    plan["skipped"].append({"plex_show": f["plex_title"], "why": "mapping marked ignored"})
                    continue

            # ---- 3. resolve now
            if not item["mal_id"] and do_resolve:
                res = matchmaking.resolve_season(cfg, log, rl, f, season, watched,
                                                 [u["season"] for u in units], sibling_ids=sorted(used_ids))
                cand = res.get("cand") or {}
                store.set_mapping(mk, gp_guid=f["gp_guid"], plex_title=f["plex_title"], season=season,
                                  mal_id=cand.get("mal_id"), anilist_id=cand.get("anilist_id"),
                                  mal_title=(cand.get("titles") or [None])[0],
                                  source=cand.get("source"), confidence=res.get("score"),
                                  evidence=json.dumps({"reason": res.get("reason"), "why": cand.get("why"),
                                                       "adult": cand.get("adult"), "episodes": cand.get("episodes")}),
                                  alternatives=json.dumps(res.get("alternatives") or []),
                                  state=res["state"])
                if res["state"] == "auto" and cand.get("mal_id"):
                    item.update(mal_id=cand["mal_id"], mal_title=(cand.get("titles") or [None])[0],
                                source=cand.get("source"), confidence=res.get("score", 0.0))
                else:
                    item["confidence"] = res.get("score", 0.0)
                    item["mal_title"] = (cand.get("titles") or [None])[0]
                    alts = res.get("alternatives") or []
                    why = res.get("reason", "unresolved")
                    if cand.get("mal_id"):
                        why += f" | best guess {cand['mal_id']} {truncate(item['mal_title'], 40)}"
                    if alts:
                        why += " | also: " + "; ".join(
                            f"{a['mal_id']} {truncate(a['title'], 30)} ({a['score']:.2f})" for a in alts[:3])
                    item["flags"].append(why)
                    plan["review"].append(item)
                    continue

            if not item["mal_id"]:
                item["flags"].append("no MAL id known (run `resolve` or add an override)")
                plan["review"].append(item)
                continue
            used_ids.add(item["mal_id"])

            # ---- 4. compare with MAL
            entry = mal_entries.get(item["mal_id"])
            if entry:
                item["mal_title"] = item["mal_title"] or entry.get("title")
                item["total_episodes"] = int(entry.get("total_episodes") or 0) or None
                if entry.get("watched") is None or not entry.get("watched_known", True):
                    item["flags"].append("MAL did not report a watched-episode count for this entry. "
                                         "Guessing 0 here is what rewrites whole lists, so it is blocked.")
                    plan["blocked"].append(item)
                    continue
                item["cur_watched"] = int(entry.get("watched") or 0)
                item["cur_status"] = entry.get("status")
            else:
                info = store.mal_cache(item["mal_id"])
                if info:
                    item["mal_title"] = item["mal_title"] or info.get("title")
                    item["total_episodes"] = info.get("num_episodes")

            if not item["total_episodes"]:
                # new entries have no cap yet; without one a finished series would be
                # filed as "watching" and the grouping check could not run
                c = canonical(item["mal_id"])
                if c:
                    item["mal_title"] = item["mal_title"] or c.get("title")
                    item["total_episodes"] = c.get("num_episodes") or None
                    item["mal_adult"] = bool(c.get("nsfw"))

            target = watched
            cap = item["total_episodes"] or 0
            if cap and target > cap:
                item["flags"].append(f"Plex counts {target} episodes but this MAL entry has only {cap} "
                                     f"- season grouping differs. Add a [mappings] override, or set "
                                     f"[single_entry] if MAL tracks this show as one entry.")
                plan["blocked"].append(item)
                continue
            cur = item["cur_watched"] or 0
            if target < cur:
                item["flags"].append(f"Plex shows {target}, MAL already has {cur} - keeping {cur} (never decrease)")
                target = cur
            item["new_watched"] = target

            # ---- 5. status
            ns = None
            if not entry:
                if not S.get("allow_new_entries", True):
                    item["flags"].append("MAL entry does not exist and [sync] allow_new_entries=false")
                    plan["blocked"].append(item)
                    continue
                full = bool(cap and target >= cap and S.get("mark_completed_at_full", True))
                ns = "completed" if full else (S.get("new_entry_status") or "watching")
            elif S.get("mark_completed_at_full", True) and cap and target >= cap \
                    and item["cur_status"] != "completed" \
                    and item["cur_status"] not in ("dropped",):
                ns = "completed"
            elif item["cur_status"] == "plan_to_watch" and target > 0 \
                    and S.get("promote_plan_to_watch", True):
                # you can only have watched episodes of something you have started
                ns = "watching"
                item["flags"].append("was Plan to Watch -> Currently Watching (Plex has watched episodes)")
            if ns == "watching" and (item["cur_status"] or "") in ("on_hold", "dropped", "completed") \
                    and S.get("status_policy", "conservative") == "conservative":
                ns = None
            item["new_status"] = ns

            if entry and target == cur and not ns:
                item["action"] = "noop"
                if show_noop:
                    plan["noop"].append(item)
                continue
            if not entry:
                item["action"] = "new_entry"
            elif target > cur:
                item["action"] = "bump"
            else:
                item["action"] = "status_only"
            if (target - cur) > int(S.get("max_episode_bump", 26)):
                item["flags"].append(f"single jump of {target - cur} episodes exceeds max_episode_bump")
                plan["blocked"].append(item)
                continue
            # One MAL entry must not be written to by two Plex seasons in the same run.
            # Both Danganronpa seasons resolved to 16592 (13 eps and 7 eps); the second
            # write would be silently dropped by never-decrease, hiding a bad match.
            other = claimed.get(item["mal_id"])
            if other is not None:
                keep, lose = (item, other) if (item["new_watched"] or 0) > \
                    (other["new_watched"] or 0) else (other, item)
                lose["flags"].append(
                    "another Plex unit (%s %s) already resolves to MAL %s for %d episodes; "
                    "only one write per entry per run - which entry is %s really?"
                    % (keep.get("plex_show"), keep.get("season"), item["mal_id"],
                       keep["new_watched"] or 0, item["mal_id"]))
                if lose is item:
                    plan["review"].append(item)
                    continue
                plan["items"].remove(other)
                plan["review"].append(other)
            claimed[item["mal_id"]] = keep if other is not None else item
            plan["items"].append(item)

    # ---- per-run caps (writes are ordered safest first)
    order = {"bump": 0, "status_only": 1, "new_entry": 2}
    plan["items"].sort(key=lambda it: (order.get(it["action"], 3), -(it["confidence"] or 0)))
    n_new = n_write = 0
    keep = []
    for it in plan["items"]:
        if it["action"] == "new_entry":
            n_new += 1
            if n_new > int(S.get("max_new_entries_per_run", 15)):
                it["flags"].append("over max_new_entries_per_run - deferred to a later run")
                plan["blocked"].append(it)
                continue
        n_write += 1
        if n_write > int(S.get("max_writes_per_run", 25)):
            it["flags"].append("over max_writes_per_run - deferred to a later run")
            plan["blocked"].append(it)
            n_write -= 1
            continue
        keep.append(it)
    plan["items"] = keep

    store.set_meta("rescache", json.dumps(matchmaking.dump_cache()))
    plan["hash"] = plan_hash(plan)
    plan["stats"] = {
        "writes": len(plan["items"]), "new_entries": sum(1 for i in plan["items"] if i["action"] == "new_entry"),
        "bumps": sum(1 for i in plan["items"] if i["action"] == "bump"),
        "status_only": sum(1 for i in plan["items"] if i["action"] == "status_only"),
        "review": len(plan["review"]), "blocked": len(plan["blocked"]),
        "noop": len(plan["noop"]) if show_noop else 0,
    }
    return plan


def plan_hash(plan):
    core = [{"mal_id": i["mal_id"], "action": i["action"], "new_watched": i["new_watched"],
             "new_status": i["new_status"], "plex_show": i["plex_show"], "season": i["season"]}
            for i in plan.get("items", [])]
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:12]


# ------------------------------------------------------------- apply

def apply_plan(cfg, store, log, plan, confirm=None, force=False, mal=None, max_calls=None):
    if not force and (confirm or "") != plan.get("hash"):
        raise SystemExit(f"plan hash mismatch - re-read the report and pass --confirm {plan.get('hash')}")
    items = [i for i in plan.get("items", []) if i.get("action") in ("new_entry", "bump", "status_only")]
    if not items:
        log.info("nothing to write")
        return {"written": 0, "failed": 0}
    mal = mal or malapi.MAL(cfg, log)
    if not mal.ready():
        raise SystemExit("not authorised with MyAnimeList - run:  python plex_mal.py login")

    log.info(f"backing up your live MAL list before writing {len(items)} change(s)")
    bpath, live = mal.backup(f"pre-apply {plan.get('hash')}")
    live_by_id = {k: v for k, v in (live or {}).items()}

    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + (plan.get("hash") or "x")
    written = failed = skipped = 0
    expect = []
    for it in items:
        if max_calls and (written + failed) >= max_calls:
            log.warn("hit --max-calls, stopping")
            break
        cur = live_by_id.get(it["mal_id"])
        if cur is not None and cur.get("watched") is None:
            log.error(f"SKIP anime/{it['mal_id']}: MAL did not report the live watched count, so "
                      f"'never decrease' cannot be verified. Nothing written.")
            store.log_applied(run_id, it["mal_id"], it["action"], "{}", "{}", False,
                              "skipped: live watched count unknown")
            skipped += 1
            continue
        cur_w = int((cur or {}).get("watched") or 0)
        cur_s = (cur or {}).get("status")
        before = {"watched": cur_w, "status": cur_s, "existed": bool(cur)}
        if cur and it["new_watched"] < cur_w:
            log.warn(f"SKIP anime/{it['mal_id']}: live MAL now has {cur_w} > planned {it['new_watched']} "
                     f"(never decrease)")
            store.log_applied(run_id, it["mal_id"], it["action"], json.dumps(before), "{}", False,
                              "skipped: would decrease")
            skipped += 1
            continue
        fields = {}
        if it.get("new_status"):
            fields["status"] = it["new_status"]
        if not cur or it["new_watched"] != cur_w:
            fields["num_watched_episodes"] = it["new_watched"]
        if not fields:
            skipped += 1
            continue
        ok, st, body = mal.update(it["mal_id"], fields)
        store.log_applied(run_id, it["mal_id"], it["action"], json.dumps(before), json.dumps(fields),
                          ok, f"HTTP {st}")
        if ok:
            written += 1
            expect.append((it["mal_id"], it["new_watched"], fields.get("status"), cur_w, cur_s))
            log.info(f"  ok  anime/{it['mal_id']} {it['mal_title'] or ''} "
                     f"{cur_w}->{fields.get('num_watched_episodes', cur_w)} eps "
                     f"{('/' + fields['status']) if fields.get('status') else ''}")
        else:
            failed += 1
            log.error(f"  FAIL anime/{it['mal_id']} -> HTTP {st} {truncate(str(body), 160)}")

    # ---- read the list back and prove every write actually landed
    mismatches = []
    if expect:
        try:
            back = mal.my_list()
        except Exception as e:
            back, mismatches = None, [f"could not re-read the list to verify writes: {e}"]
        for mal_id, want_w, want_s, was_w, was_s in (expect if back else []):
            got = (back or {}).get(mal_id) or {}
            gw, gs = got.get("watched"), got.get("status")
            if gw is None or gw != want_w or (want_s and gs != want_s):
                mismatches.append(f"anime/{mal_id}: asked for {want_w} eps"
                                  + (f"/{want_s}" if want_s else "") + f", MAL now says {gw}/{gs}")
        if mismatches:
            log.error("VERIFY FAILED after apply:\n    " + "\n    ".join(mismatches))
            log.error("Your list may be partly updated. Restore from the backup: " + str(bpath))
        else:
            log.info(f"verify ok: re-read {len(back or {})} entries, all {len(expect)} write(s) "
                     f"landed exactly as planned")
    log.info(f"apply done: {written} written, {failed} failed, {skipped} skipped. "
             f"backup: {bpath}")
    store.set_meta("last_apply_run", run_id)
    return {"written": written, "failed": failed, "skipped": skipped, "backup": bpath, "run_id": run_id}


def undo_last(cfg, store, log, mal=None, dry=True):
    run_id = store.get_meta("last_apply_run") or store.last_run()
    if not run_id:
        log.info("nothing recorded as written")
        return
    rows = [r for r in store.run_rows(run_id) if r["ok"]]
    log.info(f"undoing run {run_id}: {len(rows)} change(s)")
    mal = mal or malapi.MAL(cfg, log)
    if not dry and not mal.ready():
        raise SystemExit("not authorised with MyAnimeList")
    for r in reversed(rows):
        try:
            before = json.loads(r["before"] or "{}")
        except Exception:
            before = {}
        fields = {}
        if before.get("watched") is None and not before.get("existed"):
            log.info(f"  skip anime/{r['mal_id']}: nothing was recorded as written before it")
            continue
        if before.get("existed") and before.get("status"):
            fields["status"] = before["status"]
        if before.get("watched") is None:
            log.warn(f"  anime/{r['mal_id']}: the pre-write watched count was never recorded - "
                     f"leaving its episode count alone rather than writing 0")
        else:
            fields["num_watched_episodes"] = int(before.get("watched") or 0)
        note = "" if before.get("existed") else " (created by us: MAL has no delete API - set to 0 eps, " \
                                             "remove the entry yourself if you want it gone)"
        log.info(f"  restore anime/{r['mal_id']} -> {fields}{note}")
        if not dry:
            mal.update(r["mal_id"], fields)
