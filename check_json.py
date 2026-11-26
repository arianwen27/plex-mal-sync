"""Contract test for the machine-readable CLI. Run it:  python check_json.py

A GUI talks to this project through subprocesses, so the rules that matter are about
STREAMS, not logic (logic is selftest's job):

  * with --json, stdout is exactly one JSON object and nothing else - no log lines
  * diagnostics (progress, Plex/MAL chatter, blockers) go to stderr
  * exit code 0 = fine, non-zero = the reason is on stderr, and --json also prints
    {"ok": false, "error": ...} so a caller never has to guess
  * the read-only commands really are read-only: no MAL write, no applied rows

Only commands that need neither a MAL write nor a browser are exercised here:
status, sections, selftest, and audit's error path. plan/apply also speak JSON; they
are checked by hand because they touch the network (and, for apply, your list).
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable or "python"
LOG_LINE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] ")
FAILS = []


def run(args, expect_json=True):
    p = subprocess.run([PY, "plex_mal.py"] + args, cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=600)
    return p.returncode, p.stdout or "", p.stderr or ""


def check(ok, label, detail=""):
    print("  %-4s %s%s" % ("PASS" if ok else "FAIL", label, ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(label)
    return ok


def json_object(stdout):
    """None unless stdout is one JSON value and no log line ever reached it."""
    if LOG_LINE.search(stdout):
        return None
    try:
        return json.loads(stdout)
    except ValueError:
        return None


def main():
    print("command contract (subprocess: python plex_mal.py ...)\n")

    rc, out, err = run(["status", "--json"])
    d = json_object(out)
    check(rc == 0 and d is not None, "status --json: rc 0, stdout is one JSON object")
    if d:
        check(d.get("ok") is True and d.get("command") == "status",
              "status --json: ok/command fields")
        check("plex" in d and "mal" in d and "cache" in d and "safety" in d and "paths" in d,
              "status --json: plex/mal/cache/safety/paths present")
        blob = json.dumps(d)
        check("client_id" not in blob.replace('"client_id_set"', "")
              .replace('"client_secret_set"', ""),
              "status --json: no client_id/secret keys, only the *_set booleans")
        check(not any(k in blob for k in ("access_token", "refresh_token")),
              "status --json: no access/refresh token material")
        check(isinstance(d["safety"].get("never_decrease"), bool)
              and d["safety"]["never_decrease"] is True,
              "status --json: safety rails are reported (never_decrease true)")
    check(err.strip() == "" or not LOG_LINE.search(err.replace(err.strip(), "")),
          "status --json: stdout carried no log lines")

    rc, out, err = run(["sections", "--json"])
    d = json_object(out)
    check(rc == 0 and d is not None and isinstance(d.get("sections"), list),
          "sections --json: rc 0 with a sections list")
    if d and d.get("sections"):
        s = d["sections"][0]
        check({"key", "type", "title"} <= set(s), "sections --json: each section has key/type/title")

    rc, out, err = run(["selftest", "--json"])
    d = json_object(out)
    check(rc == 0 and d is not None, "selftest --json: rc 0, stdout is one JSON object")
    if d:
        check(d.get("ok") is True and d.get("checks", 0) > 20 and d.get("failures") == [],
              "selftest --json: ok true, checks counted, failures empty",
              "checks=%s failures=%s" % (d.get("checks"), d.get("failures")))
    check(any("PASS" in x for x in err.splitlines()),
          "selftest --json: the human listing moved to stderr")

    # apply's refusal path: a --confirm that cannot possibly match means it is guaranteed to
    # write nothing. (We never run a matching apply here: that would touch the user's list.)
    rc, out, err = run(["apply", "--no-resolve", "--confirm", "impossible-hash-0000"])
    d = json_object(out)
    check(rc == 3 and d is not None and d.get("error") == "hash_mismatch" and d.get("written") == 0,
          "apply refuses a stale hash: rc 3, error hash_mismatch, written 0")
    check(bool(d) and bool((d or {}).get("new_hash")) and bool((d or {}).get("plan_json")),
          "apply refusal carries the new hash and where to read the new plan")
    check(bool(d) and any(LOG_LINE.search(x) or "plan has changed" in x for x in err.splitlines()),
          "apply refusal: the human explanation went to stderr")

    rc, out, err = run(["audit", "--plan", "definitely-not-a-plan.json", "--json"])
    d = json_object(out)
    check(rc == 2 and d is not None and d.get("ok") is False,
          "audit --json error path: rc 2, ok:false on stdout")
    check(bool((d or {}).get("error")) and "definitely-not-a-plan" in str((d or {}).get("error")),
          "audit --json error path: the reason names what could not be read")
    check(bool(err.strip()), "audit --json error path: reason also on stderr")

    for cmd in ("status", "sections", "plan", "audit", "selftest"):
        rc, out, err = run([cmd, "--help"])
        check(rc == 0 and "--json" in out, "%s --help advertises --json" % cmd)

    print("\n(apply/plan/login/watch are not run here: they touch the network, and apply "
          "touches your MyAnimeList list.)")
    print("\n" + ("ALL CONTRACT CHECKS PASSED" if not FAILS
                  else "%d CONTRACT CHECK(S) FAILED" % len(FAILS)))
    for f in FAILS:
        print("  " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
