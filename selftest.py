#!/usr/bin/env python3
"""
fleet selftest — prove the detect -> heal -> verify loop works, without
touching anything real.

A self-healer you have never seen heal anything is a hope, not a control. This
builds two disposable resources that are deliberately NOT running, points a
throwaway registry at them, and then runs the real watch.check() and the real
medic actions against them:

    1. a docker container created but not started   -> docker_restart
    2. a launchd agent bootstrapped but not running -> launchctl_kickstart

Both are removed again at the end, pass or fail. Nothing in the production
registry, state or audit log is read or written.

Run:  python3 ~/.fleet/selftest.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watch  # noqa: E402
import medic  # noqa: E402

CONTAINER = "fleet-selftest"
LABEL = "ai.fleet.selftest"
PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)
BEAT = os.path.join(tempfile.gettempdir(), "fleet_selftest_beat.log")
UID = os.getuid()
PASS, FAIL = [], []


def sh(*args, **kw):
    return subprocess.run(list(args), capture_output=True, text=True,
                          timeout=kw.get("timeout", 120))


def note(ok, label, detail=""):
    (PASS if ok else FAIL).append(label)
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label,
                          (" — " + detail) if detail else ""))


# ---------------------------------------------------------------- fixtures
def make_container():
    d = watch.docker_bin()
    if not d:
        return None
    sh(d, "rm", "-f", CONTAINER)
    r = sh(d, "create", "--name", CONTAINER, "alpine:latest", "sleep", "600")
    return d if r.returncode == 0 else None


def make_agent():
    plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>-c</string>
  <string>while true; do date >> %s; sleep 5; done</string></array>
  <key>RunAtLoad</key><false/>
  <key>KeepAlive</key><false/>
  <key>StandardOutPath</key><string>%s</string>
  <key>StandardErrorPath</key><string>%s</string>
</dict></plist>
""" % (LABEL, BEAT, BEAT, BEAT)
    open(PLIST, "w").write(plist)
    sh("launchctl", "bootout", "gui/%d/%s" % (UID, LABEL))
    r = sh("launchctl", "bootstrap", "gui/%d" % UID, PLIST)
    return r.returncode == 0


def cleanup():
    d = watch.docker_bin()
    if d:
        sh(d, "rm", "-f", CONTAINER)
    sh("launchctl", "bootout", "gui/%d/%s" % (UID, LABEL))
    for p in (PLIST, BEAT):
        try:
            os.unlink(p)
        except Exception:
            pass


# ---------------------------------------------------------------- the test
def run_case(job, expect_action):
    name = job["name"]
    before, detail = watch.check(job)
    note(before != "ok", "%s: detected as broken" % name, "status=%s" % before)
    if before == "ok":
        return

    remedy = job["remedy"]
    note(remedy["action"] == expect_action,
         "%s: remedy is %s" % (name, expect_action))

    ok, ran = medic.ACTIONS[remedy["action"]](remedy["args"])
    note(ok, "%s: remedy executed" % name, ran[:80])

    time.sleep(int(remedy.get("verify_wait", 10)))
    after, after_detail = watch.check(job)
    note(after == "ok", "%s: verified healthy after heal" % name,
         "status=%s %s" % (after, after_detail[:60]))


def guardrail_tests():
    print("\nGuardrails")
    ok, detail = medic.ACTIONS["run"]({"script": "/etc/passwd"})
    note(not ok and "outside the allowed" in detail,
         "run refuses a script outside the allowed directories")

    ok, detail = medic.ACTIONS["escalate"]({"reason": "human only"})
    note(not ok and "needs a human" in detail,
         "escalate never claims success")

    entry = {"attempts": [time.time()] * 3, "escalated": False}
    allowed, why = medic.may_attempt("x", entry, {})
    note(not allowed and "limit" in why, "attempt limit stops a retry storm", why)

    entry = {"attempts": [], "escalated": True}
    allowed, why = medic.may_attempt("x", entry, {})
    note(not allowed and "escalated" in why, "an escalated job is left alone", why)

    entry = {"attempts": [time.time()], "escalated": False}
    allowed, why = medic.may_attempt("x", entry, {})
    note(not allowed and "backoff" in why, "backoff holds off the next attempt", why)

    note("shell" not in medic.ACTIONS and "exec" not in medic.ACTIONS,
         "the action set is closed — no shell escape hatch")


def main():
    print("fleet selftest — proving detect -> heal -> verify\n")
    try:
        print("Docker container that exists but is not running")
        if make_container():
            run_case({"name": "selftest container", "docker": CONTAINER,
                      "remedy": {"action": "docker_restart",
                                 "args": {"container": CONTAINER},
                                 "verify_wait": 8}},
                     "docker_restart")
        else:
            note(False, "selftest container: could not be created (docker down?)")

        print("\nlaunchd agent that is loaded but not running")
        if make_agent():
            run_case({"name": "selftest agent", "launchd": LABEL,
                      "must_be_running": True,
                      "remedy": {"action": "launchctl_kickstart",
                                 "args": {"label": LABEL}, "verify_wait": 6}},
                     "launchctl_kickstart")
        else:
            note(False, "selftest agent: could not be bootstrapped")

        guardrail_tests()
    finally:
        cleanup()
        print("\ncleaned up: container removed, agent booted out, files deleted")

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed: " + "; ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
