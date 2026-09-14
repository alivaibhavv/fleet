#!/usr/bin/env python3
"""
fleet watch — one alarm for the whole agent fleet.

The failure mode this exists for is SILENCE: a job that exits 0 while doing
nothing, or stops firing and never says so. Error-only alerting misses both.
This watches three conditions:

  stale    the job's own log has not been touched inside its expected window
  failed   launchd recorded a non-zero exit, or a named container is not running
  stalled  the job runs on time but its recent runs all match a "did nothing"
           pattern — the publisher skipping on a full review queue is the exact
           case this was built for, and the one error alerting could never catch

Alerts fire on TRANSITION only (ok -> problem, problem -> ok), so a job that is
down for a week sends two messages, not 672.

Read-only. It never touches the jobs it watches — it reads the logs they already
write, so nothing had to be modified to be covered.

Run:  python3 ~/.fleet/watch.py         check + alert on transitions
      python3 ~/.fleet/watch.py --dry   check + print what it would send
      python3 ~/.fleet/watch.py --now   print current status, never alerts
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

HOME = os.path.expanduser("~")
FLEET = os.path.join(HOME, ".fleet")
REGISTRY = os.path.join(FLEET, "registry.json")
STATE = os.path.join(FLEET, "state.json")
STATUS = os.path.join(FLEET, "status.json")
ENV_FILE = os.path.join(HOME, "cryptonite-agent", ".env")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def age_hours(path):
    """Hours since path was last written. None if it does not exist."""
    p = os.path.expanduser(path)
    if not os.path.exists(p):
        return None
    return (time.time() - os.path.getmtime(p)) / 3600.0


def launchd_status(label):
    """(pid, last_exit_code) for a launchd label, or (None, None) if absent."""
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception:
        return (None, None)
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].strip() == label:
            pid = None if parts[0].strip() == "-" else parts[0].strip()
            try:
                code = int(parts[1].strip())
            except ValueError:
                code = None
            return (pid, code)
    return (None, None)


# Docker Desktop does NOT install a `docker` binary onto the PATH this job runs
# with. Before 2026-09-14 every docker check here raised FileNotFoundError, became
# 'unknown', and silently inherited its last known status — which is why SEOnaut
# sat "ok" for a day after both its containers exited 255. Resolve it explicitly.
DOCKER_CANDIDATES = [
    "/Applications/Docker.app/Contents/Resources/bin/docker",
    "/usr/local/bin/docker",
    "/opt/homebrew/bin/docker",
    "/usr/bin/docker",
]


def docker_bin():
    for p in DOCKER_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def docker_running(name):
    """True/False from the container's actual State.Running. None only if the
    docker binary itself cannot be found or the call fails."""
    d = docker_bin()
    if not d:
        return None
    try:
        out = subprocess.run(
            [d, "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True, text=True, timeout=25)
        if out.returncode != 0:
            return False          # no such container == not running
        return out.stdout.strip() == "true"
    except Exception:
        return None


def docker_unsupervised(name):
    """True if the container would NOT come back after a reboot."""
    d = docker_bin()
    if not d:
        return None
    try:
        out = subprocess.run(
            [d, "inspect", "-f", "{{.HostConfig.RestartPolicy.Name}}", name],
            capture_output=True, text=True, timeout=25)
        return out.stdout.strip() in ("", "no")
    except Exception:
        return None


def http_ok(url, timeout=8, expect=None, reject=None):
    """True if url answers 2xx/3xx AND its body passes the content test.

    A status check alone is not enough. On 2026-09-14 cryptonite.ae answered
    HTTP 200 to 17 of 20 requests with Hostinger's PARKING PAGE — a noindex
    holding page — because Cloudflare was round-robining between the real
    origin and a parking IP. Every status-only monitor on earth would have
    called that healthy. `expect` is a string the real response must contain;
    `reject` is a string that proves it is the wrong response.
    """
    try:
        r = subprocess.run(
            ["/usr/bin/curl", "-s", "-m", str(timeout), "-w",
             "\n__STATUS__%{http_code}", url],
            capture_output=True, text=True, timeout=timeout + 15)
        body, _, code = r.stdout.rpartition("__STATUS__")
        code = code.strip()
        if not code or code == "000":
            return False
        if code[0] not in ("2", "3"):
            return False
        if reject and reject.lower() in body.lower():
            return False
        if expect and expect.lower() not in body.lower():
            return False
        return True
    except Exception:
        return None


def n8n_execution_count():
    """How many executions n8n has ever recorded. A container that answers
    /healthz while never executing an active workflow is the exact 'running but
    doing nothing' case this alarm exists for."""
    d = docker_bin()
    if not d:
        return None
    try:
        tmp = "/tmp/.fleet_n8n_probe.sqlite"
        # The -wal sidecar MUST come too. n8n has not checkpointed its main db
        # file since 30 Aug, so copying database.sqlite alone reports the state of
        # two weeks ago — which is how this probe first "proved" zero executions.
        cp = subprocess.run([d, "cp", "n8n:/home/node/.n8n/database.sqlite", tmp],
                            capture_output=True, text=True, timeout=60)
        if cp.returncode != 0:
            return None
        for side in ("-wal", "-shm"):
            subprocess.run([d, "cp", "n8n:/home/node/.n8n/database.sqlite" + side,
                            tmp + side], capture_output=True, text=True, timeout=60)
        import sqlite3
        # Plain connect, not mode=ro: this is our own throwaway copy, and the
        # read-only URI fails on it because SQLite cannot place its -wal sidecar.
        con = sqlite3.connect(tmp)
        n = con.execute("select count(*) from execution_entity").fetchone()[0]
        con.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(tmp + suffix)
            except Exception:
                pass
        return n
    except Exception:
        return None


def error_line_count(path, tail_lines=40, token="ERROR"):
    """How many of the last N lines are error lines. A job can exit 0 every run
    while most of its work fails — the cryptonite cron agent did this for six
    days on HTTP 429 and scored 'ok' the whole time."""
    p = os.path.expanduser(path)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, max(4096, tail_lines * 260))
            f.seek(size - block)
            lines = f.read().decode("utf-8", "replace").splitlines()[-tail_lines:]
    except Exception:
        return None
    return sum(1 for ln in lines if token in ln)


def tail_hits(path, pattern, tail_lines):
    """How many of the last N lines contain pattern. None if unreadable."""
    p = os.path.expanduser(path)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, max(4096, tail_lines * 220))
            f.seek(size - block)
            lines = f.read().decode("utf-8", "replace").splitlines()[-tail_lines:]
    except Exception:
        return None
    return sum(1 for ln in lines if pattern in ln)



def openclaw_cron(job_id, max_age_h=26):
    """(status, detail) for an OpenClaw cron job, read from its run history.
    Jobs ported off Claude land here, so a ported job cannot fail silently
    just because it no longer writes a log file of its own."""
    try:
        out = subprocess.run(["openclaw", "cron", "runs", "--id", job_id],
                             capture_output=True, text=True, timeout=45).stdout
        data = json.loads(out[out.index("{"):])
    except Exception as e:
        return ("unknown", "could not read run history (%s)" % type(e).__name__)
    entries = data.get("entries") or []
    if not entries:
        return ("stale", "no run recorded yet")
    newest = entries[0]
    age = (time.time() - newest["ts"] / 1000.0) / 3600.0
    if newest.get("status") == "error":
        return ("failed", "last run errored: %s" % str(newest.get("error"))[:120])
    if age < max_age_h:
        return ("ok", "")
    return ("stale", "last run was %.1fh ago (expected every %.0fh)"
            % (age, max_age_h))


def check(job):
    """Return (status, detail). status is ok | stale | failed | stalled | unknown."""
    name = job.get("name", "?")

    # Deliberately switched off. Reporting a decision as a fault is noise — but
    # reporting it as "ok" is worse: a job you paused for an afternoon becomes
    # indistinguishable from a healthy one and quietly stays off for weeks.
    # 'disabled' is its own state: never alerts, always visible.
    if job.get("disabled"):
        return ("disabled", job.get("_why", "switched off deliberately"))

    # Answering on a port is the only proof a service is up. Watching the launchd
    # job that is *supposed* to start it proves nothing — ae.cryptonite.ollama ran
    # `sleep 2147483647` and reported healthy for the eight hours Ollama was dead.
    if job.get("http"):
        spec = job["http"] if isinstance(job["http"], dict) else {"url": job["http"]}
        url = spec.get("url")
        up = http_ok(url, int(spec.get("timeout", 8)),
                     spec.get("expect"), spec.get("reject"))
        if up is None:
            return ("unknown", "could not probe %s" % url)
        if not up:
            detail = spec.get("note") or "%s is not answering correctly" % url
            return ("failed", detail)

    if job.get("cron_job"):
        status, detail = openclaw_cron(job["cron_job"],
                                       float(job.get("watch_max_age_h", 26)))
        if status != "ok":
            return (status, detail)

    if job.get("docker"):
        running = docker_running(job["docker"])
        if running is None:
            return ("unknown", "docker not reachable")
        if not running:
            return ("failed", "container %s is not running" % job["docker"])

    if job.get("launchd"):
        pid, code = launchd_status(job["launchd"])
        if code is None and pid is None:
            return ("failed", "launchd job %s is not loaded" % job["launchd"])
        if job.get("must_be_running") and pid is None:
            return ("failed", "%s is loaded but not running" % job["launchd"])
        # A non-zero code is the LAST exit, not the current state. If the job is
        # running now, KeepAlive already recovered it and the stale code would
        # otherwise alarm forever. Only report it when nothing is running.
        if pid is None and code not in (0, None):
            return ("failed", "%s last exited with code %s" % (job["launchd"], code))

    if job.get("log"):
        age = age_hours(job["log"])
        if age is None:
            return ("failed", "log missing at %s — job may never have run" % job["log"])
        limit = float(job.get("max_age_h", 26))
        if age > limit:
            return ("stale", "silent for %.1fh (expected activity every %.0fh)" % (age, limit))

    stall = job.get("stall")
    if stall and job.get("log"):
        # A job usually has more than one way to do nothing. The publisher was
        # watched only for "unreviewed drafts" and so scored ok through six days
        # of HTTP 429, which is a different no-op with the same result: no output.
        pats = stall.get("patterns") or [stall.get("pattern")]
        tail_n = int(stall.get("tail_lines", 60))
        min_hits = int(stall.get("min_hits", 4))
        for pat in [p for p in pats if p]:
            hits = tail_hits(job["log"], pat, tail_n)
            if hits is not None and hits >= min_hits:
                return ("stalled", "running on time but doing nothing — %s (%d recent hits on %r)"
                        % (stall.get("note", "no-op pattern"), hits, pat))

    # Exit code 0 with a log full of errors is still a failure.
    el = job.get("error_lines")
    if el and job.get("log"):
        n = error_line_count(job["log"], int(el.get("tail_lines", 40)),
                             el.get("token", "ERROR"))
        if n is not None and n >= int(el.get("max", 3)):
            return ("stalled", "exits clean but %d of its last %d log lines are %s — %s"
                    % (n, int(el.get("tail_lines", 40)), el.get("token", "ERROR"),
                       el.get("note", "most of its work is failing")))

    # n8n specifically: prove work happened, not just that the port answers.
    if job.get("n8n_executions"):
        n = n8n_execution_count()
        if n == 0:
            return ("stalled", "container is healthy but n8n has never recorded "
                               "a single workflow execution")

    # A container nothing will restart is a fault waiting for the next reboot.
    if job.get("docker") and job.get("require_restart_policy"):
        if docker_unsupervised(job["docker"]) is True:
            return ("stalled", "running, but RestartPolicy=no — it will not come "
                               "back after a reboot")

    return ("ok", "")


def telegram(text):
    """Send via curl. The Node and Python clients on this machine both flap on
    Telegram's API; curl has been reliable, so the alarm uses curl deliberately."""
    env = {}
    try:
        for line in open(ENV_FILE):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        return False
    token, chat = env.get("TELEGRAM_TOKEN"), env.get("TELEGRAM_ALERT_CHAT_ID")
    if not token or not chat:
        return False
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", "20", "-o", "/dev/null", "-w", "%{http_code}",
             "https://api.telegram.org/bot%s/sendMessage" % token,
             "-d", "chat_id=" + chat, "--data-urlencode", "text=" + text],
            capture_output=True, text=True, timeout=30)
        return r.stdout.strip() == "200"
    except Exception:
        return False


def desktop_note(title, text):
    """Second channel. Telegram is unreliable from this network, so the alarm
    never depends on a single path out."""
    try:
        subprocess.run(
            ["osascript", "-e",
             'display notification %s with title %s' % (json.dumps(text[:200]), json.dumps(title))],
            capture_output=True, timeout=15)
    except Exception:
        pass


ICON = {"ok": "OK", "stale": "SILENT", "failed": "DOWN", "stalled": "STALLED",
        "unknown": "UNKNOWN", "disabled": "OFF"}

# 'disabled' is a decision, not a fault: it shows in status.json and in --now,
# but it never alerts and never counts toward the problem total.
NON_PROBLEM = ("ok", "disabled")

# How many consecutive 'unknown' results before the checker's own failure is
# treated as a problem. Holding the last known status forever is how six docker
# checks stayed green for a day while their containers were dead.
UNKNOWN_LIMIT = 4

LOCK = os.path.join(FLEET, "watch.lock")

# A job must look bad this many checks in a row before it alerts. At a 15-minute
# interval that is a 30-minute delay — irrelevant for failures that last days,
# and it kills the flapping that turns an alarm into noise you learn to ignore.
CONFIRM = 2


def acquire_lock():
    """Single instance only. Concurrent runs corrupt the transition state."""
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(LOCK) > 600:
                os.unlink(LOCK)          # stale lock from a killed run
                return acquire_lock()
        except Exception:
            pass
        return False


def release_lock():
    try:
        os.unlink(LOCK)
    except Exception:
        pass


def main():
    dry = "--dry" in sys.argv
    now_only = "--now" in sys.argv

    reg = load_json(REGISTRY, {"jobs": []})
    prev = load_json(STATE, {})
    results, new_state = [], {}

    for job in reg.get("jobs", []):
        name = job["name"]
        status, detail = check(job)
        was = prev.get(name, {})
        if not isinstance(was, dict):
            was = {"status": "ok", "streak": 0, "alerted": False}

        # 'unknown' means the checker itself could not answer (docker busy,
        # launchctl timeout). One or two of those is not evidence the job is bad,
        # so hold the last known status — but only for a while. Holding forever is
        # exactly how every docker check here stayed green through a real outage:
        # the docker binary was not on this job's PATH, so the check raised, went
        # 'unknown', and inherited "ok" every 15 minutes for months.
        unknown_streak = was.get("unknown_streak", 0)
        if status == "unknown":
            unknown_streak += 1
            if unknown_streak >= UNKNOWN_LIMIT:
                status = "failed"
                detail = ("cannot be checked — %s (unchecked for %d consecutive runs)"
                          % (detail or "checker failed", unknown_streak))
            else:
                status, detail = was.get("status", "ok"), was.get("detail", "")
        else:
            unknown_streak = 0

        streak = was.get("streak", 0) + 1 if status == was.get("status") else 1
        new_state[name] = {"status": status, "detail": detail, "streak": streak,
                           "alerted": was.get("alerted", False),
                           "unknown_streak": unknown_streak}
        results.append((name, status, detail))

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    problems = [r for r in results if r[1] not in NON_PROBLEM]
    switched_off = [r[0] for r in results if r[1] == "disabled"]

    with open(STATUS, "w") as f:
        json.dump({"checked_at": stamp,
                   "jobs": [{"name": n, "status": s, "detail": d} for n, s, d in results],
                   "problems": len(problems),
                   "switched_off": switched_off,
                   "not_watchable_from_here": reg.get("not_watchable", [])}, f, indent=2)

    if now_only:
        for n, s, d in results:
            print("%-8s %-32s %s" % (ICON[s], n, d))
        print("\n%d of %d have a problem." % (len(problems), len(results)))
        return 0


    broke, fixed = [], []
    for name, entry in new_state.items():
        if entry["status"] not in NON_PROBLEM and entry["streak"] >= CONFIRM and not entry["alerted"]:
            broke.append("%s — %s" % (name, entry["detail"]))
            entry["alerted"] = True
        elif entry["status"] in NON_PROBLEM and entry["alerted"]:
            fixed.append(name)
            entry["alerted"] = False

    lines = []
    if broke:
        lines.append("Fleet alarm — %d job(s) went bad:" % len(broke))
        lines += ["  - " + b for b in broke]
    if fixed:
        lines.append("Recovered: " + ", ".join(fixed))

    if lines:
        msg = "\n".join(lines) + "\n\nStill bad: %d of %d. Full status: ~/.fleet/status.json" % (
            len(problems), len(results))
        if dry:
            print("[dry] would send:\n" + msg)
        else:
            sent = telegram(msg)
            desktop_note("Fleet alarm" if broke else "Fleet recovered", "\n".join(lines))
            print("[%s] alerted (telegram=%s):\n%s" % (stamp, sent, msg))
    else:
        pending = [n for n, e in new_state.items()
                   if e["status"] != "ok" and not e["alerted"]]
        note = " (%d awaiting confirmation)" % len(pending) if pending else ""
        print("[%s] no change — %d of %d have a problem%s" % (
            stamp, len(problems), len(results), note))

    if not dry:
        with open(STATE, "w") as f:
            json.dump(new_state, f, indent=2)
    return 0


if __name__ == "__main__":
    if not acquire_lock():
        print("another watch run is in progress — skipping")
        sys.exit(0)
    try:
        sys.exit(main())
    finally:
        release_lock()
