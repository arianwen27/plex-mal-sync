"""MyAnimeList: OAuth2 (auth-code + PKCE 'plain'), API v2 client, and offline export reading.

Writes are deliberately narrow: only `status` and `num_watched_episodes` on a single
entry, one entry per call, rate limited, never a bulk replace.
"""
from __future__ import annotations

import csv
import io
import json
import os
import secrets
import socket
import socketserver
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler

from .util import Ratelimit, http, now, read_json, truncate, write_json

AUTH_URL = "https://myanimelist.net/v1/oauth2/authorize"
TOKEN_URL = "https://myanimelist.net/v1/oauth2/token"
API = "https://api.myanimelist.net/v2"
LIST_FIELDS = ("id,title,media_type,num_episodes,statistics,status,start_season,my_list_status")

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

    @property
    def _pending(self):
        return os.path.join(os.path.dirname(self.cfg.tokens), "mal_oauth_pending.json")

    def login(self, manual=False, paste=None, timeout=900, url_only=False, reuse=True):
        if not self.has_client():
            raise MalError("No [mal] client_id in config.toml - register an app at "
                           "https://myanimelist.net/apiconfg and paste the Client ID.")

        if paste:  # finish a flow started with `login` or `login --url-only`
            pend = read_json(self._pending, None)
            if not pend or not pend.get("verifier"):
                raise MalError("no authorisation in progress - run `login` first to get a URL")
            if now() - int(pend.get("created") or 0) > 3600:
                raise MalError("the authorisation in progress is over an hour old - run `login` again")
            code = self._code_from(paste)
            if pend.get("state") and "state=" in str(paste) and pend["state"] not in str(paste):
                self.log.warn("state in the pasted URL is not the one we issued (continuing anyway)")
            return self.exchange(code, pend["verifier"], pend.get("state"))

        pend = read_json(self._pending, None) or {}
        fresh = (now() - int(pend.get("created") or 0)) < 600
        if reuse and fresh and pend.get("verifier") and pend.get("url"):
            # same verifier, same state: a restarted listener still accepts a URL
            # the user already has open in a tab
            verifier, state, url = pend["verifier"], pend["state"], pend["url"]
            self.log.info("reusing the authorisation in progress (state %s)" % state)
        else:
            verifier, state = self._verify(), secrets.token_urlsafe(9)
            url = self.authorize_url(verifier, state)
            # persist so that a browser that cannot reach the callback can still be finished by hand
            write_json(self._pending, {"verifier": verifier, "state": state, "created": now(), "url": url})

        print("\nOpen this in your browser and click Allow:\n\n  " + url + "\n")
        print("If the page after Allow says 'cannot be reached', that is fine: press Ctrl+L then "
              "Ctrl+A, Ctrl+C and paste the whole address into:\n"
              "   python plex_mal.py login --paste-file <file-with-the-url>\n")
        if url_only:
            print("(--url-only: nothing is listening for the callback.)\n")
            return None
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

        log = self.log

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                # browsers also fetch /favicon.ico against this socket; only a request that
                # actually carries a code (or an error) may fill the box, otherwise that
                # favicon hit wipes the real code out before the poll loop sees it.
                if q.get("code"):
                    box["code"] = q["code"][0]
                    box["state"] = (q.get("state") or [None])[0]
                    log.info("MAL callback received at %s" % path)
                elif q.get("error"):
                    box["error"] = (q.get("error") or [None])[0]
                    box["state"] = (q.get("state") or [None])[0]
                    log.error("MAL returned an error at %s: %s" % (path, box["error"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h3>plex-mal-sync: authorisation captured.</h3>"
                                 b"<p>You can close this tab now.</p>")

            def log_message(self, fmt, *args):
                pass  # keep the default stderr access log out of the way

        class DualStack(socketserver.ThreadingTCPServer):
            """Bind '::' with V6ONLY off so BOTH ::1 and 127.0.0.1 reach us.

            Browsers resolve 'localhost' to ::1 first on many Windows setups; an
            IPv4-only listener looks healthy in netstat but is unreachable there.
            """
            allow_reuse_address = True
            daemon_threads = True
            address_family = socket.AF_INET6

            def server_bind(self):
                try:
                    self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except OSError:
                    pass
                socketserver.TCPServer.server_bind(self)

        try:
            srv = DualStack(("::", port), H)
            self.log.info(f"MAL callback listening on [::]:{port} (IPv4 + IPv6)")
        except Exception as e:
            self.log.warn(f"dual-stack bind failed ({e}); trying IPv4 127.0.0.1")
            srv = socketserver.ThreadingTCPServer(("127.0.0.1", port), H)
        with srv:
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            t0 = time.time()
            while time.time() - t0 < timeout:
                if box.get("code"):
                    break
                time.sleep(0.4)
            srv.shutdown()
        if not box.get("code"):
            raise MalError(f"timed out after {timeout}s waiting for the browser redirect on port {port}. "
                           f"Nothing reached the listener, so the browser is not able to load "
                           f"http://localhost:{port}/callback (Local Network Access blocking, or the "
                           f"Windows firewall). Finish it by hand: copy the full address bar after "
                           f"clicking Allow and run  login --paste-file <file>  - the PKCE verifier is "
                           f"saved in {self._pending}")
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
        missing_status = 0
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
                ls = n.get("my_list_status")
                missing_status += 1 if not isinstance(ls, dict) else 0
                ls = ls if isinstance(ls, dict) else {}
                # MAL's list READ returns 'num_episodes_watched' while the list WRITE
                # endpoint takes 'num_watched_episodes'. Not the same spelling. Never
                # coerce a missing value to 0: unknown must stay unknown (watched_known).
                w = ls.get("num_episodes_watched")
                if w is None:
                    w = ls.get("num_watched_episodes")
                out[n.get("id")] = {
                    "mal_id": n.get("id"), "title": n.get("title"),
                    "media_type": n.get("media_type"), "total_episodes": n.get("num_episodes"),
                    "airing_status": n.get("status"),
                    "status": norm_status(ls.get("status")),
                    "watched": (int(w) if w is not None else None),
                    "watched_known": w is not None,
                    "score": ls.get("score"),
                    "list_updated": ls.get("time_updated_at") or ls.get("updated_at"),
                }
            paging = ((j or {}).get("paging") or {})
            total = (j or {}).get("total_count")
            offset += len(items)
            if not items or offset >= max_items or (paging.get("next") is None):
                break
            if not paging.get("next"):
                break
        if out and missing_status and missing_status == len(out):
            raise MalError(
                f"none of the {len(out)} list entries came back with my_list_status. The planner "
                f"cannot tell '0 episodes watched' from 'field not returned', and guessing 0 would "
                f"rewrite the whole list. Refusing to continue - check LIST_FIELDS in malapi.py.")
        if missing_status:
            self.log.warn(f"{missing_status}/{len(out)} list entries have no my_list_status; "
                          f"those entries are excluded from planning, never treated as 0")
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

    # The anime endpoint returns a stub unless you ask for fields - without
    # alternative_titles there is no English title, no synonyms and no nsfw flag, and an
    # audit that cannot see those is checking far less than it looks like it is.
    ANIME_FIELDS = "id,title,num_episodes,status,media_type"
    AUDIT_FIELDS = ("id,title,alternative_titles,synonyms,nsfw,num_episodes,status,"
                    "media_type,start_season")

    def anime(self, mal_id, fields=None):
        st, j, _ = self._req("GET", f"/anime/{mal_id}",
                             params={"fields": fields or self.ANIME_FIELDS})
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
