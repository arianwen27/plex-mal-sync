"""MyAnimeList: OAuth2 (auth-code + PKCE 'plain'), API v2 client, and offline export reading.

Writes are deliberately narrow: only `status` and `num_watched_episodes` on a single
entry, one entry per call, rate limited, never a bulk replace.
"""
from __future__ import annotations

import csv
import http.server
import io
import json
import os
import secrets
import socketserver
import threading
import time
import urllib.parse
import zipfile

from .util import Ratelimit, http, now, read_json, truncate, write_json

AUTH_URL = "https://myanimelist.net/v1/oauth2/authorize"
TOKEN_URL = "https://myanimelist.net/v1/oauth2/token"
API = "https://api.myanimelist.net/v2"
LIST_FIELDS = ("id,title,media_type,num_episodes,statistics,status,start_season,"
               "list_status{status,num_watched_episodes,num_times_rereading,is_rewatching,"
               "time_updated,updated_at,priority,notes,tags}")

STATUS_ALIASES = {
    "watching": "watching", "watching": "watching", "currently watching": "watching",
    "completed": "completed", "finish": "completed", "finished": "completed",
    "on_hold".replace(" ", "_"): "on_hold", "onhold": "on_hold", "on hold": "on_hold",
    "dropped": "dropped", "plan_to_watch": "plan_to_watch", "plantowatch": "plan_to_watch",
    "plan to watch": "plan_to_watch", "": None,
}


def norm_status(s):
    if not s:
        return None
    return STATUS_ALIASES.get(str(s).strip().lower(), str(s).strip().lower() or None)


class MalError(RuntimeError):
    pass


class MAL:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.malc = cfg.mal
        self.tokens = read_json(cfg.tokens, None)
        self.rl = Ratelimit(cfg.sync.get("request_gap_seconds", 1.5))

    # ------------------------------------------------------------ credentials
    def has_client(self):
        return bool((self.malc.get("client_id") or "").strip())

    def ready(self):
        return bool(self.tokens and self.tokens.get("access_token"))

    def _verify(self):
        return secrets.token_urlsafe(96)[:128]

    def authorize_url(self, verifier, state):
        q = {"response_type": "code", "client_id": self.malc["client_id"].strip(), "state": state,
             "code_challenge": verifier, "code_challenge_method": "plain"}
        if self.malc.get("redirect_uri"):
            q["redirect_uri"] = self.malc["redirect_uri"]
        return AUTH_URL + "?" + urllib.parse.urlencode(q)

    def login(self, manual=False, timeout=300):
        if not self.has_client():
            raise MalError("No [mal] client_id in config.toml - register an app at "
                           "https://myanimelist.net/apiconfg and paste the Client ID.")
        verifier, state = self._verify(), secrets.token_urlsafe(9)
        url = self.authorize_url(verifier, state)
        print("\nOpen this in your browser and click Allow:\n\n  " + url + "\n")
        if manual:
            raw = input("Paste the full redirect URL (or just the code) and press Enter: ").strip()
            code = self._code_from(raw)
        else:
            code = self._catch_redirect(state, timeout)
        self.exchange(code, verifier, state)
        return self.tokens

    @staticmethod
    def _code_from(raw):
        raw = raw.strip()
        if "code=" in raw:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query or raw.split("?", 1)[-1])
            if q.get("code"):
                return q["code"][0]
        return raw

    def _catch_redirect(self, state, timeout):
        redir = self.malc.get("redirect_uri") or "http://localhost:8765/callback"
        port = urllib.parse.urlparse(redir).port or 8765
        box = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                box["code"] = (q.get("code") or [None])[0]
                box["state"] = (q.get("state") or [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h3>plex-mal-sync: authorisation captured.</h3>"
                                 b"<p>You can close this tab now.</p>")

            def log_message(self, *a):
                pass

        with socketserver.TCPServer(("127.0.0.1", port), H) as srv:
            srv.timeout = 1
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            t0 = time.time()
            while time.time() - t0 < timeout:
                if box.get("code"):
                    break
                time.sleep(0.4)
            srv.shutdown()
        if not box.get("code"):
            raise MalError(f"timed out waiting for the browser redirect on port {port}. "
                           f"Retry with --manual and paste the code.")
        if box.get("state") and box["state"] != state:
            raise MalError("OAuth state mismatch - refusing to continue.")
        return box["code"]

    def exchange(self, code, verifier, state=None):
        form = {"client_id": self.malc["client_id"].strip(), "grant_type": "authorization_code",
                "code": code, "code_verifier": verifier}
        if self.malc.get("redirect_uri"):
            form["redirect_uri"] = self.malc["redirect_uri"]
        if (self.malc.get("client_secret") or "").strip():
            form["client_secret"] = self.malc["client_secret"].strip()
        st, j, _ = http("POST", TOKEN_URL, form=form, timeout=40)
        if st != 200 or not isinstance(j, dict) or not j.get("access_token"):
            raise MalError(f"token exchange failed: HTTP {st} {truncate(str(j), 300)}")
        j["obtained_at"] = now()
        j["expires_at"] = now() + int(j.get("expires_in") or 2415600)
        write_json(self.cfg.tokens, j)
        self.tokens = j
        self.log.info("MAL: access token saved (expires "
                      + time.strftime("%Y-%m-%d", time.localtime(j["expires_at"])) + ")")
        return j

    def refresh(self):
        if not self.tokens or not self.tokens.get("refresh_token"):
            return False
        form = {"client_id": self.malc.get("client_id", "").strip(), "grant_type": "refresh_token",
                "refresh_token": self.tokens["refresh_token"]}
        if (self.malc.get("client_secret") or "").strip():
            form["client_secret"] = self.malc["client_secret"].strip()
        st, j, _ = http("POST", TOKEN_URL, form=form, timeout=40)
        if st != 200 or not isinstance(j, dict) or not j.get("access_token"):
            self.log.warn(f"MAL refresh failed: HTTP {st} {truncate(str(j), 200)}")
            return False
        j["obtained_at"] = now()
        j["expires_at"] = now() + int(j.get("expires_in") or 2415600)
        write_json(self.cfg.tokens, j)
        self.tokens = j
        self.log.info("MAL: token refreshed")
        return True

    # ------------------------------------------------------------------ calls
    def _auth(self):
        if not self.ready():
            raise MalError("Not authorised with MyAnimeList. Run:  python plex_mal.py login")
        return {"Authorization": "Bearer " + self.tokens["access_token"]}

    def _req(self, method, path, params=None, form=None, _retry=True):
        if self.tokens and self.tokens.get("expires_at", 0) - 3600 < now() and self.tokens.get("refresh_token"):
            self.refresh()
        self.rl.wait()
        st, j, hdr = http(method, API + path, params=params, form=form, headers=self._auth(), timeout=45, retries=2)
        if st == 401 and _retry and self.tokens.get("refresh_token"):
            self.log.info("MAL: 401 -> refreshing and retrying once")
            if self.refresh():
                return self._req(method, path, params=params, form=form, _retry=False)
        return st, j, hdr

    def whoami(self):
        st, j, _ = self._req("GET", "/users/@me", params={"fields": "id,name,anime_list_status_count"})
        if st != 200:
            raise MalError(f"whoami failed HTTP {st} {truncate(str(j), 200)}")
        return j

    def my_list(self, status=None, max_items=5000):
        """Full anime list of the authorised user -> {mal_id: entry}."""
        out, offset, total = {}, 0, None
        while True:
            params = {"limit": "100", "offset": str(offset), "nsfw": "true", "fields": LIST_FIELDS}
            if status:
                params["status"] = status
            st, j, _ = self._req("GET", "/users/@me/animelist", params=params)
            if st != 200:
                raise MalError(f"animelist read failed HTTP {st} {truncate(str(j), 250)}")
            items = (j or {}).get("data") or []
            for it in items:
                n = it.get("node") or {}
                ls = n.get("my_list_status") or {}
                out[n.get("id")] = {
                    "mal_id": n.get("id"), "title": n.get("title"),
                    "media_type": n.get("media_type"), "total_episodes": n.get("num_episodes"),
                    "airing_status": n.get("status"),
                    "status": norm_status(ls.get("status")),
                    "watched": ls.get("num_watched_episodes"),
                    "list_updated": ls.get("time_updated") or ls.get("updated_at"),
                }
            paging = ((j or {}).get("paging") or {})
            total = (j or {}).get("total_count")
            offset += len(items)
            if not items or offset >= max_items or (paging.get("next") is None):
                break
            if not paging.get("next"):
                break
        return out

    def update(self, mal_id, fields, methods=("PATCH", "PUT")):
        """Set only the given fields on one entry. Returns (ok, status, body)."""
        body = {k: v for k, v in fields.items() if v is not None}
        if not body:
            return True, "noop", {}
        path = f"/anime/{mal_id}/my_list_status"
        last = None
        for m in methods:
            st, j, hdr = self._req(m, path, form=body)
            last = (st, j, hdr)
            if st in (200, 201):
                self.log.info(f"MAL {m} anime/{mal_id} {body} -> ok")
                return True, st, j
            if st == 405:
                continue
            # 400 with 'invalid num_watched_episodes' etc: do not retry with another verb
            self.log.warn(f"MAL {m} anime/{mal_id} {body} -> HTTP {st} {truncate(str(j), 200)}")
            return False, st, j
        st, j, _ = last
        return False, st, j

    def anime(self, mal_id):
        st, j, _ = self._req("GET", f"/anime/{mal_id}", params={"fields": "id,title,num_episodes,status,media_type"})
        if st != 200:
            return None
        return j

    def backup(self, note=""):
        """Snapshot the current live list to data/backups before any write."""
        data = self.my_list()
        os.makedirs(self.cfg.backups_dir, exist_ok=True)
        p = os.path.join(self.cfg.backups_dir,
                         f"mal_list_{time.strftime('%Y%m%d_%H%M%S')}.json")
        write_json(p, {"backup_at": now(), "note": note, "count": len(data), "list": data})
        self.log.info(f"MAL: backed up {len(data)} list entries -> {os.path.basename(p)}")
        return p, data


# ----------------------------------------------------------- offline export

def load_export(path, log=None):
    """Read a MAL 'Export Data' file (.zip / .json / .csv) -> {mal_id: entry}."""
    if not path or not os.path.exists(path):
        raise MalError(f"export file not found: {path}")
    blobs = []
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if "animelist" in n.lower():
                    blobs.append((n.lower(), z.read(n).decode("utf-8", "replace")))
    else:
        blobs.append((os.path.basename(path).lower(), open(path, encoding="utf-8", errors="replace").read()))

    out = {}
    for name, text in blobs:
        if name.endswith(".json"):
            out.update(_parse_export_json(text, log))
        elif name.endswith(".csv"):
            out.update(_parse_export_csv(text, log))
    if not out:
        raise MalError(f"could not parse any anime list out of {path}")
    if log:
        log.info(f"read {len(out)} list entries from MAL export {os.path.basename(path)}")
    return out


def _pick(d, *cands):
    for c in cands:
        for k in d:
            if k.lower().replace(" ", "_") == c:
                return d[k]
    for c in cands:
        for k in d:
            if c in k.lower():
                return d[k]
    return None


def _to_int(v, default=0):
    try:
        return int(str(v).strip())
    except Exception:
        return default


def _parse_export_json(text, log=None):
    try:
        obj = json.loads(text)
    except Exception as e:
        if log: log.warn(f"export json parse error: {e}")
        return {}
    rows = obj.get("data") if isinstance(obj, dict) else obj
    if not isinstance(rows, list):
        return {}
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        mid = _to_int(_pick(r, "series_id", "mal_id", "id"), 0)
        if not mid:
            continue
        st = _pick(r, "series_status", "status")
        if isinstance(st, dict):
            st = st.get("status") or st.get("name")
        out[mid] = {"mal_id": mid,
                    "title": _pick(r, "series_title", "title") or "",
                    "total_episodes": _to_int(_pick(r, "series_total_episodes", "num_episodes", "episodes"), 0),
                    "watched": _to_int(_pick(r, "series_watched_episodes", "num_watched_episodes", "watched"), 0),
                    "status": norm_status(st), "source": "export"}
    return out


def _parse_export_csv(text, log=None):
    out = {}
    try:
        rd = csv.DictReader(io.StringIO(text))
    except Exception as e:
        if log: log.warn(f"export csv parse error: {e}")
        return {}
    for r in rd:
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        mid = _to_int(_pick(r, "mal id", "mal_id", "series_id", "id"), 0)
        if not mid:
            continue
        out[mid] = {"mal_id": mid,
                    "title": _pick(r, "title", "series_title") or "",
                    "total_episodes": _to_int(_pick(r, "total episodes", "episodes"), 0),
                    "watched": _to_int(_pick(r, "watched episodes", "watched"), 0),
                    "status": norm_status(_pick(r, "watching status", "status")), "source": "export"}
    return out
