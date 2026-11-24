"""Resolve Plex show titles -> MyAnimeList entries.

Free, no-auth sources are used for matching (AniList GraphQL is primary,
Shikimori + Kitsu as cross-checks, MAL's own search when a client id exists).
Every decision records its evidence; anything not clearly right goes to a
review queue instead of being guessed at.
"""
from __future__ import annotations

import json
import re
import time

from .util import best_sim, http, norm_title, roman, sim, truncate

ANILIST = "https://graphql.anilist.co"

_ANILIST_Q = """
query($s:String!){
  Page(perPage:10){
    media(search:$s,type:ANIME,sort:SEARCH_MATCH){
      id idMal isAdult format status season seasonYear episodes duration countryOfOrigin
      title{romaji english native canonical} synonyms
      relations{edges{relationType node{
        id idMal isAdult format status season seasonYear episodes
        title{romaji english native} synonyms
      }}}
    }
  }
}"""

_AL_FIELDS = ("id", "idMal", "isAdult", "format", "status", "season", "seasonYear",
              "episodes", "duration", "countryOfOrigin", "title", "synonyms")


def _cand(source, **kw):
    c = {"source": source, "mal_id": None, "anilist_id": None, "titles": [], "year": None,
         "episodes": None, "fmt": None, "status": None, "adult": False, "country": None,
         "relations": []}
    c.update(kw)
    return c


def _titles(raw):
    t = raw or {}
    out = []
    for k in ("romaji", "english", "native", "canonical"):
        v = t.get(k)
        if v:
            out.append(v)
    for s in (raw.get("synonyms") or []):
        if s:
            out.append(s)
    return out


def anilist_candidates(title, log=None, rl=None):
    if rl:
        rl.wait()
    st, j, _ = http("POST", ANILIST, json_body={"query": _ANILIST_Q, "variables": {"s": title}}, retries=2, log=log)
    if st != 200 or not isinstance(j, dict):
        if log:
            log.warn(f"anilist {title!r} -> HTTP {st} {truncate(str(j), 120)}")
        return []
    media = (((j.get("data") or {}).get("Page") or {}).get("media")) or []
    out = []
    for m in media:
        rels = []
        for e in (m.get("relations") or {}).get("edges") or []:
            n = e.get("node") or {}
            rels.append({"rel": e.get("relationType"), **{k: n.get(k) for k in _AL_FIELDS}})
        out.append(_cand("anilist",
                         anilist_id=m.get("id"), mal_id=m.get("idMal"), adult=bool(m.get("isAdult")),
                         fmt=m.get("format"), status=m.get("status"), year=m.get("seasonYear"),
                         episodes=m.get("episodes"), country=m.get("countryOfOrigin"),
                         titles=_titles(m), relations=rels))
    return out


def shikimori_candidates(title, log=None, rl=None):
    if rl:
        rl.wait()
    st, j, _ = http("GET", "https://shikimori.one/api/animes",
                    params={"search": title, "limit": 6, "censored": 1 if False else None}, retries=1, log=log)
    if st != 200 or not isinstance(j, list):
        if log:
            log.warn(f"shikimori {title!r} -> HTTP {st} {truncate(str(j), 100)}")
        return []
    out = []
    for a in j:
        kind = (a.get("kind") or "").upper()
        eps = a.get("episodes") or a.get("episodes_aired")
        out.append(_cand("shikimori", mal_id=a.get("id"), fmt=kind if kind else None,
                         episodes=eps if eps and eps > 0 else None,
                         titles=[a.get("name"), a.get("russian"), a.get("japanese")],
                         status=a.get("status")))
    return [c for c in out if c["titles"]]


def mal_search_candidates(cfg, title, log=None, rl=None):
    cid = (cfg.mal.get("client_id") or "").strip()
    if not cid:
        return []
    if rl:
        rl.wait()
    st, j, _ = http("GET", "https://api.myanimelist.net/v2/anime",
                    params={"q": title, "limit": 8, "nsfw": "true",
                            "fields": "id,title,main_episode_count,status,start_season,adult"},
                    headers={"X-MAL-CLIENT-ID": cid}, retries=1, log=log)
    if st != 200 or not isinstance(j, dict):
        if log:
            log.warn(f"mal search {title!r} -> HTTP {st} {truncate(str(j), 120)}")
        return []
    out = []
    for e in j.get("data") or []:
        d = e.get("node") or {}
        ss = d.get("start_season") or {}
        out.append(_cand("mal", mal_id=d.get("id"), titles=[d.get("title")] + list(d.get("synonyms") or []),
                         episodes=d.get("main_episode_count"), fmt=(d.get("media_type") or "").upper() or None,
                         year=ss.get("year"), status=(d.get("status") or "").upper(), adult=bool(d.get("adult"))))
    return out


# ------------------------------------------------------------------ scoring

def _year_of(c):
    return c.get("year") or None


def score_candidate(c, queries, plex_year, season, watched, cross_ids, skip_adult, sibling_ids):
    """Return (score 0..~1.2, reasons list, hard_reject bool)."""
    reasons = []
    base = best_sim(queries, [norm_title(t) for t in c["titles"] if t])
    if base < 0.50:
        return base, [f"title_sim={base:.2f} too low"], True
    s = base
    reasons.append(f"title={base:.2f}")

    if c["mal_id"]:
        s += 0.06
        reasons.append("has_mal_id")
    else:
        reasons.append("NO_MAL_ID")

    if c.get("adult"):
        if skip_adult:
            return 0.0, ["adult_skipped"], True
        s -= 0.05

    eps = c.get("episodes")
    if eps and watched and eps < watched:
        reasons.append(f"episodes({eps})<watched({watched})")
        return 0.0, reasons, True

    y = _year_of(c)
    if y and plex_year:
        delta = abs(y - plex_year) - max(0, (season or 1) - 1)
        if delta <= 0:
            s += 0.10
            reasons.append(f"year={y}")
        elif delta <= 1:
            s += 0.05
            reasons.append(f"year~{y}")
        elif delta <= 3:
            s += 0.0
            reasons.append(f"year?{y}")
        else:
            s -= 0.18
            reasons.append(f"year_off_by_{y - plex_year}")

    if c.get("country") and c["country"] != "JP":
        s -= 0.10
        reasons.append(f"country={c['country']}")

    if eps and eps >= max(6, (watched or 1)):
        s += 0.05
        reasons.append(f"eps={eps}")

    if c["mal_id"] and c["mal_id"] in cross_ids:
        s += 0.14
        reasons.append("cross_source_agreement")

    if season and season > 1 and c["mal_id"] and c["mal_id"] in sibling_ids:
        s -= 0.45
        reasons.append("already_used_for_other_season")

    fmt = (c.get("fmt") or "")
    if fmt in ("MOVIE", "MUSIC") and (watched or 1) > 1:
        s -= 0.30
        reasons.append(f"fmt={fmt}_penalty")
    elif fmt in ("TV", "ONA") :
        s += 0.04

    return min(s, 1.35), reasons, False


def _dedupe(cands):
    out, seen = [], set()
    for c in cands:
        k = (c.get("mal_id") or ("al" if not c.get("mal_id") else "") , c.get("mal_id") or c.get("anilist_id"))
        key = c.get("mal_id") or ("anilist:%s" % c.get("anilist_id"))
        if key in seen:
            for prev in out:
                if (prev.get("mal_id") or ("anilist:%s" % prev.get("anilist_id"))) == key:
                    prev["titles"] = list(dict.fromkeys(prev["titles"] + c["titles"]))
                    if not prev.get("episodes") and c.get("episodes"):
                        prev["episodes"] = c["episodes"]
                    if not prev.get("year") and c.get("year"):
                        prev["year"] = c["year"]
                    if c.get("relations") and not prev.get("relations"):
                        prev["relations"] = c["relations"]
            continue
        seen.add(key)
        out.append(c)
    return out


def season_chain(anilist_id, log=None, rl=None, depth=8):
    """Follow AniList SEQUEL relations to get per-season entries (with MAL ids)."""
    chain, cur_id, seen = [], anilist_id, set()
    for _ in range(depth):
        if not cur_id or cur_id in seen:
            break
        seen.add(cur_id)
        st, j, _ = http("POST", ANILIST,
                        json_body={"query": "query($id:Int){Media(id:$id){id idMal season seasonYear episodes "
                                            "format title{romaji english} relations{edges{relationType "
                                            "node{id idMal season seasonYear episodes format title{romaji english}}}}}}",
                                   "variables": {"id": int(cur_id)}}, retries=1, log=log)
        if rl:
            rl.wait()
        if st != 200 or not isinstance(j, dict):
            break
        m = (j.get("data") or {}).get("Media") or {}
        chain.append({"anilist_id": m.get("id"), "mal_id": m.get("idMal"), "year": m.get("seasonYear"),
                      "episodes": m.get("episodes"), "titles": _titles(m)})
        nxt = None
        for e in (m.get("relations") or {}).get("edges") or []:
            if (e.get("relationType") or "").upper() == "SEQUEL":
                n = e.get("node") or {}
                if n.get("type") in (None, "ANIME") and n.get("id") not in seen:
                    nxt = n.get("id")
                    break
        cur_id = nxt
    return chain


def resolve_season(cfg, log, rl, show, season, watched, other_seasons, sibling_ids=()):
    """Resolve one (show, season) -> best MAL candidate + confidence + alternatives."""
    skip_adult = bool(cfg.sync.get("skip_adult", True))
    titles = [show["plex_title"]]
    if show.get("original_title"):
        titles.append(show["original_title"])
    if season and season > 1:
        titles += [f"{show['plex_title']} season {season}", f"{show['plex_title']} {roman(season)}"]
    queries = list(dict.fromkeys(norm_title(t) for t in titles if t))

    pool = []
    seen_q = set()
    for q in titles:
        if q.lower() in seen_q:
            continue
        seen_q.add(q.lower())
        pool += anilist_candidates(q, log, rl)
        pool += shikimori_candidates(q, log, rl)
        if len(queries) >= 3:
            break
    pool += mal_search_candidates(cfg, show["plex_title"], log, rl)
    pool = _dedupe(pool)

    cross = {}
    for c in pool:
        if c.get("mal_id"):
            cross[c["mal_id"]] = cross.get(c["mal_id"], 0) + 1
    cross_ids = {k for k, v in cross.items() if v >= 2}

    scored = []
    for c in pool:
        sc, why, rej = score_candidate(c, queries, show.get("year"), season, watched,
                                       cross_ids, skip_adult, set(sibling_ids or ()))
        if rej:
            continue
        scored.append({"score": round(sc, 4), "why": why, "cand": c})
    scored.sort(key=lambda x: -x["score"])

    if not scored:
        return {"state": "needs_review", "score": 0.0, "cand": None, "alternatives": [],
                "reason": "no candidate survived filtering"}

    top = scored[0]
    second = scored[1]["score"] if len(scored) > 1 else 0.0
    margin = float(cfg.sync.get("ambiguity_margin", 0.08))
    minc = float(cfg.sync.get("min_confidence", 0.75))
    alts = [{"score": x["score"], "mal_id": x["cand"].get("mal_id"),
             "title": truncate(x["cand"]["titles"][0] if x["cand"]["titles"] else "", 60),
             "year": x["cand"].get("year"), "episodes": x["cand"].get("episodes")}
            for x in scored[1:5]]

    if not top["cand"].get("mal_id"):
        return {"state": "needs_review", "score": top["score"], "cand": top["cand"], "alternatives": alts,
                "reason": "best match has no MAL id"}
    if top["score"] < minc:
        return {"state": "needs_review", "score": top["score"], "cand": top["cand"], "alternatives": alts,
                "reason": f"confidence {top['score']:.2f} < {minc}"}
    if second and (top["score"] - second) < margin and second >= minc - 0.1:
        return {"state": "needs_review", "score": top["score"], "cand": top["cand"], "alternatives": alts,
                "reason": f"ambiguous (runner-up {second:.2f})"}

    return {"state": "auto", "score": top["score"], "cand": top["cand"], "alternatives": alts, "reason": "ok"}
