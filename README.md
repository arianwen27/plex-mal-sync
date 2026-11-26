# plex-mal-sync

Keeps your **MyAnimeList** anime list in step with what you actually watch on your
**own Plex Media Server** — so you stop hand-editing the website after every episode.

It reads Plex, decides which episodes you have *properly* finished, maps each show
to the right MyAnimeList entry, shows you a **plan**, and only then writes — a few
fields at a time, never a bulk replace.

```
Plex (read-only)            matching                        MyAnimeList
┌──────────────────┐   ┌──────────────────────┐   ┌───────────────────────┐
│ library DB  (ro) │   │ AniList (primary)     │   │ API v2, OAuth2 + PKCE │
│ + HTTP API       ├──►│ Shikimori / Kitsu     ├──►│ PATCH one entry       │
│ watch history    │   │ cross-checks + scores │   │ status / episodes     │
└──────────────────┘   └──────────┬───────────┘   └───────────────────────┘
                                  │
                        plan + report first
                     anything uncertain -> review queue
```

## Why it looks the way it does

* **Plex is only ever read.** The SQLite library is opened `mode=ro` and the HTTP API
  is only `GET`. No Plex config, plugin, scanner, agent or file is modified.
* **Watched = finished, not started.** An episode counts when Plex records a completed
  play. A half-watched episode stays uncounted (same rule the Plex badge uses).
* **Two sources, unioned.** Plex's history table and its per-episode play counts each
  have blind spots on real libraries; an episode counts if *either* records a completed
  watch. Both numbers are shown in the report so discrepancies are visible, not silent.
* **Never decreases.** A write can only raise `num_watched_episodes`. Lowering, deleting,
  re-scoring or touching entries Plex has never heard of is refused by the code, not by config.
* **Uncertain matches are never guessed at.** If the title resolver is not clearly right,
  the show lands in a `NEEDS REVIEW` section of the report and nothing is written for it.
* **A season cannot be filed against the wrong sequel.** A MAL entry whose name points at a
  *later* season than the Plex unit it would receive is refused outright — that is what
  nearly sent Shield Hero Season 1's 25 watches to *Season 5*, which has not aired.
* **Every write is checked from MAL's side before it happens, and audited after.** See
  [The audit](#the-audit).
* **Backups and undo.** Your whole live MAL list is snapshotted before any write, and every
  write is logged with its previous value so it can be reverted.

## Requirements

* Python 3.11+ (stdlib only — no pip installs)
* A Plex Media Server on this machine (or reachable over the LAN)
* A MyAnimeList API app: <https://myanimelist.net/apiconfg> → *Create ID*

## Setup

```bash
git clone https://github.com/<you>/plex-mal-sync.git
cd plex-mal-sync
copy config.example.toml config.toml      # (cp on Linux/macOS)
python plex_mal.py sections               # proves Plex is readable
python plex_mal.py login                  # browser authorisation, tokens stay local
python plex_mal.py scan                   # journal Plex watch history into data/
python plex_mal.py resolve                # title -> MAL id mapping
python plex_mal.py plan                   # WHAT WOULD BE WRITTEN (writes nothing)
python plex_mal.py apply --confirm <hash> # the only command that writes
python plex_mal.py audit --backup auto    # afterwards: prove exactly what moved
```

### `config.toml` essentials

| Key | Meaning |
|---|---|
| `[plex] db_path` | `.../Plug-in Support/Databases/com.plexapp.plugins.library.db` |
| `[plex] libraries` | only these Plex libraries are considered (e.g. `['Anime']`) |
| `[plex] accounts` | which Plex users count as *you* — shared accounts are ignored by default |
| `[plex] token` | blank is fine; the local admin token is auto-detected |
| `[mal] client_id` / `client_secret` | from your MAL API app |
| `[mal] export_path` | optional MAL *Export Data* file, used as the read side for offline diffs |
| `[sync] …` | the safety rails (caps, minimum confidence, never-decrease, etc.) |

## The safety rails

| Rail | Default | Effect |
|---|---|---|
| `never_decrease` | `true` | refuses any write that would lower a watched count |
| `min_confidence` | `0.75` | below this the show is only reported, never written |
| `ambiguity_margin` | `0.08` | if a runner-up match is close, it goes to review |
| `max_new_entries_per_run` | `15` | cap on brand-new list entries per run |
| `max_episode_bump` | `26` | a single jump bigger than this needs review |
| `max_writes_per_run` | `25` | hard cap on API writes per run |
| episode-count cap | always on | never sets watched above an entry's total episode count |
| `status_policy` | `conservative` | only `plan_to_watch`/absent → `watching`/`completed`; your `on_hold`/`dropped` stay yours |

`apply` also requires the plan's hash, which changes if the plan content changes — so a
report you have actually read is the thing being approved.

## The audit

`plan` reasons from titles and episode counts. `audit` ignores all of that and asks
MyAnimeList instead: what is this entry *really* called, how many episodes does it have,
has it aired, is it adult — and after a write, what does your list actually contain now.

```bash
python plex_mal.py audit                    # newest saved plan. writes nothing
python plex_mal.py audit --plan reports/plan_xxx.json
python plex_mal.py audit --backup auto      # afterwards: pre-apply backup vs live list
```

It prints **blockers** (apply must not run) and **warnings** (worth a glance):

| Blocker | Why it exists |
|---|---|
| entry has fewer episodes than the write | 51535 *Kanketsu-hen* has 2 episodes; the 30-episode Plex "specials" unit that contains them is recorded elsewhere |
| entry has not aired yet | 62564 *Shield Hero Season 5* (`not_yet_aired`, no episode count) was the matcher's pick for Season 1's 25 watches |
| entry looks like a mini / preview / recap | 56885 is a Frieren *Mini Anime*; the romaji title hides it — only MAL's English synonym gives it away |
| one MAL entry claimed by two Plex units | Danganronpa and Assassination Classroom both wanted the same entry. One write per entry is the only thing provable afterwards |
| the write would lower a watched count | belt and braces on top of `never_decrease` |
| MAL would not return the entry at all | a stale or invented id cannot be checked, so it is not written |
| adult entry while `skip_adult` is on | |

| Warning | Meaning |
|---|---|
| no episode count on MAL | still airing — the write cannot be capped |
| small-media type receiving many episodes | an ONA/OVA taking 10+ watches: right show, wrong entry? |
| already accurate | the write changes nothing |
| Plex unit and MAL entry disagree on episode count | season grouping differs; read it before approving |
| MAL's own search does not return the entry | the match came from AniList/Shikimori only |

`apply` runs the same check **after** the backup and **before** the first write, and refuses
to write a single field if anything is blocked. Afterwards it re-reads the list and proves
that nothing outside the plan moved:

```
audit: list 199 -> 208 entries, +9 new, 2 changed, 0 removed, 197 untouched
audit: nothing outside the planned writes moved
```

`apply --skip-audit` exists for when MAL is unreachable. It skips precisely the check that
stops a write aimed at the wrong entry, so use it knowingly.

### Machine-readable output

Everything a UI or a script needs is documented in [HANDOVER.md](HANDOVER.md). In short:

```bash
python plex_mal.py status --json      # health: Plex reachable? MAL authorised? when does it expire?
python plex_mal.py plan --json        # the plan itself (same object as data/reports/*.json)
python plex_mal.py audit --json       # findings + the before/after list diff
python plex_mal.py selftest --json    # safety rails: {ok, checks, failures[]}
python check_json.py                  # asserts this contract (stdout one JSON object, logs on stderr)
```

With `--json`, **stdout is exactly one JSON object** and every progress/diagnostic line goes
to **stderr** — so a caller never has to parse a table, and a failure still says why on stderr
and exits non-zero (`3` = the plan changed since you read it, `2` = can't run, `1` = a problem
was found). `apply` needs no flag: its stdout is always JSON. `status --json` reports only
*whether* the keys and tokens exist, never their values.

## Checking the code itself

```bash
python plex_mal.py selftest           # offline: no Plex, no MAL, no writes
python plex_mal.py selftest --online  # then the same rules against the real API
```

The fixtures are copies of the entries this project actually aimed a write at by mistake, so
if a rule is loosened later the suite fails loudly instead of quietly wrecking a list. Run
it after touching `matchmaking.py`, `audit.py`, `engine.py` or `config.py`.

## Multi-season shows

MAL splits seasons into separate entries; Plex groups them under one show (and can group
several *series* under one title). The resolver follows AniList's sequel chain to line
seasons up, and anything that does not line up cleanly is reported instead of guessed.
Long-running shows that MAL tracks as a single entry can be pinned in `overrides.toml`:

```toml
[mappings]
"One Piece" = 21           # whole show -> one MAL entry
"Re:Zero|S2" = 48029       # one specific season

[single_entry]
shows = ["One Piece"]

[ignore]
shows = ["Some Show I Never Want On MAL"]

[covered]
units = ["86|ALL", "Beastars|S3"]   # already on MAL, just split differently
```

`[covered]` is for the case where MAL splits a season into more entries than Plex has
folders: your Plex "Season 1" of 86 is 23 episodes because it is MAL's *86* (11) plus
*86 Part 2* (12). Neither entry can take 23 watches, so the unit would sit in the review
queue forever and hide the ones that really need a decision. Marking it covered skips it
with a reason in the report - per season, so a new season is still picked up later.

A season-level key (`|S<n>`) is only ever consulted for a Plex unit that MAL should map
to one entry; whole-show keys are the bare title or `|ALL`.

If your library genuinely numbers a show's second season as `S1` (some people keep each
cour in its own folder), the later-season rule will refuse every candidate and the unit
lands in the review queue — pin it with a `[mappings]` entry instead of loosening the rule.

## Limits worth knowing

* The scan caches each show so repeat runs are fast. The cache is only reused when the
  show's `lastViewedAt`, its episode count *and* a fingerprint of your recorded plays
  all match, so marking episodes unwatched or moving them into the specials season in
  Plex is picked up automatically. `plan --force-scan` re-reads everything regardless.

* Title → MAL matching is heuristic. It is good, not perfect: `overrides.toml` exists
  because ambiguous names (`86`, seasonal title variants, anime with three names) happen.
* MAL's API has no "delete list entry" operation, so `undo` restores previous values
  rather than removing entries.
* Score, rewatch count, start/finish dates and notes are **never** written — out of respect
  for the list you already curated.
* Plex only records plays made through Plex; a file played in VLC stays invisible to everything.

## Privacy

Everything stays on your machine. `config.toml` (your keys), `overrides.toml` (your shows),
and `data/` (watch journal, MAL list snapshots, tokens, logs, reports) are git-ignored —
this repository contains code and documentation only.

## Licence

MIT
