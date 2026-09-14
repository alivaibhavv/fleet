#!/usr/bin/env python3
"""
fleet medic — the half of the fleet that does something about it.

watch.py is deliberately read-only: it detects and it alerts. That is the right
shape for an alarm, but it means every fix waits for a human to read a message.
This is the other half. It takes the problems watch.py has already CONFIRMED,
looks up a remedy the operator wrote down in advance, applies it, and then
re-runs the very same check to prove the fix worked.

Design rules, in order of importance:

  1. Detection is not duplicated. medic imports watch.check(), so there is
     exactly one definition of "is this job healthy".
  2. Remedies are a closed set. The registry names an action from ACTIONS
     below; it can never supply a shell command. A compromised or fat-fingered
     registry cannot make this run arbitrary code.
  3. It gives up. Three attempts in 24h per job, then it escalates to a human
     and stops. An auto-healer that retries forever is a way to turn a small
     outage into a loud one.
  4. It only touches problems watch.py has seen twice. Acting on a single
     failed probe is how you restart a healthy service during a blip.
  5. Anything a human must do — scan a QR code, change a DNS record, re-enable
     a paused cloud task — is declared `escalate` and never attempted.
  6. Everything it does is written to an append-only audit log, whether it
     worked or not.

Run:  python3 ~/.fleet/medic.py           detect, heal, verify, alert
      python3 ~/.fleet/medic.py --dry     say what it would do, touch nothing
      python3 ~/.fleet/medic.py --status  print the healing history
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watch  # noqa: E402  — single source of truth for detection

HOME = os.path.expanduser("~")
FLEET = watch.FLEET
REGISTRY = watch.REGISTRY
WATCH_STATE = watch.STATE
MEDIC_STATE = os.path.join(FLEET, "medic-state.json")
AUDIT = os.path.join(FLEET, "medic-audit.jsonl")
LOCK = os.path.join(FLEET, "medic.lock")

# Ceilings. These are the difference between a self-healing fleet and a fleet
# that restarts itself into the ground.
MAX_ATTEMPTS_PER_DAY = 3      # per job, rolling 24h
COOLDOWN_MINUTES = 20         # minimum gap between attempts on one job
MAX_ACTIONS_PER_RUN = 3       # a cascade must not become an action storm
CONFIRM_STREAK = 2            # watch.py must have seen it bad this many times
VERIFY_WAIT_SECONDS = 12      # let a restarted service bind its port

# Scripts the `run` action is permitted to execute. A path outside these
# prefixes is refused even if the registry asks for it.
SCRIPT_ALLOW_PREFIXES = (
    os.path.join(HOME, ".fleet"),
    os.path.join(HOME, "cryptonite-agent"),
)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def audit(event):
    event["ts"] = now_iso()
    try:
        with open(AUDIT, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# The closed set of things the medic is allowed to do.
# Each returns (ok: bool, detail: str). None of them take free-form input.
# ---------------------------------------------------------------------------

def act_launchctl_kickstart(args):
    label = args["label"]
    uid = os.getuid()
    r = subprocess.run(
        ["launchctl", "kickstart", "-k", "gui/%d/%s" % (uid, label)],
        capture_output=True, text=True, timeout=60)
    ok = r.returncode == 0
    return ok, "launchctl kickstart %s -> rc=%d %s" % (
        label, r.returncode, (r.stderr or "").strip()[:120])


def act_docker_start(args):
    d = watch.docker_bin()
    if not d:
        return False, "docker binary not found"
    name = args["container"]
    r = subprocess.run([d, "start", name], capture_output=True, text=True, timeout=120)
    ok = r.returncode == 0
    return ok, "docker start %s -> rc=%d %s" % (
        name, r.returncode, (r.stderr or "").strip()[:120])


def act_docker_restart(args):
    d = watch.docker_bin()
    if not d:
        return False, "docker binary not found"
    name = args["container"]
    r = subprocess.run([d, "restart", name], capture_output=True, text=True, timeout=180)
    ok = r.returncode == 0
    return ok, "docker restart %s -> rc=%d %s" % (
        name, r.returncode, (r.stderr or "").strip()[:120])


def act_open_app(args):
    app = args["app"]
    # -g keeps it in the background: healing should not steal the screen.
    r = subprocess.run(["open", "-g", "-a", app],
                       capture_output=True, text=True, timeout=60)
    ok = r.returncode == 0
    return ok, "open -g -a %s -> rc=%d %s" % (
        app, r.returncode, (r.stderr or "").strip()[:120])


def act_run(args):
    script = os.path.expanduser(args["script"])
    real = os.path.realpath(script)
    if not any(real.startswith(os.path.realpath(p)) for p in SCRIPT_ALLOW_PREFIXES):
        return False, "refused: %s is outside the allowed script directories" % real
    if not os.path.exists(real):
        return False, "refused: %s does not exist" % real
    r = subprocess.run(["/bin/bash", real], capture_output=True, text=True, timeout=600)
    ok = r.returncode == 0
    return ok, "bash %s -> rc=%d %s" % (
        os.path.basename(real), r.returncode, (r.stdout or "").strip()[-160:])


def act_escalate(args):
    return False, "needs a human: %s" % args.get("reason", "no reason recorded")


ACTIONS = {
    "launchctl_kickstart": act_launchctl_kickstart,
    "docker_start": act_docker_start,
    "docker_restart": act_docker_restart,
    "open_app": act_open_app,
    "run": act_run,
    "escalate": act_escalate,
}


# ---------------------------------------------------------------------------
# Attempt accounting
# ---------------------------------------------------------------------------

def recent_attempts(entry, hours=24):
    cutoff = time.time() - hours * 3600
    return [a for a in entry.get("attempts", []) if a > cutoff]


def may_attempt(name, entry, remedy):
    """(allowed, reason). Every 'no' here is a guardrail doing its job."""
    if entry.get("escalated"):
        return False, "already escalated to a human; not retrying"
    limit = int(remedy.get("max_attempts_per_day", MAX_ATTEMPTS_PER_DAY))
    tried = recent_attempts(entry)
    if len(tried) >= limit:
        return False, "hit the %d-attempt limit for today" % limit
    cooldown = int(remedy.get("cooldown_minutes", COOLDOWN_MINUTES)) * 60
    # Back off: the 2nd attempt waits twice the cooldown, the 3rd three times.
    cooldown *= max(1, len(tried))
    if tried and time.time() - max(tried) < cooldown:
        wait = int((cooldown - (time.time() - max(tried))) / 60)
        return False, "in backoff for another %d min" % wait
    return True, ""


def main():
    dry = "--dry" in sys.argv
    if "--status" in sys.argv:
        return print_status()

    reg = load_json(REGISTRY, {"jobs": []})
    wstate = load_json(WATCH_STATE, {})
    mstate = load_json(MEDIC_STATE, {})

    healed, failed, escalated, skipped = [], [], [], []
    actions_taken = 0

    for job in reg.get("jobs", []):
        name = job["name"]
        seen = wstate.get(name, {})
        status = seen.get("status", "ok")

        # Only act on a confirmed, genuinely bad job.
        if status in ("ok", "disabled", "unknown"):
            continue
        if seen.get("streak", 0) < CONFIRM_STREAK:
            skipped.append((name, "not confirmed yet (streak %s)" % seen.get("streak")))
            continue

        remedy = job.get("remedy")
        if not remedy:
            skipped.append((name, "no remedy defined"))
            continue

        entry = mstate.setdefault(name, {"attempts": [], "escalated": False})

        action = remedy.get("action")
        if action == "escalate":
            if not entry.get("escalated"):
                entry["escalated"] = True
                entry["escalated_at"] = now_iso()
                escalated.append((name, remedy.get("reason", "")))
                audit({"job": name, "action": "escalate", "ok": False,
                       "detail": remedy.get("reason", ""), "dry": dry})
            continue

        if action not in ACTIONS:
            skipped.append((name, "unknown action %r — refused" % action))
            audit({"job": name, "action": str(action), "ok": False,
                   "detail": "action not in the allowed set", "dry": dry})
            continue

        allowed, why = may_attempt(name, entry, remedy)
        if not allowed:
            skipped.append((name, why))
            if "attempt limit" in why and not entry.get("escalated"):
                entry["escalated"] = True
                entry["escalated_at"] = now_iso()
                escalated.append((name, "auto-heal failed %d times: %s"
                                  % (len(recent_attempts(entry)), seen.get("detail", ""))))
            continue

        if actions_taken >= MAX_ACTIONS_PER_RUN:
            skipped.append((name, "run action budget spent; will retry next cycle"))
            continue

        if dry:
            skipped.append((name, "[dry] would run %s %s" % (action, remedy.get("args", {}))))
            continue

        # --- act ---
        entry.setdefault("attempts", []).append(time.time())
        actions_taken += 1
        try:
            ok, detail = ACTIONS[action](remedy.get("args", {}))
        except Exception as e:
            ok, detail = False, "%s: %s" % (type(e).__name__, e)

        # --- verify with the same check that found the problem ---
        time.sleep(int(remedy.get("verify_wait", VERIFY_WAIT_SECONDS)))
        post_status, post_detail = watch.check(job)
        fixed = post_status in ("ok", "disabled")

        audit({"job": name, "action": action, "args": remedy.get("args", {}),
               "ran_ok": ok, "detail": detail,
               "status_before": status, "status_after": post_status,
               "fixed": fixed})

        if fixed:
            entry["attempts"] = []
            entry["escalated"] = False
            entry["last_healed"] = now_iso()
            entry["heal_count"] = entry.get("heal_count", 0) + 1
            healed.append((name, detail))
        else:
            failed.append((name, "%s -> still %s: %s" % (detail, post_status, post_detail)))

    if not dry:
        with open(MEDIC_STATE, "w") as f:
            json.dump(mstate, f, indent=2)

    report(healed, failed, escalated, skipped, dry)
    return 0


def report(healed, failed, escalated, skipped, dry):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    for n, d in healed:
        lines.append("HEALED    %s — %s" % (n, d))
    for n, d in failed:
        lines.append("FAILED    %s — %s" % (n, d))
    for n, d in escalated:
        lines.append("ESCALATED %s — %s" % (n, d))

    if lines:
        print("[%s] %s" % (stamp, "; ".join(l.split(" — ")[0] for l in lines)))
        for l in lines:
            print("   " + l)
    else:
        print("[%s] nothing to heal (%d skipped)" % (stamp, len(skipped)))
    for n, d in skipped:
        print("   skip      %s — %s" % (n, d))

    # Tell the human only about things they need to know: a fix that happened,
    # and a fix that could not happen. Silence otherwise.
    if not dry and (healed or escalated or failed):
        msg = ["Fleet medic — %s" % stamp]
        if healed:
            msg.append("Healed automatically:")
            msg += ["  - %s" % n for n, _ in healed]
        if failed:
            msg.append("Tried and did not fix:")
            msg += ["  - %s" % n for n, _ in failed]
        if escalated:
            msg.append("Needs you:")
            msg += ["  - %s — %s" % (n, d) for n, d in escalated]
        body = "\n".join(msg)
        watch.telegram(body)
        watch.desktop_note("Fleet medic", body)


def print_status():
    mstate = load_json(MEDIC_STATE, {})
    print("%-42s %-8s %-22s %s" % ("JOB", "HEALS", "LAST HEALED", "STATE"))
    print("-" * 96)
    for name, e in sorted(mstate.items()):
        state = "ESCALATED" if e.get("escalated") else "armed"
        print("%-42s %-8s %-22s %s" % (
            name[:42], e.get("heal_count", 0),
            (e.get("last_healed") or "never")[:19], state))
    if os.path.exists(AUDIT):
        print("\nLast 10 actions:")
        with open(AUDIT) as f:
            for line in f.readlines()[-10:]:
                try:
                    a = json.loads(line)
                except Exception:
                    continue
                print("  %s  %-34s %-20s %s" % (
                    a.get("ts", "")[:19], a.get("job", "")[:34],
                    a.get("action", ""),
                    "FIXED" if a.get("fixed") else a.get("detail", "")[:60]))
    return 0


def acquire_lock():
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(LOCK) > 1800:
                os.unlink(LOCK)
                return acquire_lock()
        except Exception:
            pass
        return False


def release_lock():
    try:
        os.unlink(LOCK)
    except Exception:
        pass


if __name__ == "__main__":
    if "--status" in sys.argv or "--dry" in sys.argv:
        sys.exit(main())
    if not acquire_lock():
        print("another medic run is in progress — skipping")
        sys.exit(0)
    try:
        sys.exit(main())
    finally:
        release_lock()
