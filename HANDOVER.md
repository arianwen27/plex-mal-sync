# HANDOVER — for whoever builds the UI on top of plex-mal-sync

Written 2026-11-26 by the agent that built this. You have no memory of that work, so read
this before writing integration code. Every number here was measured on the live machine or
read out of the code — nothing is assumed. If you find a claim that no longer holds, change
this file and say what you found.

Sibling project: `../plex-gap` (a Plex → TVmaze "what's missing" scanner). Its `HANDOVER.md`
documents the same subprocess conventions. This file has a
[section on hosting both](#if-the-gui-also-hosts-plex-gap).

---

## What this is

One-way sync: **what you finished watching on your own Plex Media Server → your MyAnimeList
list.** It never writes to Plex. It only ever PATCHes `num_watched_episodes` and `status` on
your own MAL list entries — never scores, dates, notes, rewatch counts, or bulk replaces.

Live on this machine right now:

| | |
|---|---|
| Plex library in scope | `Anime` only (the server has four sections; the film section, the TV section and the adult-anime section are deliberately excluded in `config.toml`) |
| Plex watch history | 1,081 completed-play rows → 972 distinct episodes; 129 shows known, 109 with completed episodes, **2,309 completed episodes** |
| MyAnimeList list | 208 entries (was 199 before the one apply so far) |
| Writes performed | one `apply`: 11 writes, 0 failed, 0 skipped (2026-11-25 22:53:54 → 22:54:17) |
| Python | 3.11+, **stdlib only** — no venv, no pip installs |
| Safety self-test | `python plex_mal.py selftest` → 41 offline assertions, all pass |
| Contract self-test | `python check_json.py` → subprocess/stream contract of the JSON mode, all pass |

Two facts that shape everything:

1. **The blast radius is the user's whole MAL history.** A bug here silently rewrites
   hundreds of entries. Every design decision below follows from that.
2. **Title → MAL entry matching is heuristic.** It is good, not perfect, and the tool's
   honest answer to uncertainty is "report it, write nothing".

## Hard rules (violating these breaks the setup or the user's trust)

1. **Only `apply` (and `undo --yes`) write to MyAnimeList.** Every other command is
   read-only, including `plan`, `audit`, `selftest`, `status`, `sections`, `scan`,
   `resolve`. Never invent a write path.
2. **Never auto-apply. Never use `apply --force`.** `apply` requires `--confirm <hash>`,
   where the hash is a SHA-256 (first 12 hex) of *exactly* the write set: `mal_id`,
   `action`, `new_watched`, `new_status`, `plex_show`, `season`. A UI must render the plan
   it fetched and pass that plan's hash back. If anything changed meanwhile (new plays, a
   mapping change) the hash differs and apply refuses with exit 3 and writes nothing —
   **that is the safety property, not an obstacle.** `--force` skips the check. Don't.
3. **Never pass `--skip-audit`.** That flag disables the pre-write check that re-reads every
   target entry from MAL and refuses writes that are aimed at the wrong entry. It has
   already caught real disasters (details in *Sharp edges*). If the user demands it, log it
   loudly: the tool prints `AUDIT SKIPPED` as a warning.
4. **Never print, render, log, embed or commit:** the MAL `client_secret`, the MAL
   access/refresh tokens, the MAL username, the MAL user id, the Plex admin token. They live
   in `config.toml` and `data/mal_tokens.json`, both git-ignored. `status --json` therefore
   reports only `client_id_set` / `client_secret_set` / `authorised` booleans + expiry — no
   values. If a UI needs to *edit* the credentials, offer write-only inputs (masked, never
   read back from the file).
5. **Plex stays read-only.** SQLite is opened `file:...?mode=ro` and the HTTP API is
   `GET`-only. No config, plugin, scanner, agent, or file on the Plex side is ever touched.
   If a feature seems to need a Plex write, stop and ask the user first — that is an
   explicit standing instruction.
6. **`on_hold` belongs to the user.** The tool never moves an entry out of `on_hold`
   (episode counts still bump). Opt-in only via `[sync] promote_on_hold = true`. Don't
   build a "tidy up my statuses" button.
7. **`overrides.toml` is hand-verified data, not scratch space.** It currently holds 22
   mappings, 2 ignores and 15 covered units, each with a comment explaining *why*. If the UI
   edits it, append only what the user explicitly chose and preserve the comments. Never
   regenerate the file, never reorder it.
8. **One apply at a time, and don't overlap MAL work.** MAL calls are throttled
   (`request_gap_seconds = 1.5`, matcher calls `resolve_gap_seconds = 2.6`; AniList 429s
   hard). A UI needs a single in-flight job per backend and a visible queue, not fan-out.
9. **Never `git clean -xdf`.** `config.toml`, `overrides.toml` and `data/` are ignored but
   essential: tokens, the watch journal, the matcher cache, backups, reports.
10. **Keep `data/backups/`.** Every apply snapshots the entire list first
    (`mal_list_<stamp>.json`). `undo` restores recorded previous values, but **MAL has no
    delete-entry API**, so entries the tool created are set to 0 episodes rather than
    removed. Backups are the real rollback.

## Two ways to call it

### A. Subprocess with `--json` (recommended: fully decoupled)

Stream discipline, identical in both sibling projects:

* **stdout is exactly one JSON object**, nothing else (for `apply`, stdout is JSON even
  without a flag).
* **every diagnostic, progress line, warning and blocker goes to stderr.** Format:
  `[HH:MM:SS] LEVEL message`.
* Non-zero exit ⇒ the reason is on stderr; `--json` commands also emit
  `{"ok": false, "command": ..., "error": ...}` on stdout so a caller never has to guess.
* Windows: the CLI forces UTF-8 on both streams (`_utf8()`), but read subprocess output as
  UTF-8 explicitly. Titles include Japanese/romaji characters.

```python
import json, subprocess, sys
p = subprocess.run([sys.executable, "plex_mal.py", "plan", "--json"],
                   capture_output=True, text=True, encoding="utf-8", timeout=600)
plan = json.loads(p.stdout)          # p.stderr has the progress log
```

`python check_json.py` asserts all of the above and is the thing to re-run after any change
to `cli.py` or `util.py`.

### B. Import the package (in-process, `src/` layout — no installer)

```python
import sys; sys.path.insert(0, r"<repo>\src")
from plexmal.config import Cfg
from plexmal.store import Store
from plexmal.util import Log
from plexmal import engine, audit, malapi

cfg, log = Cfg(r"<repo>\config.toml"), Log(r"<repo>\data\logs\gui.log")
store = Store(cfg.cache_db)
facts = engine.scan(cfg, store, log)                  # Plex (read-only) -> facts
entries, src = engine.load_mal_entries(cfg, store, log, live=True)   # MAL live list
plan = engine.build_plan(cfg, store, log, facts, entries, do_resolve=True)
plan["hash"]                                          # pass this to apply
res = engine.apply_plan(cfg, store, log, plan, confirm=plan["hash"], mal=None)
```

| call | what |
|---|---|
| `engine.scan(cfg, store, log, force=False)` | Plex DB + API → per-show facts (cached, fingerprint-checked) |
| `engine.load_mal_entries(cfg, store, log, export=None, live=True)` | your live MAL list `{mal_id: entry}`; `export=` reads a MAL "Export Data" file instead (offline) |
| `engine.build_plan(..., do_resolve=, show_noop=)` | the plan dict (see contract) |
| `engine.plan_hash(plan)` | the 12-hex confirmation token |
| `engine.apply_plan(..., confirm=, skip_audit=)` | backup → audit → PATCH → re-read verify → diff proof |
| `engine.undo_last(cfg, store, log, dry=True)` | roll back the last apply (`dry=False` writes) |
| `audit.check_writes(cfg, mal, log, items, live=)` | `[(severity, mal_id, msg)]`, `severity in ("block","warn")` |
| `audit.diff(before, after)` / `audit.moved_ids(d)` | snapshot comparison |
| `malapi.MAL(cfg, log).my_list()` / `.anime(id, fields=)` / `.backup(note=)` | reads; `update()` is the only write |

Both routes are safe to call concurrently with *plex-gap*, but keep **one job per backend**
(see rule 8) and never two `apply`s.

## Command surface

| command | writes to MAL | needs Plex | needs MAL auth | `--json` | measured wall time |
|---|---|---|---|---|---|
| `status` | no | db + api | no (reports it) | yes | **0.2 s** |
| `sections` | no | db + api | no | yes | **0.2 s** |
| `selftest` | no | no | no (only with `--online`) | yes | **0.2 s** (offline, 41 checks) |
| `scan` | no | db + api | no | no | ~1 s warm, longer cold |
| `plan` | no | db + api | **yes** (list is the baseline) | yes | **3.6 s** warm |
| `audit [--plan F] [--backup auto]` | no | no | **yes** | yes | **3.4 s** for a 0-write plan; add ~0.3 s per planned write (MAL's copy of the entry) plus ~1.2 s for each non-override write's search cross-check |
| `apply --confirm <hash>` | **YES** | db + api | **yes** | stdout is always JSON | **23 s** for 11 writes, **28 s** for 15 (incl. backup, audit, re-read) |
| `undo` | dry run by default; **`--yes` writes** | no | for `--yes` | no | ~1 s + 1.5 s/write |
| `resolve` | no | db + api | no | no | slow: 2.6 s per candidate looked up |
| `reports` | no | no | no | no | <0.1 s |
| `login` | no | no | interactive (browser + `localhost:8765`) | no | human-paced |
| `watch [--apply]` | with `--apply`, **YES** | db + api | **yes** | no | loop; `--once` for one pass |

**Exit codes:** `0` fine · `1` a problem was *found* (apply had failures / `audit` saw
blockers / `selftest` failed) · `2` couldn't run (Plex unreachable, db missing, not
authorised, plan/backup file unreadable) · `3` **plan hash mismatch, nothing written** ·
`130` interrupted (Ctrl-C; nothing mid-write is left half-applied except that MAL had
already received earlier writes of that run — which is exactly why backups exist).

`status`, `sections`, `plan`, `audit`, `selftest` accept `--json`.
`apply` needs no flag: stdout is always one JSON object; its human explanation (including
the whole re-rendered plan table on a hash mismatch) goes to stderr.

## The plan contract — the important one

`plan --json` and the saved `data/reports/plan_<stamp>_<hash>.json` are the **same object**.
`plan` writes both a `.md` (for a human) and `.json` every time, so a UI can render the
markdown straight into a webview.

```jsonc
{
  "ok": true, "command": "plan",
  "created": "2026-11-26T18:45:13",
  "hash": "4f53cda18c2b",              // pass to apply --confirm. Write-set only.
  "mal_source": "mal_api",             // or "export:<path>" when reading a MAL data export
  "mal_entries": 208, "plex_show_facts": 129,
  "next": "apply --confirm 4f53cda18c2b",   // or "nothing to write"
  "stats": { "writes": 0, "new_entries": 0, "bumps": 0, "status_only": 0,
             "review": 0, "blocked": 0, "noop": 0 },
  "report_md":  "C:\\...\\data\\reports\\plan_...md",
  "report_json":"C:\\...\\data\\reports\\plan_...json",

  "items":   [ /* WRITE THESE. Exactly what apply will PATCH. */ ],
  "review":  [ /* needs a human decision; nothing will be written */ ],
  "blocked": [ /* refused by a rail; nothing will be written */ ],
  "skipped": [ { "plex_show": "86", "why": "marked [covered] in overrides: already recorded on MAL" } ],
  "noop":    [ /* only present with --show-noop: already in sync */ ]
}
```

**The four buckets are the UI's whole job.** `items` → "here is what I will change",
`review` → "pick one of these / add an override", `blocked` → "refused on purpose, read the
reason", `skipped` → "deliberately not touched". Do not merge them, and never label the
count of `items` as "missing shows".

An **item** (all keys always present; `null` means unknown, never a guess):

```jsonc
{
  "mkey": "rising shield hero|S1",     // norm_title(plex title) + "|S<season>", or "|ALL" for a single-unit show
  "gp_guid": "plex://show/...",        // stable Plex guid — use as your row id
  "plex_show": "The Rising of the Shield Hero",
  "plex_year": 2019, "season": 1,
  "structure": "api+db",               // how the counts were derived: "api" (Plex API only) or "api+db" (both agree to look at)
  "plex_watched": 25,          // what Plex proves finished (union of history table + API play counts)
  "plex_unit_total": 25,       // episodes in that Plex unit
  "in_progress": [ { "season": 1, "episode": 1, "title": "Sophist", "pct": 11 } ],  // NEVER counted, NEVER written
  "mal_id": 35790, "mal_title": "Tate no Yuusha no Nariagari",
  "total_episodes": 25,        // MAL's episode count for that entry
  "cur_watched": 12, "cur_status": "watching",   // your LIST right now
  "new_watched": 25, "new_status": "completed",  // what apply will send (null = field untouched)
  "action": "bump",            // new_entry | bump | status_only  (noop only with --show-noop)
  "source": "override",        // override | manual | auto | anilist | shikimori | mal
  "confidence": 1.0,           // override=1.0; auto matches must clear min_confidence (0.75)
  "flags": [ /* human reasons — see below */ ]
}
```

Real `flags` strings, so you can style them rather than re-derive them:

* `"MAL entry does not exist and [sync] allow_new_entries=false"`
* `"Plex counts 30 episodes but this MAL entry has only 2 ..."`
* `"Plex shows 12, MAL already has 25 - keeping 25 (never decrease)"`
* `"single jump of 41 episodes exceeds max_episode_bump"`
* `"over max_new_entries_per_run - deferred to a later run"`
* `"was Plan to Watch -> Currently Watching (Plex has watched episodes)"`
* `"no MAL id known (run `resolve` or add an override)"` ← the usual `review` reason
* when a show resolves ambiguously, the reason carries the runner-ups:
  `"... | best guess 22319 Tokyo Ghoul √A (0.81) | also: 28907 Gate: ... (0.79)"`

**Hash semantics, which people get wrong:** the hash covers **only** `items`. Adding a
review row, a blocked row or a skip does **not** change it. A hash is therefore a
commitment to a *write set*, which is exactly what you want to make a human approve. If your
UI needs "the situation changed" detection, diff the JSON — do not rely on the hash.

## The audit contract

`audit` ignores everything the matcher believed and asks MAL instead. It is **read-only** and
it is also run automatically inside `apply`, after the backup and before the first PATCH.

```jsonc
{
  "ok": true, "command": "audit",
  "plan_file": "C:\\...\\plan_...json", "plan_hash": "4f53cda18c2b",
  "writes": 11, "live_entries": 208,
  "blocked": false,                       // true => apply would refuse this plan. exit code 1.
  "findings": [ { "severity": "block" | "warn", "mal_id": 62564, "message": "entry has not aired (NOT_YET_AIRED) but the plan files 25 watches against it (Tate no Yuusha no Nariagari Season 5 (tv, 0 eps, NOT_YET_AIRED))" } ],
  "backup": { "path": "C:\\...\\mal_list_...json", "taken_at": 1795710401, "entries": 199 },
  "diff":   { "before": 199, "after": 208, "untouched": 197,
              "added":   [ { "mal_id": 5114, "title": "Fullmetal Alchemist: Brotherhood",
                             "was_watched": null, "was_status": null,
                             "watched": 6, "status": "on_hold" } ],
              "changed": [ { "mal_id": 13125, "title": "Shinsekai yori",
                             "was_watched": 9, "was_status": "on_hold",
                             "watched": 10, "status": "on_hold" } ],
              "removed": [] },
  "unexplained": []                        // ids that moved but are NOT in the plan ⇒ bug or a third party editing the list
}
```

Blockers (each of these has a real near-miss behind it):
write exceeds the entry's episode count · entry has not aired · one entry claimed by two
Plex units · write would lower a watched count · adult entry while `skip_adult` is on ·
MAL returns nothing for that id · entry looks like a mini/preview/recap and was not matched
by a human.
Warnings: no episode count (still airing) · ONA/OVA receiving ≥10 watches · already accurate ·
Plex unit and MAL entry disagree on episode count · MAL's own search doesn't return the id.

UI advice: **render `diff` after every apply** — "+9 new, 2 changed, 0 removed, 197
untouched" is the sentence that tells the user their list is fine. `removed` non-empty or
`unexplained` non-empty must be a red banner.

## apply contract

`apply` does, in this order — a UI can narrate exactly this:

1. scan Plex (cached) → build plan → **compare hash**; mismatch ⇒ exit 3, nothing written.
2. **Back up the whole live list** to `data/backups/mal_list_<stamp>.json`.
3. **Audit** every write against MAL. Any blocker ⇒ `SystemExit` ⇒ nothing written, backup
   path quoted in the message.
4. PATCH each item (`num_watched_episodes`, `status`), honouring `--max-calls`, never
   decreasing, skipping entries whose live count MAL didn't report.
5. **Re-read the list** and prove every write landed as asked.
6. Print the **diff** proof: nothing outside the write set moved.

Success (measured with a 0-write plan, so `"written": 0`; that is the shape):

```jsonc
{ "written": 0, "failed": 0, "ok": true, "command": "apply", "plan_hash": "4f53cda18c2b",
  "report_md": "...", "report_json": "..." }
```
plus `"skipped"`, `"backup"`, `"run_id"` whenever there was something to write.

Hash mismatch ⇒ **exit 3**:
```jsonc
{ "ok": false, "command": "apply", "written": 0, "error": "hash_mismatch",
  "reason": "the plan changed after it was read; nothing was written",
  "asked_for": "deadbeef0000", "new_hash": "4f53cda18c2b", "stats": { ... },
  "plan_json": "...", "report_md": "...",
  "next": "plan --no-resolve --json  (then apply --confirm 4f53cda18c2b)" }
```

Refused by the audit (or no token) ⇒ **exit 1**:
`{"ok": false, "command": "apply", "written": 0, "error": "refused", "reason": "apply refused: the plan does not survive the audit against MyAnimeList's own data (blockers listed above). Nothing was written. Backup of your list: ...", "plan_hash": "...", "stats": {...}}`

The refusal path was proven against the live list with a write the *engine's own*
never-decrease guard would also have skipped, so it could not have changed anything even
with the audit switched off — that probe is described in the commit that added the audit
(`82c5ac7`, `92fe1d4`).

Real apply log (stderr), for reference:

```
backing up your live MAL list before writing 11 change(s)
MAL: backed up 199 list entries -> mal_list_20261125_225356.json
audit: clean - every write matches MAL's own record of the entry
  ok  anime/30654 Ansatsu Kyoushitsu 2nd Season 0->25 eps /completed
verify ok: re-read 208 entries, all 11 write(s) landed exactly as planned
audit: list 199 -> 208 entries, +9 new, 2 changed, 0 removed, 197 untouched
audit: nothing outside the planned writes moved
apply done: 11 written, 0 failed, 0 skipped. backup: ...
```

## Files

| path | what | disposable? |
|---|---|---|
| `plex_mal.py` | entry point (`python plex_mal.py <cmd>`) | no |
| `src/plexmal/engine.py` | scan → plan → apply → undo | no |
| `src/plexmal/audit.py` | the pre/post-write checks | no |
| `src/plexmal/matchmaking.py` | Plex title → MAL id resolution + scoring guards | no |
| `src/plexmal/malapi.py` | OAuth2 PKCE + API v2 client | no |
| `src/plexmal/selftest.py` | 41 offline safety assertions | no |
| `check_json.py` | subprocess/stream contract test | no |
| `README.md` | user-facing docs | no |
| `config.toml` | **private**: paths, Plex url/token, MAL client id+secret, all rails | NEVER commit |
| `overrides.toml` | **private**: hand-verified mappings, ignores, covered units | NEVER commit |
| `data/cache.sqlite` | watch journal, mappings, MAL cache, `applied` log (undo source) | precious |
| `data/mal_tokens.json` | **private**: access+refresh tokens | NEVER commit |
| `data/mal_oauth_pending.json` | PKCE verifier+state of an in-flight login | transient |
| `data/backups/mal_list_*.json` | whole-list snapshots, one per apply | keep forever |
| `data/reports/plan_*.md/.json` | every plan ever built (`{plan,hash}`) | keep: audit trail |
| `data/logs/run.log` | timestamped log of everything | keep |

## Performance (measured, plan the UX around it)

| operation | wall time on this machine |
|---|---|
| `status` / `sections` / `selftest` | **0.2 s** each |
| `plan --json` (warm caches, all shows mapped) | **3.6 s** |
| `audit --json` (0-write plan; = one live list read) | **3.4 s** |
| `apply`, 11 writes | **23 s** |
| `apply`, 15 writes | **28 s** |
| a show needing a cold title resolve | ~2.6 s **per candidate looked up** |
| `selftest --online` (re-reads MAL) | ~15 s |

Notes: `apply`'s time is dominated by the 1.5 s MAL request gap plus backup + verification —
it is deliberate, don't "optimise" it. The scan cache reuses a show's facts only when its
`lastViewedAt`, episode count **and** a fingerprint of the recorded plays all match, so
unwatching a season or moving episodes into Specials in Plex *is* picked up automatically;
`plan --force-scan` overrides. Run everything asynchronously with a progress indicator, and
don't poll faster than every few minutes.

## Sharp edges — all of these are real, observed on this library

**MyAnimeList API asymmetries** (each one cost a bug):
* Read your list with field `my_list_status`. `list_status{...}` does **not** exist and
  silently returns `watched: null` for every entry — which defeats *both* never-decrease
  guards. `my_list()` raises rather than guess zeros if the field vanishes; keep that.
* The list **read** field is `num_episodes_watched`; the **write** field is
  `num_watched_episodes`. Different spellings.
* `/anime/{id}` returns a *stub* unless you pass `fields=`. No `alternative_titles`, no
  `synonyms`, no `nsfw` ⇒ an audit that looks thorough isn't. Use `MAL.AUDIT_FIELDS`.
* `nsfw` on that endpoint is a **colour string** (`"white"` for a normal entry).
  `bool("white")` is `True` — treating it as a flag blocked every single write. Use
  `audit._nsfw()`, which only counts `pink`/`red`/`true`/`1`/`adult`/`17+`.
* Un-aired status: MAL writes `NOT_YET_AIRED`, AniList writes `NOT_YET_AIRD`. Both must be
  checked (missing the first let "Shield Hero Season 5" become a planned 25-watch write).
* MAL search rejects `q` shorter than 3 characters with HTTP 400 `invalid q` — so a show
  called *86* cannot be searched directly.
* There is **no delete-entry** API. `undo` sets counts back; it cannot make a created entry
  disappear.
* `update()` is PATCH (with a PUT fallback). Writes are idempotent per field.

**This library:**
* `status`/`sections` `--json` payloads include **every Plex account name on the server**
  (33 here) — that's for setup help. Render locally only; never send it anywhere.
* 129 shows are known but only 109 have completed episodes; `skipped` rows that say
  *"watch history exists but the show is no longer in the Plex library"* are **not errors**
  — the history outlived the library entry. Don't show them as missing data.
* `resolve` saves mappings without consulting `overrides.toml`, so cached mapping rows can
  look contradictory (e.g. a Tokyo Ghoul cours showing `needs_review 22319` while planning
  uses the override **37799**). Planning is right; the `mappings` table is not a UI source.
  **Build the UI on `plan`, never on `store.mappings()`.** That table currently holds 171
  rows: 140 `auto`, 22 `needs_review`, 9 `ignored` — a cache of matcher guesses, not decisions.
* A MAL entry that names a **later** season than the Plex unit is refused outright
  (`season_conflict`). A unit that genuinely numbers seasons differently than MAL lands in
  `review`, not in a wrong write. The fix is an override, never loosening the guard.
* MAL and Plex disagree about what one "episode" is for some shows (recap specials, ONAs,
  PVs). `plex_unit_total` vs `total_episodes` mismatches are warnings, not bugs — e.g. Kill
  la Kill's 25th Plex file is the recap special; Danmachi S1's 14th is an OVA.
* Plex "in progress" episodes are never counted and never written; that matches the Plex UI
  badge (fully watched counts, half-watched doesn't).
* `login` is interactive: browser → `http://localhost:8765/callback`. If the browser resolves
  `localhost` to `::1` the local listener can miss the redirect; the fallbacks exist for
  exactly that — `login --url-only` then `login --paste "<url>"`, or
  `login --paste-file <path>` (a file avoids shell quoting entirely, which bit us twice).
  A UI should shell out to a real console for login rather than try to embed the flow.
* `[sync] skip_adult = true` means adult-tagged shows are *never* added. A user asking
  "why isn't X on my list?" may just be that.
* `plan --mal-export` and `[mal] export_path` work without API authorisation, for
  "what's missing" reports offline. With neither, `plan` fails with exit 2 and a message telling
  the user to `login` or point at an export — a UI should catch that and offer the login flow.

## If the GUI also hosts `plex-gap`

Same conventions on both: `--json` on stdout, diagnostics on stderr, non-zero exit means
"read stderr", stdlib-only Python, `check_*` scripts to re-run before finishing, one job in
flight per backend, private `config.*` files that must never be committed.

Differences that will bite:

| | `plex-gap` | `plex-mal-sync` |
|---|---|---|
| question answered | what am I *missing* (TVmaze) | what have I *finished* (Plex) → MAL |
| writes | nothing, ever | MyAnimeList, via `apply --confirm <hash>` |
| "already reported" state | `state/reported.json`, `--no-save` / `--full` semantics | no suppression state; `overrides.toml` `[covered]`/`[ignore]` |
| confirmation token | none needed | plan hash; **never** `--force` |
| token to protect | Plex token | Plex token **and** MAL client secret + user tokens |
| runtime | ~2 s warm, ~80 s cold | 3.6 s plan, 23 s apply |

Practical advice: one subprocess helper serving both, per-project config/token/state dirs,
separate spinners (they may run concurrently with each other — different external services —
but never two `apply`s, and remember both talk to the same Plex). A "sync everything" button
must still stop at "here is the MAL plan, confirm" — `plex-gap`'s read-only report can
auto-refresh; this one cannot.

## Suggested UI shape

* **Dashboard**: `status --json` (Plex reachable?, db exists, MAL authorised + expiry → a
  "re-login needed" badge), plus the last apply from `cache.last_apply_run`.
* **"Check now"**: `plan --json`. Show `items` as a table (`plex_show` → `mal_title`,
  `cur_watched → new_watched`, `cur_status → new_status`, `action`, `source`,
  `confidence`), then `review` (with its runner-up guesses and an "add override" affordance),
  then `blocked`, then `skipped` collapsed. Show the hash in the UI next to the button.
* **"Write it"**: one confirmation modal that re-shows the write table + hash, then
  `apply --no-resolve --confirm <that hash>` — pass `--no-resolve` so the plan being written
  is the plan that was displayed. Handle exit 3 by re-showing the *new* plan, never by
  retrying with `--force`.
* **After any apply**: show the diff (`+N new, M changed, 0 removed, K untouched`) and link
  the backup path. Red banner if `removed` or `unexplained` is non-empty.
* **"Undo last apply"**: run `undo` (dry) first, show what it would restore, then `--yes`.
* **Overrides editor**: three lists (`[mappings]`, `[ignore]`, `[covered]`) with the comments
  preserved; validate keys as `"<exact Plex title>|S<n>"` / `|ALL` / bare title. Note the
  subtlety: single-unit shows are given `season = None` by the planner, so only a bare title
  or `|ALL` matches them — a `|S1` key silently does nothing.
* **Never** expose: `--force`, `--skip-audit`, `watch --apply`. If you want an unattended
  mode, ask the user first: `watch --interval N --apply` exists, is audited and capped, but
  the user has chosen to run things by hand so far.

## Not verified / still open

* `watch --apply` (unattended loop) has never been run by the user. It computes each plan
  and passes that plan's own hash, so nothing unseen is written — but it is unaudited by
  practice here. Treat as experimental until the user opts in.
* `undo` has been exercised against the live list (it rolled back a 15-write test run on
  2026-11-25), but the entries that run created had to be deleted by hand on the MAL website
  afterwards, because MAL cannot delete list entries. Say that in the undo UI.
* MAL token **auto-refresh** on 401/near-expiry is implemented but only seen working
  incidentally; if a UI runs long-lived jobs, surface `status.mal.expires_human` and offer a
  re-login button.
* `resolve`'s mapping cache is misleading (see sharp edges) — a known cosmetic wart, not
  fixed.
* `[overrides] season_totals` is parsed but unused; `resolve_season`'s sequel-chain remains
  the imperfect part of matching.
* Nothing is scheduled on this machine: no Task Scheduler entry, no service. The user runs it
  deliberately, which is the point of a UI.
