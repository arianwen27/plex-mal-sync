"""Config loading + standard paths."""
from __future__ import annotations

import os
import tomllib

DEFAULTS = {
    "plex": {
        "db_path": "",
        "url": "http://127.0.0.1:32400",
        "token": "",
        "libraries": [],
        "accounts": [],
        "confirm_complete_play": True,
        "complete_threshold_pct": 90,
        "ignore_season_zero": True,
        "include_metadata_types": [1, 4],
    },
    "mal": {
        "client_id": "",
        "client_secret": "",
        "redirect_uri": "http://localhost:8765/callback",
        "export_path": "",
    },
    "sync": {
        "never_decrease": True,
        "allow_new_entries": True,
        "mark_completed_at_full": True,
        "status_policy": "conservative",
        "skip_adult": True,
        "min_confidence": 0.75,
        "ambiguity_margin": 0.08,
        "max_new_entries_per_run": 15,
        "max_episode_bump": 26,
        "max_writes_per_run": 25,
        "season_chains": True,
        "request_gap_seconds": 1.5,
        "resolve_gap_seconds": 1.0,
    },
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Cfg:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.root = os.path.dirname(self.path)
        raw = {}
        if os.path.exists(self.path):
            with open(self.path, "rb") as fh:
                raw = tomllib.load(fh)
        self.d = _merge(DEFAULTS, raw)
        for sec in DEFAULTS:
            self.d.setdefault(sec, dict(DEFAULTS[sec]))

        d = os.path.join(self.root, "data")
        self.data_dir = d
        self.cache_db = os.path.join(d, "cache.sqlite")
        self.tokens = os.path.join(d, "mal_tokens.json")
        self.overrides_path = os.path.join(self.root, "overrides.toml")
        self.reports_dir = os.path.join(d, "reports")
        self.backups_dir = os.path.join(d, "backups")
        self.logs_dir = os.path.join(d, "logs")
        for p in (d, self.reports_dir, self.backups_dir, self.logs_dir):
            os.makedirs(p, exist_ok=True)

    # convenience accessors -------------------------------------------------
    @property
    def plex(self): return self.d["plex"]
    @property
    def mal(self): return self.d["mal"]
    @property
    def sync(self): return self.d["sync"]

    def overrides(self) -> dict:
        """overrides.toml:  [mappings] "Title|S1" = 12345 ; [ignore] shows = ['X'] ; [notes] ..."""
        out = {"mappings": {}, "ignore": [], "single_entry": [], "season_totals": {}}
        if not os.path.exists(self.overrides_path):
            return out
        try:
            with open(self.overrides_path, "rb") as fh:
                raw = tomllib.load(fh)
        except Exception as e:
            raise SystemExit(f"overrides.toml is not valid TOML: {e}")
        out["mappings"] = dict(raw.get("mappings") or {})
        out["ignore"] = list(raw.get("ignore", {}).get("shows") or raw.get("ignore") or [])
        out["single_entry"] = list(raw.get("single_entry", {}).get("shows") or [])
        # [covered] units = ["86|S1", ...]: a Plex unit whose watches are already on MAL
        # under entries MAL splits differently, so it should be neither written nor reviewed.
        out["covered"] = list(raw.get("covered", {}).get("units") or raw.get("covered") or [])
        out["season_totals"] = dict(raw.get("season_totals") or {})
        return out

    def redacted(self) -> str:
        m = dict(self.mal)
        if m.get("client_secret"): m["client_secret"] = "***"
        p = dict(self.plex)
        if p.get("token"): p["token"] = "***"
        return f"plex={p}\nmal={ {k: v for k, v in m.items() if k != 'export_path'} }"
