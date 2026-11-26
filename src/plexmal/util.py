"""Small stdlib-only helpers: HTTP, title normalisation, logging, time."""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

USER_AGENT = "plex-mal-sync/1.0 (personal Plex->MyAnimeList sync)"


def now() -> int:
    return int(time.time())


def ts(x) -> str:
    if not x:
        return "-"
    try:
        return datetime.fromtimestamp(int(x), timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return str(x)


def tsn(x) -> str:
    if not x:
        return "-"
    try:
        return datetime.fromtimestamp(int(x), timezone.utc).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(x)


class Log:
    def __init__(self, path=None, quiet=False):
        self.path = path
        self.quiet = quiet
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)

    def _emit(self, level, msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {level:5s} {msg}"
        if not self.quiet:
            # diagnostics go to STDERR, deliberately: the report a caller parses lives on
            # stdout, and a progress line in the middle of it makes the output unparseable
            print(line, file=sys.stderr, flush=True)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(datetime.now().isoformat(timespec="seconds") + f" {level:5s} {msg}\n")

    def info(self, m): self._emit("INFO", m)
    def warn(self, m): self._emit("WARN", m)
    def error(self, m): self._emit("ERROR", m)
    def debug(self, m):
        if not self.quiet:
            print(f"[debug] {m}", file=sys.stderr, flush=True)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------- HTTP

def http(method, url, *, params=None, headers=None, form=None, json_body=None,
         data=None, timeout=25, retries=2, log=None):
    """Return (status, parsed_body_or_text, headers_dict). Never raises on HTTP status."""
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(params, doseq=True)
    h = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    body = None
    if json_body is not None:
        h["Content-Type"] = "application/json"
        body = json.dumps(json_body).encode()
    elif form is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
        body = urllib.parse.urlencode(form, doseq=True).encode()
    elif data is not None:
        body = data.encode() if isinstance(data, str) else data
    if headers:
        h.update({k: v for k, v in headers.items() if v is not None})

    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=h, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, _decode(raw), dict(r.headers)
        except urllib.error.HTTPError as e:
            raw = e.read()
            hdr = dict(e.headers or {})
            status = e.code
            if status in (429, 500, 502, 503, 504) and attempt < retries:
                wait = float(hdr.get("Retry-After") or (2 ** attempt * 2))
                if log:
                    log.warn(f"HTTP {status} from {url.split('?')[0]} - retry in {wait:.0f}s")
                time.sleep(min(wait, 60))
                continue
            return status, _decode(raw), hdr
        except Exception as e:  # URLError, timeout, DNS...
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return 0, f"{type(e).__name__}: {e}", {}
    return 0, str(last), {}


def _decode(raw):
    try:
        txt = raw.decode("utf-8")
    except Exception:
        return raw
    if not txt:
        return {}
    try:
        return json.loads(txt)
    except Exception:
        return txt


class Ratelimit:
    """Simple min-gap limiter."""

    def __init__(self, gap):
        self.gap = max(0.0, float(gap))
        self._last = 0.0

    def wait(self):
        d = time.time() - self._last
        if d < self.gap:
            time.sleep(self.gap - d)
        self._last = time.time()


# ---------------------------------------------------------------- titles

_PARENS = re.compile(r"[\(\[\{\]<][^\)\]\}>]*[\)\]\}>]")
_NONWORD = re.compile(r"[^0-9a-z\u00c0-\uffff]+")
_STOP = {"the", "a", "an", "of", "and", "to", "in", "on", "for", "is", "le", "la", "les", "el", "das", "der", "die"}
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
_SEASON_WORDS = {"season", "sezon", "staffel", "series", "part", "cour", "nd", "rd"}


def norm_title(s: str) -> str:
    """Aggressive normalisation for comparing Plex / AniList / MAL titles."""
    if not s:
        return ""
    s = str(s)
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    # drop bracketed noise: "(TV)", "[Sub]", "(2019)", "<!>" ...
    prev = None
    while prev != s:
        prev = s
        s = _PARENS.sub(" ", s)
    s = s.lower()
    s = re.sub(r"\b(ova|ona|special|movie|tv)\b", " ", s) if len(s) > 12 and s.count(" ") > 3 else s
    s = _NONWORD.sub(" ", s)
    toks = [t for t in s.split() if t]
    toks = [_ROMAN.get(t, t) for t in toks]
    toks = [t for t in toks if t not in _SEASON_WORDS]
    core = [t for t in toks if t not in _STOP]
    return " ".join(core or toks)


def tokens(s):
    return set(s.split())


def sim(a: str, b: str) -> float:
    """0..1 similarity between two already-normalised titles."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    r = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = tokens(a), tokens(b)
    inter = len(ta & tb)
    jac = inter / max(1, len(ta | tb))
    cov = inter / max(1, min(len(ta), len(tb)))  # containment: short title inside long one
    cand = [r, 0.75 * jac + 0.25 * r, 0.55 * cov + 0.45 * r]
    if a in b or b in a:
        # a substring hit is weaker when the other title has substantially more words
        # ("chainsmoker cat" vs "chainsmoker cat minis" must not read as an exact match)
        ratio = min(len(a), len(b)) / max(len(a), len(b))
        cand.append(0.94 * ratio)
    return max(cand)


def title_variants(*vals) -> list[str]:
    out = []
    for v in vals:
        n = norm_title(v)
        if n and n not in out:
            out.append(n)
    return out


def best_sim(queries, targets) -> float:
    b = 0.0
    for q in queries:
        for t in targets:
            b = max(b, sim(q, t))
    return b


def truncate(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def roman(n):
    conv = {1: "i", 2: "ii", 3: "iii", 4: "iv", 5: "v", 6: "vi", 7: "vii", 8: "viii", 9: "ix", 10: "x",
            11: "xi", 12: "xii"}
    return conv.get(int(n), str(n))
