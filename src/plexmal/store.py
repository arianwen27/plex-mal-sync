"""Local cache: Plex history journal, title->MAL mappings, MAL list cache, write log (undo)."""
from __future__ import annotations

import os
import sqlite3
import time

from .util import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS shows(
  gp_guid TEXT PRIMARY KEY,
  section_id INTEGER,
  section_key TEXT,
  plex_title TEXT,
  original_title TEXT,
  year INTEGER,
  last_viewed_at INTEGER,
  first_seen INTEGER,
  updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS episodes(
  guid TEXT PRIMARY KEY,
  gp_guid TEXT,
  section_id INTEGER,
  account_id INTEGER,
  account_name TEXT,
  season INTEGER,
  episode INTEGER,
  ep_title TEXT,
  mtype INTEGER,
  first_viewed_at INTEGER,
  last_viewed_at INTEGER,
  view_types TEXT,
  confirmed TEXT,
  journal_ts INTEGER
);
CREATE INDEX IF NOT EXISTS ep_gp ON episodes(gp_guid);
CREATE TABLE IF NOT EXISTS mappings(
  mkey TEXT PRIMARY KEY,          -- 'normalised title|S1' or '...|ALL'
  gp_guid TEXT,
  plex_title TEXT,
  season INTEGER,                 -- NULL = whole show
  mal_id INTEGER,
  anilist_id INTEGER,
  mal_title TEXT,
  source TEXT,                    -- anilist|shikimori|kitsu|mal|manual|override
  confidence REAL,
  evidence TEXT,
  alternatives TEXT,
  state TEXT,                     -- auto|needs_review|manual|ignored|blocked
  created_at INTEGER,
  updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS mal_cache(
  mal_id INTEGER PRIMARY KEY,
  title TEXT,
  num_episodes INTEGER,
  mal_status TEXT,
  watched INTEGER,
  list_status TEXT,
  adult INTEGER,
  fetched_at INTEGER,
  payload TEXT
);
CREATE TABLE IF NOT EXISTS applied(
  id INTEGER PRIMARY KEY,
  run_id TEXT,
  ts INTEGER,
  mal_id INTEGER,
  action TEXT,
  before TEXT,
  after TEXT,
  ok INTEGER,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


class Store:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.con.commit()

    # ---------------------------------------------------------------- meta
    def get_meta(self, k, default=None):
        r = self.con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def set_meta(self, k, v):
        self.con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
        self.con.commit()

    # ---------------------------------------------------------------- shows
    def upsert_show(self, gp_guid, section_id, plex_title, original_title, year, last_viewed_at):
        self.con.execute(
            """INSERT INTO shows(gp_guid,section_id,plex_title,original_title,year,last_viewed_at,first_seen,updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(gp_guid) DO UPDATE SET plex_title=excluded.plex_title,
                 original_title=excluded.original_title, year=excluded.year,
                 last_viewed_at=MAX(shows.last_viewed_at, excluded.last_viewed_at), updated_at=excluded.updated_at""",
            (gp_guid, section_id, plex_title, original_title, year, last_viewed_at, now(), now()))

    def shows(self):
        return [dict(r) for r in self.con.execute("SELECT * FROM shows ORDER BY last_viewed_at DESC")]

    # ------------------------------------------------------------- episodes
    def record_episode(self, guid, gp_guid, section_id, account_id, account_name, season, episode,
                       ep_title, mtype, viewed_at, view_type, confirmed=None):
        self.con.execute(
            """INSERT INTO episodes(guid,gp_guid,section_id,account_id,account_name,season,episode,ep_title,
                                     mtype,first_viewed_at,last_viewed_at,view_types,confirmed,journal_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(guid) DO UPDATE SET
                 last_viewed_at=MAX(episodes.last_viewed_at, excluded.last_viewed_at),
                 first_viewed_at=MIN(episodes.first_viewed_at, excluded.first_viewed_at),
                 view_types=(CASE WHEN instr(episodes.view_types, excluded.view_types) > 0
                                  THEN episodes.view_types ELSE episodes.view_types || ',' || excluded.view_types END),
                 confirmed=COALESCE(excluded.confirmed, episodes.confirmed),
                 journal_ts=excluded.journal_ts""",
            (guid, gp_guid, section_id, account_id, account_name, season, episode, ep_title,
             mtype, viewed_at, viewed_at, str(view_type), confirmed, now()))

    def episodes(self, gp_guid=None, season=None):
        q, args = "SELECT * FROM episodes", []
        wh = []
        if gp_guid:
            wh.append("gp_guid=?"); args.append(gp_guid)
        if season is not None:
            wh.append("season=?"); args.append(season)
        if wh:
            q += " WHERE " + " AND ".join(wh)
        return [dict(r) for r in self.con.execute(q, args)]

    def episode_count(self):
        return self.con.execute("SELECT COUNT(*) c FROM episodes").fetchone()["c"]

    # ------------------------------------------------------------ mappings
    def set_mapping(self, mkey, **kw):
        cols = dict(gp_guid=None, plex_title=None, season=None, mal_id=None, anilist_id=None,
                    mal_title=None, source=None, confidence=None, evidence=None,
                    alternatives=None, state="needs_review", created_at=now(), updated_at=now())
        cols.update({k: v for k, v in kw.items() if k in cols})
        cols["updated_at"] = now()
        self.con.execute(
            """INSERT INTO mappings(mkey,gp_guid,plex_title,season,mal_id,anilist_id,mal_title,source,
                                     confidence,evidence,alternatives,state,created_at,updated_at)
               VALUES(:mkey,:gp_guid,:plex_title,:season,:mal_id,:anilist_id,:mal_title,:source,
                      :confidence,:evidence,:alternatives,:state,:created_at,:updated_at)
               ON CONFLICT(mkey) DO UPDATE SET gp_guid=COALESCE(excluded.gp_guid,mappings.gp_guid),
                 plex_title=COALESCE(excluded.plex_title,mappings.plex_title),
                 season=COALESCE(excluded.season,mappings.season),
                 mal_id=COALESCE(excluded.mal_id,mappings.mal_id),
                 anilist_id=COALESCE(excluded.anilist_id,mappings.anilist_id),
                 mal_title=COALESCE(excluded.mal_title,mappings.mal_title),
                 source=excluded.source, confidence=excluded.confidence,
                 evidence=excluded.evidence, alternatives=excluded.alternatives,
                 state=excluded.state, updated_at=excluded.updated_at""", cols)
        self.con.commit()

    def mapping(self, mkey):
        r = self.con.execute("SELECT * FROM mappings WHERE mkey=?", (mkey,)).fetchone()
        return dict(r) if r else None

    def mappings(self, state=None):
        if state:
            return [dict(r) for r in self.con.execute("SELECT * FROM mappings WHERE state=? ORDER BY state, plex_title", (state,))]
        return [dict(r) for r in self.con.execute("SELECT * FROM mappings ORDER BY state, plex_title")]

    def mapping_for_mal(self, mal_id):
        return [dict(r) for r in self.con.execute("SELECT * FROM mappings WHERE mal_id=?", (mal_id,))]

    # ----------------------------------------------------------- mal cache
    def cache_mal(self, mal_id, title, num_episodes, mal_status, watched, list_status, adult, payload):
        self.con.execute(
            """INSERT INTO mal_cache(mal_id,title,num_episodes,mal_status,watched,list_status,adult,fetched_at,payload)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(mal_id) DO UPDATE SET title=excluded.title,num_episodes=excluded.num_episodes,
                 mal_status=excluded.mal_status, watched=excluded.watched, list_status=excluded.list_status,
                 adult=excluded.adult, fetched_at=excluded.fetched_at, payload=excluded.payload""",
            (mal_id, title, num_episodes, mal_status, watched, list_status, 1 if adult else 0, now(), payload))

    def mal_cache(self, mal_id):
        r = self.con.execute("SELECT * FROM mal_cache WHERE mal_id=?", (mal_id,)).fetchone()
        return dict(r) if r else None

    def all_mal_cache(self):
        return {r["mal_id"]: dict(r) for r in self.con.execute("SELECT * FROM mal_cache")}

    # ------------------------------------------------------------- writes
    def log_applied(self, run_id, mal_id, action, before, after, ok, detail=""):
        self.con.execute(
            "INSERT INTO applied(run_id,ts,mal_id,action,before,after,ok,detail) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, now(), mal_id, action, before, after, 1 if ok else 0, detail))
        self.con.commit()

    def last_run(self):
        r = self.con.execute("SELECT run_id FROM applied ORDER BY id DESC LIMIT 1").fetchone()
        return r["run_id"] if r else None

    def run_rows(self, run_id):
        return [dict(r) for r in self.con.execute("SELECT * FROM applied WHERE run_id=? ORDER BY id", (run_id,))]

    def commit(self):
        self.con.commit()
