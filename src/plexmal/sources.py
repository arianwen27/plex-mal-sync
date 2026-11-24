"""Read everything from Plex. Strictly read-only: SQLite opened mode=ro + HTTP GETs.
No Plex settings, files, plugins or agents are ever modified."""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile

from .util import http, now, tsn


class PlexError(RuntimeError):
    pass


HDRS = {"X-Plex-Client-Identifier": "plex-mal-sync",
        "X-Plex-Product": "plex-mal-sync",
        "X-Plex-Version": "1.0",
        "X-Plex-Platform": "Windows"}


def find_token(cfg, log):
    """config token -> .LocalAdminToken -> Preferences.xml (all local reads)."""
    t = (cfg.plex.get("token") or "").strip()
    if t:
        return t, "config"
    roots = []
    db = cfg.plex.get("db_path") or ""
    if db:
        roots.append(os.path.dirname(os.path.dirname(os.path.abspath(db))))
    env = os.environ.get("LOCALAPPDATA")
    if env:
        roots.append(os.path.join(env, "Plex Media Server"))
    for r in roots:
        if not r:
            continue
        p = os.path.join(r, ".LocalAdminToken")
        if os.path.exists(p):
            try:
                v = open(p, encoding="utf-8", errors="replace").read().strip()
                if v:
                    return v, ".LocalAdminToken"
            except Exception:
                pass
        p = os.path.join(r, "Preferences.xml")
        if os.path.exists(p):
            try:
                m = re.search(r'PlexOnlineToken="([^"]+)"', open(p, encoding="utf-8", errors="replace").read())
                if m:
                    return m.group(1), "Preferences.xml"
            except Exception:
                pass
    return "", ""


def _ro_uri(path):
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return f"file:{p}?mode=ro"


def connect_db(cfg, log):
    """Read-only handle on the live PMS DB. Falls back to a private temp snapshot."""
    db = cfg.plex.get("db_path") or ""
    if not db or not os.path.exists(db):
        raise PlexError(f"Plex DB not found: {db} (check [plex] db_path in config.toml)")
    try:
        con = sqlite3.connect(_ro_uri(db), uri=True, timeout=10)
        con.execute("SELECT COUNT(*) FROM metadata_item_views")
        con.row_factory = sqlite3.Row
        log.info("Plex DB: opened live file read-only")
        return con, None
    except Exception as e:
        log.warn(f"Plex DB read-only open failed ({e}) - using a private snapshot copy")
        d = tempfile.mkdtemp(prefix="pmsync_")
        tmp = os.path.join(d, "library.db")
        shutil.copy2(db, tmp)
        for ext in ("-wal", "-shm"):
            if os.path.exists(db + ext):
                shutil.copy2(db + ext, tmp + ext)
        con = sqlite3.connect(tmp)
        con.row_factory = sqlite3.Row
        return con, d


class Plex:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.token, src = find_token(cfg, log)
        self.base = (cfg.plex.get("url") or "http://127.0.0.1:32400").rstrip("/")
        if self.token:
            log.info(f"Plex API: {self.base} (token from {src})")
        else:
            log.warn("Plex API: no token found - per-episode confirmation disabled")

    # ------------------------------------------------------------- sections
    def sections(self):
        st, j, _ = http("GET", self.base + "/library/sections", headers=self._h(), params=self._p())
        if st != 200:
            raise PlexError(f"cannot list Plex sections: HTTP {st} {str(j)[:150]}")
        return [{"key": d.get("key"), "title": d.get("title"), "type": d.get("type")}
                for d in ((j or {}).get("MediaContainer", {}) or {}).get("Directory", [])]

    def resolve_sections(self):
        want = [str(x).lower() for x in (self.cfg.plex.get("libraries") or [])]
        out = []
        for s in self.sections():
            if s["title"].lower() in want or s["key"] in want:
                out.append(s)
        if not out:
            raise PlexError(f"no configured library sections found. Plex has: "
                            f"{[s['title'] for s in self.sections()]}")
        return out

    def accounts(self, con):
        return {r["name"]: r["id"] for r in con.execute("SELECT id,name FROM accounts")}

    # ------------------------------------------------------------ HTTP API
    def _h(self):
        return dict(HDRS)

    def _p(self):
        return {"X-Plex-Token": self.token} if self.token else {}

    def show_entries(self, section_key, limit=1000):
        """All show entries in a section (paginated)."""
        out, start, total = [], 0, None
        while True:
            st, j, _ = http("GET", f"{self.base}/library/sections/{section_key}/all",
                            params={**self._p(), "X-Plex-Container-Start": str(start),
                                    "X-Plex-Container-Size": str(limit)},
                            headers=self._h(), timeout=60)
            if st != 200:
                self.log.warn(f"section {section_key}/all -> HTTP {st}")
                break
            mc = (j or {}).get("MediaContainer", {}) or {}
            rows = mc.get("Metadata") or []
            out += rows
            total = mc.get("totalSize")
            start += len(rows)
            if not rows or (total is not None and start >= total):
                break
        return out

    def episodes_of_show(self, section_key, show):
        """Episode leaves of one show + whether the enumeration looks complete.

        NOTE: Plex's `filter=show.id:` silently returns shows instead of episodes on
        current PMS builds, and a single `/children` call can miss whole series on
        multi-series shows - so we walk the tree recursively and then check the leaf
        count against the show's own leafCount.
        """
        rk = show.get("ratingKey")
        out = self._walk(section_key, rk)
        leaf_count = int(show.get("leafCount") or 0)
        eps = []
        for e in out:
            if e.get("type") not in ("episode", "movie"):
                continue
            dur = e.get("duration") or 0
            off = e.get("viewOffset") or 0
            ctx = e.get("_ctx") or {}
            eps.append({
                "ratingKey": e.get("ratingKey"),
                "guid": e.get("guid"),
                "season": e.get("parentIndex", ctx.get("season")) if e.get("type") == "episode" else 1,
                "episode": e.get("index") if e.get("type") == "episode" else 1,
                "season_title": e.get("parentTitle") or ctx.get("season_title"),
                "ep_title": e.get("title"),
                "gp_guid": e.get("grandparentGuid") or show.get("guid"),
                "gp_rk": e.get("grandparentRatingKey") or rk,
                "show_title": e.get("grandparentTitle") or show.get("title"),
                "view_count": e.get("viewCount") or 0,
                "view_offset": off,
                "duration": dur,
                "last_viewed_at": e.get("lastViewedAt") or 0,
                "pct": (100.0 * off / dur) if (dur and off) else (100.0 if (e.get("viewCount") or 0) else 0.0),
            })
        complete = (leaf_count == 0) or (len(eps) >= leaf_count)
        return eps, complete

    def _walk(self, section_key, show_rk, depth=0, ctx=None):
        ctx = ctx or {}
        rows = []
        st, j, _ = http("GET", f"{self.base}/library/metadata/{show_rk}/children",
                        params={**self._p(), "includeViewOffsets": "1"},
                        headers=self._h(), timeout=60)
        if st != 200:
            self.log.warn(f"walk {show_rk} -> HTTP {st}")
            return rows
        for c in ((j or {}).get("MediaContainer", {}) or {}).get("Metadata") or []:
            if c.get("type") in ("season", "show", "collection", "boxset"):
                if depth < 4:
                    nctx = dict(ctx)
                    if c.get("type") == "season":
                        nctx["season"] = c.get("parentIndex", ctx.get("season"))
                        nctx["season_title"] = c.get("title")
                    rows += self._walk(section_key, c.get("ratingKey"), depth + 1, nctx)
            else:
                if ctx:
                    c["_ctx"] = ctx
                rows.append(c)
        return rows

    # ------------------------------------------------------- SQLite history
    def db_history(self, section_ids, account_names, ignore_season_zero=True, mtypes=(1, 4)):
        """Completed-play rows for the chosen libraries, restricted to *your* accounts."""
        con, cleanup = connect_db(self.cfg, self.log)
        try:
            acc_all = self.accounts(con)
            ids, unknown = [], []
            for n in (account_names or []):
                if str(n) in acc_all:
                    ids.append(int(acc_all[str(n)]))
                else:
                    hit = [v for k, v in acc_all.items() if k.lower() == str(n).lower()]
                    if hit:
                        ids.append(int(hit[0]))
                    elif str(n).isdigit():
                        ids.append(int(n))
                    else:
                        unknown.append(str(n))
            if unknown:
                self.log.warn(f"Plex accounts not found (ignored): {unknown}. "
                              f"Server accounts include: {sorted(k for k in acc_all if k)[:25]}")
            if not ids:
                raise PlexError(f"none of [plex] accounts {account_names} exist on this Plex server")
            account_ids = ids
            acc_names = {k: v for k, v in acc_all.items() if int(v) in account_ids}
            rows = []
            q = ('SELECT v.grandparent_guid, v.guid, v.parent_index, v."index", v.title, v.viewed_at, '
                 'v.view_type, v.account_id, v.metadata_type, v.library_section_id, a.name acct '
                 'FROM metadata_item_views v LEFT JOIN accounts a ON a.id=v.account_id '
                 'WHERE v.library_section_id IN ({sec}) AND v.account_id IN ({acc}) '
                 'AND v.metadata_type IN ({mt})')
            q = q.format(sec=",".join(str(int(s)) for s in section_ids),
                         acc=",".join(str(int(a)) for a in account_ids),
                         mt=",".join(str(int(m)) for m in mtypes))
            for r in con.execute(q):
                gp = r["grandparent_guid"] or r["guid"]
                season = r["parent_index"] if r["parent_index"] is not None else 1
                ep = r["index"] if r["index"] is not None else 1
                if ignore_season_zero and season == 0:
                    continue
                rows.append({"gp_guid": gp, "guid": r["guid"], "season": season, "episode": ep,
                             "ep_title": r["title"], "viewed_at": r["viewed_at"],
                             "view_type": r["view_type"], "account_id": r["account_id"],
                             "account_name": r["acct"], "mtype": r["metadata_type"],
                             "section_id": r["library_section_id"]})
            # show-level metadata (year / original title) for the shows we saw
            gids = sorted({r["gp_guid"] for r in rows})
            meta = {}
            if gids:
                ph = ",".join("?" * len(gids))
                for r in con.execute(f"SELECT guid,title,original_title,year FROM metadata_items "
                                     f"WHERE guid IN ({ph})", gids):
                    meta[r["guid"]] = {"plex_title": r["title"], "original_title": r["original_title"],
                                       "year": r["year"]}
            return rows, meta, acc_names
        finally:
            con.close()
            if cleanup:
                shutil.rmtree(cleanup, ignore_errors=True)
