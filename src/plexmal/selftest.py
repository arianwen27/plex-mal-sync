"""Self-test for the safety rails. Run it after touching matchmaking.py or audit.py.

    python plex_mal.py selftest            # offline: no network, no MAL, no Plex
    python plex_mal.py selftest --online   # same rules re-checked against the real MAL API

Offline, the MAL answers are canned copies of entries this project actually aimed a write
at by mistake, and the "live list" is a small fixture so the results are the same on every
machine. The online pass sends the same items through the real API: an entry that has since
been corrected on MAL can change a warning into a clean row, which is why the online
assertions are limited to rules that do not depend on what happens to be on the list today.

Nothing in this module writes anything, online or off.
"""
from __future__ import annotations

import types

from . import audit, engine, matchmaking

# ---------------------------------------------------------------- fake MAL
# Copied from MAL's own /anime/{id} responses (fields=AUDIT_FIELDS). Each one is an entry
# this planner once tried, or nearly tried, to write to.
FAKE_ANIME = {
    # the two Kanketsu-hen episodes the user completed by hand; Plex keeps them in a
    # 37-episode "specials" unit, so a 30-episode write aimed here is always wrong
    51535: {"id": 51535, "title": "Shingeki no Kyojin: The Final Season - Kanketsu-hen",
            "alternative_titles": {"en": "Attack on Titan: The Final Season - The Telling",
                                   "ja": "", "synonyms": ["AoT Final Season Part 3"]},
            "synonyms": [], "nsfw": "white", "num_episodes": 2,
            "status": "FINISHED_AIRING", "media_type": "tv"},
    # what the matcher picked for "Shield Hero Season 1" before the season guard existed
    62564: {"id": 62564, "title": "Tate no Yuusha no Nariagari Season 5",
            "alternative_titles": {"en": "The Rising of the Shield Hero Season 5",
                                   "synonyms": []},
            "synonyms": [], "nsfw": "white", "num_episodes": 0,
            "status": "NOT_YET_AIRED", "media_type": "tv"},
    # the Frieren mini-anime: romaji title hides that it is a "Mini Anime" - only the
    # English synonym gives it away, which is why audit fetches alternative_titles
    56885: {"id": 56885, "title": "Sousou no Frieren: ●● no Mahou",
            "alternative_titles": {"en": "Frieren: Beyond Journey's End - ●● Magic",
                                   "synonyms": ["Frieren: Beyond Journey's End Mini Anime"]},
            "synonyms": [], "nsfw": "white", "num_episodes": 16,
            "status": "FINISHED_AIRING", "media_type": "ona"},
    # the real entry: nsfw comes back as the word "white", which is truthy in Python. The
    # first version of audit.py blocked every single write on it.
    30654: {"id": 30654, "title": "Assassination Classroom",
            "alternative_titles": {"en": "Assassination Classroom",
                                   "synonyms": ["Ansatsu Kyoushitsu"]},
            "synonyms": [], "nsfw": "white", "num_episodes": 22,
            "status": "FINISHED_AIRING", "media_type": "tv"},
    # an entry the user keeps "on hold": counts may bump, status may not
    59144: {"id": 59144, "title": "Kaso Saitei to Yobarete Oh ni Fasuru",
            "alternative_titles": {"en": "Even Given the Worthless 'Appraiser', I Am Actually "
                                         "at the Top of the Party", "synonyms": []},
            "synonyms": [], "nsfw": "white", "num_episodes": 12,
            "status": "FINISHED_AIRING", "media_type": "tv"},
    5114: {"id": 5114, "title": "Fullmetal Alchemist: Brotherhood",
           "alternative_titles": {"en": "Fullmetal Alchemist: Brotherhood",
                                  "synonyms": ["Hagane no Renkinjutsushi"]},
           "synonyms": [], "nsfw": "white", "num_episodes": 64,
           "status": "FINISHED_AIRING", "media_type": "tv"},
    # MAL returns an adult colour for pink entries; skip_adult must refuse it
    40001: {"id": 40001, "title": "Some Doujinshi Work",
            "alternative_titles": {"en": "", "synonyms": []},
            "synonyms": [], "nsfw": "pink", "num_episodes": 12,
            "status": "FINISHED_AIRING", "media_type": "tv"},
}

FAKE_LIVE = {5114: {"watched": 5, "status": "watching", "title": "Fullmetal Alchemist: Brotherhood"},
             30654: {"watched": 22, "status": "completed", "title": "Assassination Classroom"},
             59144: {"watched": 11, "status": "on_hold", "title": "Even Given the Worthless"}}


class FakeMAL:
    """Stands in for malapi.MAL. Reads a canned table; cannot write."""

    def __init__(self, table=None, live=None):
        self.table = table if table is not None else FAKE_ANIME
        self._live = live if live is not None else FAKE_LIVE
        self.reads = 0

    def anime(self, mal_id, fields=None):
        self.reads += 1
        assert fields, "audit must ask for the fields it needs: the default response is a stub"
        return dict(self.table.get(int(mal_id)) or {}) or None

    def my_list(self, *a, **kw):
        return dict(self._live)


CFG = types.SimpleNamespace(sync={"skip_adult": True})

FAILURES = []


def check(ok, label, detail=""):
    print("  %-4s %s%s" % ("PASS" if ok else "FAIL", label, ("  " + detail) if detail else ""))
    if not ok:
        FAILURES.append(label)
    return ok


# ---------------------------------------------------------------- A. season guard
def test_season_guard():
    print("\nA. matchmaking: an entry naming a LATER season than the Plex unit is refused\n")
    cases = [
        # (queries, candidate titles, eps, status, plex season, watched, expect_reject, label,
        #  reason substring that must be what rejected it, when we care which rule fired)
        (["The Rising of the Shield Hero Season 1"],
         ["The Rising of the Shield Hero Season 5"], None, "NOT_YET_AIRED", 1, 25, True,
         "Shield Hero S1 must not match 'Season 5'", "season_conflict"),
        (["Is It Wrong to Try to Pick Up Girls in a Dungeon Season 1"],
         ["Is It Wrong to Try to Pick Up Girls in a Dungeon? V: Houjou no Megami-hen"],
         15, "FINISHED_AIRING", 1, 14, True, "Danmachi S1 must not match 'Danmachi V'",
         "season_conflict"),
        (["Is It Wrong to Try to Pick Up Girls in a Dungeon"],
         ["Is It Wrong to Try to Pick Up Girls in a Dungeon? V: Houjou no Megami-hen"],
         15, "FINISHED_AIRING", 1, 14, True, "same, with no season number in the Plex title",
         "season_conflict"),
        (["86"], ["86 Part 2"], 12, "FINISHED_AIRING", 1, 11, True,
         "86 S1 must not match '86 Part 2'", "season_conflict"),
        (["Some Show"], ["Some Show Part 4"], 11, "FINISHED_AIRING", 3, 11, True,
         "unit S3 must not match an entry naming Part 4", "season_conflict"),
        (["My Hero Academia Season 7"], ["Boku no Hero Academia 7th Season"], 21,
         "FINISHED_AIRING", 7, 21, False, "MHA S7 vs '7th Season' stays allowed", ""),
        (["Gate: Jieitai Kanochi nite, Kaku Tatakaeri 2nd Season"],
         ["Gate: Jieitai Kanochi nite, Kaku Tatakaeri 2nd Season"], 12, "FINISHED_AIRING",
         2, 12, False, "Gate S2 vs '2nd Season' stays allowed", ""),
        (["Some Show"], ["Some Show Part 2"], 11, "FINISHED_AIRING", 3, 11, False,
         "an entry naming an EARLIER season stays a soft penalty (Plex/TMDB numbering drifts)", ""),
    ]
    for q, titles, eps, status, season, watched, want, label, why in cases:
        c = {"source": "anilist", "mal_id": 123456, "anilist_id": 1, "titles": titles,
             "episodes": eps, "fmt": "TV", "year": 2021, "status": status, "adult": False,
             "rank": 0, "mal_rank": None, "popularity": 1500}
        score, reasons, rej = matchmaking.score_candidate(c, q, 2021, season, watched,
                                                         set(), True, set())
        joined = ";".join(reasons)
        ok = rej == want and (not (rej and why) or why in joined)
        check(ok, label, f"want reject={want}{('+' + why) if why else ''} got {rej} "
                         f"score={score:.2f} {joined[:62]}")

    # MAL's spelling of "not yet aired" is not AniList's; both must be caught
    for status in ("NOT_YET_AIRD", "NOT_YET_AIRED", "ANNOUNCED", "NOT_YET_AIRING", "PRODUCTION"):
        c = {"source": "mal", "mal_id": 62564, "titles": ["Some Sequel"], "episodes": 0,
             "fmt": "TV", "year": 2026, "status": status, "adult": False, "rank": 0,
             "mal_rank": None, "popularity": 5000, "anilist_id": None}
        _, r, rej = matchmaking.score_candidate(c, ["Some Show"], 2026, 1, 25, set(), True, set())
        check(rej, f"un-aired status {status} is a hard reject")


# ---------------------------------------------------------------- B. audit
def _item(mid, want, action="bump", plex="Some Show", unit=0, status=None, source="override"):
    return {"mal_id": mid, "action": action, "new_watched": want, "source": source,
            "plex_show": plex, "plex_unit_total": unit, "new_status": status}


def test_audit():
    print("\nB. audit: MAL's own record vetoes a write\n")
    items = [
        _item(51535, 30, "new_entry", "Attack on Titan", 30),          # 2-episode entry
        _item(62564, 25, "new_entry", "The Rising of the Shield Hero", 25),   # not aired
        _item(56885, 10, "new_entry", "Frieren: Beyond Journey's End", 10, source="anilist"),
        _item(30654, 5),                                               # live has 22 -> decrease
        _item(59144, 12, "bump", "Even Given the Worthless", 12),      # same entry, twice
        _item(59144, 13, "bump", "Another season claiming it", 13),
        _item(999999, 10, "new_entry", "Invented Show", 10),           # MAL has no such id
        _item(40001, 12, "new_entry", "Some Doujinshi", 12),           # adult
        _item(5114, 6, "bump", "Fullmetal Alchemist: Brotherhood", 64, "watching"),  # legit
    ]
    m = FakeMAL()
    findings = audit.check_writes(CFG, m, _Quiet(), items, live=m.my_list(), search_check=False)
    blocks = {mid for sev, mid, _ in findings if sev == "block"}
    warns = {mid for sev, mid, _ in findings if sev == "warn"}
    for sev, mid, msg in findings:
        print(f"      {sev:<5} anime/{mid}: {msg}")
    for mid, why in [(51535, "write exceeds the entry's 2 episodes"),
                     (62564, "entry has not aired"),
                     (30654, "would decrease the live count"),
                     (59144, "one entry claimed by two Plex units"),
                     (999999, "MAL would not return the id"),
                     (40001, "adult entry with skip_adult on"),
                     (56885, "mini-anime found only through its synonym")]:
        check(mid in blocks, f"anime/{mid} blocked: {why}")
    check(5114 not in blocks, "anime/5114 (a correct write) is NOT blocked")
    check(56885 in blocks, "the Frieren mini-anime is caught by its English synonym alone")

    # a plan of one clean write must produce no findings at all
    clean = audit.check_writes(CFG, FakeMAL(), _Quiet(), [_item(5114, 6)],
                               live=FakeMAL().my_list(), search_check=False)
    check(not clean, "a single correct write produces no findings", str(clean))

    # nsfw is a colour word on this endpoint, not a boolean
    check(audit._nsfw("white")[0] is False, "nsfw='white' is not adult")
    check(audit._nsfw("pink")[0] is True, "nsfw='pink' is adult")
    check(audit._nsfw(True)[0] is True, "nsfw=True (other spellings) is adult")

    # an audit that cannot see alternative_titles is checking far less than it looks like
    try:
        audit.canonical(FakeMAL(), _Quiet(), 56885)
    except AssertionError:
        check(False, "audit asks MAL for the extra fields")
    else:
        check(True, "audit asks MAL for the extra fields")

    # a blocker has to be LOUD in the log, or it scrolls past on a long apply
    cap = _Capture()
    safe = audit.write_summary(cap, [("block", 123, "nope")])
    check(safe is False and any("BLOCKERS" in x for x in cap.err),
          "a blocker is logged as an error, not as information")
    cap2 = _Capture()
    check(audit.write_summary(cap2, [("warn", 123, "hmm")]) is True
          and not cap2.err, "a warning alone still allows the write")


def test_diff():
    print("\nC. audit: the after-the-fact proof that nothing else moved\n")
    before = {1: {"watched": 5, "status": "watching"},
              2: {"watched": 12, "status": "completed"},
              3: {"watched": 1, "status": "watching"}}
    after_ok = {1: {"watched": 6, "status": "watching"},
                2: {"watched": 12, "status": "completed"},
                3: {"watched": 1, "status": "watching"}}
    after_gone = {1: {"watched": 6, "status": "watching"},
                  3: {"watched": 1, "status": "watching"}}
    d = audit.diff(before, after_ok)
    check(d["added"] == [] and d["changed"] == [1] and d["removed"] == [],
          "diff separates added / changed / removed", str(d))
    check(d["untouched"] == 2, "diff counts untouched entries")
    check(audit.moved_ids(d) == {1}, "moved_ids is the set the plan must explain")

    log = _Capture()
    ok = audit.write_summary(log, [], d, expect_ids={1})
    check(ok is True and not log.err, "an apply whose writes explain every move passes",
          "".join(log.err)[:90])
    log2 = _Capture()
    audit.write_summary(log2, [], d, expect_ids=set())
    check("NOT part of the plan" in "".join(log2.err),
          "an entry that moved without being planned is an error")
    d2 = audit.diff(before, after_gone)
    log3 = _Capture()
    audit.write_summary(log3, [], d2, expect_ids={1, 2})
    check(d2["removed"] == [2] and "disappeared" in "".join(log3.err),
          "an entry vanishing from the list is an error")
    log4 = _Capture()
    check(audit.write_summary(log4, [("block", 42, "nope")], d, expect_ids={1}) is False,
          "a blocker means apply is refused")


def test_overrides():
    print("\nD. overrides: [covered] keys match the units build_plan produces\n")
    ov = {"covered": ["Attack on Titan|S4", "86|ALL", "Kill La Kill"]}
    check(engine._covered(ov, "Attack on Titan", 4), "an exact 'Show|S4' key matches")
    check(not engine._covered(ov, "Attack on Titan", 5), "the next season is still planned")
    check(engine._covered(ov, "86", 2), "'|ALL' matches every season")
    check(engine._covered(ov, "kill la kill", None), "a bare title matches the single-unit case")
    check(not engine._covered(ov, "Kill la Kill: Recap", None), "a different title does not match")


# ---------------------------------------------------------------- online pass
def test_online(cfg, log):
    print("\nE. the same rules against the real MAL API (read-only)\n")
    try:
        mal = __import__("plexmal.malapi", fromlist=["malapi"]).MAL(cfg, log)
        live = mal.my_list()
    except Exception as e:                                      # noqa: BLE001
        print(f"  SKIP live MAL list unavailable: {e}")
        return
    print(f"  live MAL entries: {len(live)}")
    items = [_item(51535, 30, "new_entry", "Attack on Titan", 30),
             _item(62564, 25, "new_entry", "The Rising of the Shield Hero", 25),
             _item(56885, 10, "new_entry", "Frieren: Beyond Journey's End", 10, source="anilist"),
             _item(999999, 10, "new_entry", "Invented Show", 10)]
    findings = audit.check_writes(cfg, mal, log, items, live=live)
    for sev, mid, msg in findings:
        print(f"      {sev:<5} anime/{mid}: {msg}")
    blocks = {mid for sev, mid, _ in findings if sev == "block"}
    for mid, why in [(51535, "write exceeds its 2 episodes"),
                     (62564, "has not aired"),
                     (999999, "MAL has no such entry"),
                     (56885, "mini-anime, matched without a human")]:
        check(mid in blocks, f"live: anime/{mid} blocked ({why})")


class _Quiet:
    """Log stand-in: audit logs progress, tests do not want it."""

    def info(self, *a, **k):
        pass

    warn = error = debug = step = info


class _Capture:
    def __init__(self):
        self.err, self.out = [], []

    def info(self, m, *a, **k):
        self.out.append(str(m))

    warn = info

    def error(self, m, *a, **k):
        self.err.append(str(m))

    debug = step = error


def run(cfg, log, online=False):
    """Run every check. Returns the number of failures."""
    global FAILURES
    FAILURES = []
    test_season_guard()
    test_audit()
    test_diff()
    test_overrides()
    if online:
        test_online(cfg, log)
    print("\n" + "=" * 72)
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED")
        for f in FAILURES:
            print("  " + f)
    else:
        print("ALL CHECKS PASSED")
    return len(FAILURES)
