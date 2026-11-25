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

_POOL_CACHE = {}

# Short-form / promo entries that share a series' exact title but are NOT the series:
# "Sousou no Frieren Mini Anime", "Saikyou Onmyouji no Isekai Tenseiki Mini Anime",
# "Yani Neko Mini Anime", previews, recaps, trailers, MVs. A full 12-episode season must
# never be filed against one of these - it writes 12 watched episodes into the wrong entry.
VARIANT_RE = re.compile(
    r"mini|("
    r"preview|recap|re[\s\-]?cap|make[\s\-]?of|making[\s\-]?of|trailer|teaser|"
    r"\bpv\b|\bcm\b|opening|ending[\s\-]?theme|insert[\s\-]?song|image[\s\-]?song|"
    r"music[\s\-]?video|\bmv\b|pixiv|douga|short[\s\-]?story|short[\s\-]?film|"
    r"specials?\b|\bspecials\b|commentary|footage|collab)", re.I)


# what may follow a short title after a bare space before it is still the same show
SEASONISH_RE = re.compile(r"(?i)^(ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii|\d+(st|nd|rd|th)?|part|"
                          r"season|s\d+|ova|oad|movie|film|premiere|ex|plus|zero|0|final|end|"
                          r"kou|ni|dai|2nd|3rd)\b")


def _short_title_ok(raw, q):
    """Is `raw` really the series called `q` (both lowercase), not just something
    that happens to contain the word? 'gate' -> 'Gate: Jieitai...' yes;
    'gate' -> 'Gatekeepers' / 'Gate Keepers' / 'Black Gate' no."""
    r = str(raw or "").strip().lower()
    if not r.startswith(q):
        return False
    rest = r[len(q):]
    if not rest.strip():
        return True                                   # the title *is* the series
    seps = ":;-–'\"(,./&!?+|="
    if rest[0] in seps or (rest.lstrip() and rest.lstrip()[0] in seps):
        return True                                   # "gate: jieitai", "gate -the golden age-"
    return bool(SEASONISH_RE.match(norm_title(rest)))  # "86 part 2", "86 ii"


def variant_title(cand):
    """The title that marks this candidate as a spin-off/promo entry, else None."""
    for t in (cand.get("titles") or []):
        t = str(t or "")
        if VARIANT_RE.search(t):
            return t
    return None


def load_cache(d):
    """Reuse previously fetched candidate pools (keeps re-runs fast and polite)."""
    global _POOL_CACHE
    _POOL_CACHE = dict(d or {})


def dump_cache():
    return _POOL_CACHE


def _cand(source, **kw):
    c = {"source": source, "mal_id": None, "anilist_id": None, "titles": [], "year": None,
         "episodes": None, "fmt": None, "status": None, "adult": False, "country": None,
         "popularity": None, "relations": []}
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
    return anilist_multi([title], log, rl)


def anilist_multi(titles, log=None, rl=None):
    """One GraphQL request for up to 4 title variants (aliased queries)."""
    titles = [t for t in dict.fromkeys(titles or []) if t][:4]
    if not titles:
        return []
    key = "||".join(titles).lower()
    if key in _POOL_CACHE:
        return _POOL_CACHE[key]
    vars_, sel = {}, []
    for i, t in enumerate(titles):
        a = f"q{i}"
        vars_[a] = t
        sel.append(f"{a}: Page(perPage:8){{ media(search:${a},type:ANIME,sort:SEARCH_MATCH) {{ " \
                   f"id idMal isAdult format status season seasonYear episodes popularity countryOfOrigin " \
                   f"title{{romaji english native}} synonyms relations{{edges{{relationType " \
                   f"node{{id idMal isAdult format status season seasonYear episodes title{{romaji english}}}}}}}} }} }}")
    q = "query(" + ",".join(f"${a}:String!" for a in vars_) + ")" + "{" + ",".join(sel) + "}"
    if rl:
        rl.wait()
    st, j, _ = http("POST", ANILIST, json_body={"query": q, "variables": vars_}, retries=5, log=log)
    if st != 200 or not isinstance(j, dict):
        if log:
            log.warn(f"anilist {titles!r} -> HTTP {st} {truncate(str(j), 140)}")
        return []
    out = []
    for k in [f"q{i}" for i in range(len(titles))]:
        node = (j.get("data") or {}).get(k) or {}
        # aliased GraphQL: the alias IS the Page object, so media sits directly under it
        media = node.get("media") or ((node.get("Page") or {}).get("media")) or []
        for m in media:
            rels = []
            for e in (m.get("relations") or {}).get("edges") or []:
                n = e.get("node") or {}
                rels.append({"rel": e.get("relationType"), **{x: n.get(x) for x in _AL_FIELDS}})
            out.append(_cand("anilist", anilist_id=m.get("id"), mal_id=m.get("idMal"),
                             adult=bool(m.get("isAdult")), fmt=m.get("format"), status=m.get("status"),
                             year=m.get("seasonYear"), episodes=m.get("episodes"), popularity=m.get("popularity"),
                             country=m.get("countryOfOrigin"), titles=_titles(m), relations=rels))
    _POOL_CACHE[key] = out
    return out


def shikimori_candidates(title, log=None, rl=None):
    if rl:
        rl.wait()
    st, j, _ = http("GET", "https://shikimori.one/api/animes",
                    params={"search": title, "limit": 6}, retries=1, log=log)
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
                            "fields": "id,title,synonyms,alternative_titles,media_type,"
                                      "main_episode_count,num_episodes,status,start_season,adult"},
                    headers={"X-MAL-CLIENT-ID": cid}, retries=1, log=log)
    if st != 200 or not isinstance(j, dict):
        if log:
            log.warn(f"mal search {title!r} -> HTTP {st} {truncate(str(j), 120)}")
        return []
    out = []
    for idx, e in enumerate(j.get("data") or []):
        d = e.get("node") or {}
        ss = d.get("start_season") or {}
        alt = d.get("alternative_titles") or {}
        # MAL's `title` is romaji ("Hataraku Saibou") while Plex/TMDB use the official
        # English ("Cells at Work!"). Without alternative_titles there is no textual
        # bridge at all between them and the real entry gets rejected as unrelated.
        titles = [d.get("title"), alt.get("en"), alt.get("ja")]
        titles += list(alt.get("synonyms") or []) + list(d.get("synonyms") or [])
        out.append(_cand("mal", mal_id=d.get("id"), titles=[t for t in titles if t],
                         episodes=d.get("num_episodes") or d.get("main_episode_count"),
                         fmt=(d.get("media_type") or "").upper() or None,
                         year=ss.get("year"), status=(d.get("status") or "").upper(),
                         adult=bool(d.get("adult")), rank=idx, mal_rank=idx))
    return out


# ------------------------------------------------------------------ scoring

def _year_of(c):
    return c.get("year") or None


_ROM = {"ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
        "xi": 11, "xii": 12, "iiInd": 2}


def season_hint(text):
    """Which season number is encoded in a title ('2nd Season', 'II', 'Part 2', 'Psycho-Pass 2').

    Deliberately conservative: a one-word title like '86' must not become season 86.
    """
    t = " " + re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip() + " "
    m = re.findall(r"(?:season|staffel|cour|part)[\s:.\-]*(\d{1,2})", t)
    if m:
        return int(m[-1])
    m = re.findall(r"(\d{1,2})(?:st|nd|rd|th)\s+(?:season|part|cour)", t)
    if m:
        return int(m[-1])
    toks = t.split()
    for w in toks:
        if w in _ROM:
            return _ROM[w]
    if len(toks) >= 2:
        m = re.findall(r"(\d{1,2})\s*$", t)
        if m and 1 < int(m[-1]) <= 12:
            return int(m[-1])
    return None


def best_title_match(queries, titles):
    """(similarity, season-hint of the title that matched best)."""
    best, hint, seen = 0.0, None, set()
    for t in titles:
        nt = norm_title(t)
        if not nt or nt in seen:
            continue
        seen.add(nt)
        s = max((sim(q, nt) for q in queries), default=0.0)
        if s > best:
            best, hint = s, season_hint(t)
    return best, hint


def score_candidate(c, queries, plex_year, season, watched, cross_ids, skip_adult, sibling_ids, orig=None):
    """Return (score 0..~1.2, reasons list, hard_reject bool)."""
    reasons = []
    base, hint = best_title_match(queries, c["titles"])
    # MAL's own search is the only source that knows Plex's "Cells at Work!" is MAL's
    # "Hataraku Saibou", or "Attack on Titan" is "Shingeki no Kyojin". Character
    # similarity cannot see that link at all (0.32), so it must not be allowed to veto a
    # top search hit; those are judged on rank, year, episode count and agreement instead.
    mal_rank = c.get("mal_rank")
    floor = 0.28 if mal_rank is not None else 0.50
    if base < floor:
        return base, [f"title_sim={base:.2f} too low"], True
    s = base
    reasons.append(f"title={base:.2f}")
    if mal_rank is not None:
        bonus = {0: 0.34, 1: 0.26, 2: 0.20}.get(int(mal_rank), 0.10)
        s += bonus
        reasons.append(f"mal_search_rank={mal_rank}+{bonus:.2f}")

    # A very short Plex title ("Gate", "86", "CSM") substring-matches half the database:
    # "Gate" happily matched "Black Gate"/Hei Men and would have created a duplicate entry
    # for a show that is already on the list. Such titles must match a candidate that starts
    # with them (or equals them), not merely contains them.
    short = [q for q in (norm_title(x) for x in queries) if 0 < len(q) <= 5]
    if short:
        if not any(_short_title_ok(t, q) for t in (c["titles"] or []) for q in short):
            return base, [f"short title '{short[0]}' is not a prefix of this entry"], True

    # (a duplicate of the short-title guard above was removed here - it crashed resolve)

    # season markers are decisive when a Plex show spans several seasons
    if season and season > 1:
        if hint == season:
            s += 0.20
            reasons.append(f"season_hint={hint}")
        elif hint is not None:
            s -= 0.45
            reasons.append(f"season_hint{hint}_not_S{season}")
    elif season is None and hint and hint > 1:
        s -= 0.28
        reasons.append(f"looks_like_season_{hint}")
    elif season == 1 and hint and hint > 1:
        s -= 0.20
        reasons.append(f"looks_like_season_{hint}")

    # You cannot have watched episodes of something that has not aired. "Shibou Yuugi de
    # Meshi wo Kuu. (Zok.)" (MAL 65513) is the announced sequel with no episodes listed,
    # and it outscored the series that actually aired (59711) - 11 watched episodes would
    # have been filed against an entry that has nothing to watch.
    if str(c.get("status") or "").upper() in ("NOT_YET_AIRD", "ANNOUNCED", "NOT_YET_AIRING",
                                              "PRODUCTION"):
        return base, [f"entry has not aired ({c.get('status')})"], True
    if (watched or 0) > 0 and c.get("episodes") == 0:
        return base, ["entry lists 0 episodes but Plex has watched episodes"], True

    # mini anime / preview / recap / trailer entries are a different product. Push them
    # well down so the real series wins; resolve_season refuses to auto-write either way.
    vt = variant_title(c)
    if vt:
        pen = 0.60 if (watched or 0) > 4 else 0.30
        s -= pen
        reasons.append(f"variant_entry({truncate(vt, 26)})-{pen:.2f}")

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
    if eps and watched:
        if eps == watched:
            s += 0.12
            reasons.append("eps_match")
        elif eps >= watched and eps <= watched * 1.35:
            s += 0.05
            reasons.append("eps_plausible")

    # a real series outranks a PV / mini-anime / trailer with a similar name
    pop = c.get("popularity")
    if pop:
        if pop <= 2000:
            s += 0.12
            reasons.append(f"pop={pop}")
        elif pop <= 8000:
            s += 0.06
            reasons.append(f"pop={pop}")
        elif pop > 40000:
            s -= 0.10
            reasons.append(f"pop={pop}")

    # Plex keeps the Japanese title: matching it against native titles is strong evidence
    if orig:
        for t in c["titles"]:
            v = sim(orig, norm_title(t))
            if v > base:
                base, s = max(base, v), max(s, v + 0.10)
                reasons.append(f"native={v:.2f}")
                break

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
                    # carry MAL's search rank across the merge: the surviving record is
                    # often the AniList copy, which would otherwise lose the fact that
                    # MAL itself returned this entry first for the Plex title
                    if c.get("mal_rank") is not None and prev.get("mal_rank") is None:
                        prev["mal_rank"] = c["mal_rank"]
                    prev["sources"] = sorted(set([prev.get("source")] + [c.get("source")] +
                                                 (c.get("sources") or [])))
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
    pt = (show.get("plex_title") or "").strip()
    if not pt or pt.lower().startswith(("plex://", "rk:", "local://")):
        # history for a show that is no longer in the library: there is no title to match on
        return {"state": "ignored", "score": 0.0, "cand": None, "alternatives": [],
                "reason": "no usable Plex title (show removed from the library?)"}
    titles = [pt]
    if show.get("original_title"):
        titles.append(show["original_title"])
    # Plex names each season from TMDB data, and that name is often the sub-series itself:
    # "Tokyo Ghoul √A", "Kyoto Saga", "-Shimane Illuminati Saga-". That is exactly what
    # tells a Plex S3 apart from the S1 of the same show, so search and score on it.
    stitle = next((str(s.get("season_title") or "").strip() for s in (show.get("seasons") or [])
                   if s.get("season") == season), "")
    if stitle and not re.fullmatch(r"(?i)(season|stagione|staffel)\s*\d+", stitle):
        titles.append(stitle)
        if norm_title(stitle) != norm_title(pt):
            titles.append(f"{pt} {stitle}")
    if season and season > 1:
        titles += [f"{show['plex_title']} season {season}", f"{show['plex_title']} {roman(season)}"]
    queries = list(dict.fromkeys(norm_title(t) for t in titles if t))

    pool = anilist_multi(titles, log, rl)
    pool += shikimori_candidates(show["plex_title"], log, rl)
    if stitle and norm_title(stitle) != norm_title(show["plex_title"]):
        pool += shikimori_candidates(stitle, log, rl)
    if show.get("original_title") and norm_title(show["original_title"]) not in queries:
        pool += shikimori_candidates(show["original_title"], log, rl)
    pool += mal_search_candidates(cfg, show["plex_title"], log, rl)
    if stitle and norm_title(stitle) != norm_title(show["plex_title"]):
        pool += mal_search_candidates(cfg, stitle, log, rl)
    pool = _dedupe(pool)

    cross = {}
    for c in pool:
        if c.get("mal_id"):
            cross[c["mal_id"]] = cross.get(c["mal_id"], 0) + 1
    cross_ids = {k for k, v in cross.items() if v >= 2}

    orig_norm = norm_title(show.get("original_title") or "")
    scored = []
    for c in pool:
        sc, why, rej = score_candidate(c, queries, show.get("year"), season, watched,
                                       cross_ids, skip_adult, set(sibling_ids or ()), orig=orig_norm)
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

    # A mini anime / preview / recap entry must never outrank the real series just
    # because its title is a verbatim copy of it. Prefer the best non-variant candidate;
    # if nothing non-variant clears the bar, send it to review instead of writing 12
    # watched episodes into a short-form spin-off.
    if variant_title(top["cand"]):
        nv = [x for x in scored if not variant_title(x["cand"])]
        if nv and nv[0]["cand"].get("mal_id") and nv[0]["score"] >= minc - 0.10:
            log.info(f"  skipping mini/promo entry '{truncate(variant_title(top['cand']), 34)}' "
                     f"in favour of '{truncate(nv[0]['cand']['titles'][0], 34)}'")
            top = nv[0]
            second = nv[1]["score"] if len(nv) > 1 else 0.0
        else:
            return {"state": "needs_review", "score": top["score"], "cand": top["cand"],
                    "alternatives": alts,
                    "reason": f"'{truncate(variant_title(top['cand']), 40)}' is a mini/preview/promo "
                              f"version, not the series itself - set the MAL id by hand if it is"}

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
