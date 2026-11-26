"""Independent audit of a plan - and of an apply afterwards - using MAL's own records.

The matcher and the engine both work from titles and counts they were handed. This module
deliberately ignores all of that and asks MyAnimeList instead: what is this entry really
called, how many episodes does it have, has it aired, is it an adult entry - and after
writing, what does my list actually contain now, and did anything move that we did not
touch?

Three planned writes were stopped by exactly this check before they could happen:
  * MAL 34742 "Youjo Shenki", the 2017 Chinese reboot, chosen for *Saga of Tanya the Evil*
    (32615, already on the list completed) - the canonical title is what gave it away.
  * MAL 62564 "Tate no Yuusha no Nariagari Season 5": status not_yet_aired, no episode
    count, chosen to receive Plex Shield Hero Season 1's 25 watches.
  * Two Plex seasons claiming the same MAL entry (Danganronpa, Assassination Classroom).

Everything here is read-only against MyAnimeList.
"""
from __future__ import annotations

from . import malapi, matchmaking
from .util import Ratelimit, norm_title, truncate

UNAIRED = ("NOT_YET_AIRD", "NOT_YET_AIRED", "ANNOUNCED", "NOT_YET_AIRING", "PRODUCTION",
           "UPCOMING")
SMALL_MEDIA = ("ona", "ova", "special", "tv_special", "tv_s", "music", "movie", "pv", "cm")
# MAL's /anime/{id} `nsfw` field is a colour word, not a boolean: a normal entry comes back
# as "white", which is truthy in Python. Treating it as a flag marks every entry in the
# database adult and refuses every plan (that is exactly what the first version of this
# code did, caught by the self-test). Only the explicit adult colours count.
NSFW_ADULT = ("pink", "red", "true", "1", "adult", "17+")


def _nsfw(v):
    """(is_adult, raw_value). Anything unrecognised is reported, never assumed adult."""
    if v is True:
        return True, str(v)
    raw = str(v or "").strip()
    return raw.lower() in NSFW_ADULT, raw


def canonical(mal, log, mal_id):
    """MAL's own record for one entry, or None if it cannot be read."""
    try:
        j = mal.anime(mal_id, fields=malapi.MAL.AUDIT_FIELDS) or {}
    except Exception as e:                                    # noqa: BLE001
        log.error(f"audit: cannot read MAL anime/{mal_id}: {e}")
        return None
    if not j.get("title"):
        return None
    alt = j.get("alternative_titles") or {}
    titles = [t for t in [j.get("title"), alt.get("en"), alt.get("ja")] if t]
    titles += list(alt.get("synonyms") or []) + list(j.get("synonyms") or [])
    return {"title": j.get("title"), "en": alt.get("en"), "episodes": j.get("num_episodes"),
            "type": (j.get("media_type") or "").lower(),
            "status": str(j.get("status") or "").upper(),
            "adult": _nsfw(j.get("nsfw") if j.get("nsfw") is not None else j.get("adult"))[0],
            "nsfw_raw": _nsfw(j.get("nsfw") if j.get("nsfw") is not None else j.get("adult"))[1],
            "titles": titles, "synonyms": list(alt.get("synonyms") or [])}


def _text(title):
    return norm_title(title or "")


def check_writes(cfg, mal, log, items, live=None, rl=None, search_check=True):
    """Return [(severity, mal_id, message)]. severity is 'block' or 'warn'.

    'block' means apply_plan must refuse to write anything at all: the plan is not the
    plan you read, or it points at something that cannot hold what we want to file there.
    """
    live = live or {}
    rl = rl or Ratelimit(1.2)
    skip_adult = bool((cfg.sync or {}).get("skip_adult", True))
    findings, seen = [], {}
    counts = {}
    for it in items:
        counts[it["mal_id"]] = counts.get(it["mal_id"], 0) + 1

    for it in items:
        mid = it["mal_id"]
        want = int(it.get("new_watched") or 0)
        src = it.get("source")
        human = src in ("override", "manual")
        c = canonical(mal, log, mid)
        if c is None:
            findings.append(("block", mid, "MAL would not return this entry, so the write "
                                          "cannot be checked at all"))
            continue
        if seen.get(mid) and seen[mid] != c["title"]:
            findings.append(("block", mid, f"one MAL entry used twice in this run "
                                          f"({seen[mid]!r} and {c['title']!r})"))
        seen[mid] = c["title"]

        label = f"{c['title']} ({c['type'] or '?'}, {c['episodes']} eps, {c['status'] or '?'})"

        # ---- things that must never be written
        if counts[mid] > 1:
            findings.append(("block", mid, f"claimed by {counts[mid]} Plex units this run - "
                                          f"only one write per entry is provable afterwards"))
        if c["episodes"] and want > int(c["episodes"]):
            findings.append(("block", mid, f"plan writes {want} episodes but the entry has "
                                          f"only {c['episodes']} ({label})"))
        cur = live.get(mid) or {}
        cur_w = cur.get("watched")
        if cur_w is not None and want < int(cur_w):
            findings.append(("block", mid, f"live list already has {cur_w} watched; plan would "
                                          f"write {want} (would decrease)"))
        if want > 0 and c["status"] in UNAIRED:
            findings.append(("block", mid, f"entry has not aired ({c['status']}) but the plan "
                                          f"files {want} watches against it ({label})"))
        if c["adult"] and skip_adult:
            findings.append(("block", mid, f"entry is flagged adult/NSFW and [sync] "
                                          f"skip_adult is on ({label})"))
        elif c.get("nsfw_raw") and c["nsfw_raw"].lower() not in ("white", ""):
            findings.append(("warn", mid, f"MAL tags this entry nsfw={c['nsfw_raw']!r} - not "
                                          f"blocked, but check it is the entry you mean"))
        variants = [t for t in c["titles"] if matchmaking.VARIANT_RE.search(str(t))]
        if not human and variants:
            findings.append(("block", mid, f"entry looks like a mini/preview/recap/spin-off "
                                          f"rather than the series (MAL calls it "
                                          f"{truncate(variants[0], 40)!r}), and the match was "
                                          f"not human-made ({label})"))

        # ---- things a human should glance at
        if not c["episodes"]:
            findings.append(("warn", mid, f"entry has no episode count on MAL (still airing?), "
                                          f"so the write of {want} cannot be capped ({label})"))
        if c["type"] in SMALL_MEDIA and want >= 10:
            findings.append(("warn", mid, f"{c['type'].upper()} entry receiving {want} episodes "
                                          f"- make sure that is the right entry ({label})"))
        if cur_w is not None and int(cur_w) == want and not it.get("new_status"):
            findings.append(("warn", mid, "already accurate - this write changes nothing"))
        ut = int(it.get("plex_unit_total") or 0)
        if c["episodes"] and ut and ut != int(c["episodes"]) and want >= ut:
            findings.append(("warn", mid, f"Plex unit has {ut} episodes, MAL entry has "
                                          f"{c['episodes']} - season grouping may differ"))
        if search_check and not human and it.get("plex_show"):
            rl.wait()
            hits = [h.get("mal_id") for h in
                    (matchmaking.mal_search_candidates(cfg, it["plex_show"], log, None) or [])]
            if mid not in hits:
                findings.append(("warn", mid, f"MAL's own search for {truncate(repr(it['plex_show']), 40)} "
                                              f"does not return this entry - the match came from "
                                              f"{src or 'another source'} only"))
    return findings


def diff(before, after):
    """Compare two MAL list snapshots ({id: {watched, status, title}}).

    Returns added / removed / changed / untouched, so an apply can prove that nothing
    outside its own write set moved.
    """
    before, after = before or {}, after or {}

    def state(e):
        return (str((e or {}).get("status")), (e or {}).get("watched"))

    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if state(before[k]) != state(after[k]))
    untouched = len(set(before) & set(after)) - len(changed)
    return {"added": added, "removed": removed, "changed": changed, "untouched": untouched,
            "before": len(before), "after": len(after)}


def moved_ids(d):
    return set(d["added"]) | set(d["changed"]) | set(d["removed"])


def format_findings(findings):
    """Group findings into printable lines: blockers first."""
    out = []
    for sev in ("block", "warn"):
        rows = [f for f in findings if f[0] == sev]
        if not rows:
            continue
        head = ("BLOCKERS - do not write this" if sev == "block" else "warnings - worth a look")
        out.append(f"{head} ({len(rows)})")
        for _, mid, msg in rows:
            out.append(f"  anime/{mid}: {msg}")
    return out


def write_summary(log, findings, d=None, expect_ids=()):
    """Log an audit result. Returns True when it is safe to proceed."""
    for line in format_findings(findings):
        # blockers must be LOUD: a header that ends up in the info stream is a blocker
        # somebody reads past on a long apply
        (log.error if line.startswith("BLOCKERS") else log.info)(line)
    if not findings:
        log.info("audit: clean - every write matches MAL's own record of the entry")
    if d is not None:
        extra = sorted(moved_ids(d) - set(expect_ids))
        log.info(f"audit: list {d['before']} -> {d['after']} entries, "
                 f"+{len(d['added'])} new, {len(d['changed'])} changed, "
                 f"{len(d['removed'])} removed, {d['untouched']} untouched")
        if d["removed"]:
            log.error(f"audit: entries disappeared from the list: {d['removed'][:20]}")
        if extra:
            log.error(f"audit: entries changed that were NOT part of the plan: {extra[:20]}")
        else:
            log.info("audit: nothing outside the planned writes moved")
    return not any(f[0] == "block" for f in findings)
