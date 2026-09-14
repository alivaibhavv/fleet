# fleet

**A watchdog and an auto-healer for a fleet of local agents.** It watches the
signals your jobs already produce, tells you when one of them stops doing its
job, and — where a fix is safe to automate — fixes it and proves the fix worked.

Runs entirely on your own machine. No vendor account, no API key, no model.
MIT licensed.

---

## Why this exists

This was built after an audit of a working 27-job agent fleet — launchd jobs,
Docker containers, an OpenClaw gateway, n8n workflows. The fleet's own monitor
reported **3 problems**. A careful audit found **12**.

Every one of the nine it missed failed the same way: the monitor checked
whether a job *ran*, not whether it *did anything*.

- A publisher ran on schedule, exited 0 every time, and had saved nothing for
  **123 hours**. Its last step was failing. Exit code 0.
- The Ollama keep-alive was a `sleep 2147483647`. The supervisor was immortal,
  so launchd and the monitor both reported healthy for the eight hours the
  model server was dead.
- Every Docker check had been silently broken for months: Docker Desktop does
  not put its binary on the PATH a launchd job gets, so each check raised
  `FileNotFoundError`, returned "unknown", and inherited its last known status
  — forever. Two containers sat `Exited (255)` for a day, scored green.
- An n8n workflow was active, scheduled, and had **never executed once**. The
  check asked the container if it was up. It was up.
- And the one that motivated the content checks: a site answering **HTTP 200
  with a noindex parking page** on 85% of requests. Every status-code monitor
  in the world calls that healthy.

The lesson is the whole design: **liveness is not the same as usefulness**, and
a check that cannot answer must not get a free pass.

---

## The two halves

| | `watch.py` | `medic.py` |
|---|---|---|
| Role | detect and alert | repair and verify |
| Writes anything? | never — read-only by design | only the closed set of remedies below |
| Runs | every 15 min | every 15 min, after the watcher |
| Escalates | on state transitions only | when a repair fails or is a human's job |

They share one definition of health: `medic` imports `watch.check()`. There is
no second opinion to drift out of sync.

---

## What it can detect

| State | Meaning |
|---|---|
| `ok` | healthy |
| `stale` | the job's own log has gone quiet past its window |
| `failed` | not running, a non-zero exit, a dead port, or the wrong content on a live one |
| `stalled` | **runs on time and produces nothing** — the expensive one |
| `disabled` | switched off on purpose. Never alerts, always visible, never silently "ok" |
| `unknown` | the checker could not answer. Held briefly, then treated as a failure |

Signals it can read, per job:

- **launchd** — loaded, running, last exit code
- **Docker** — real `State.Running`, and a flag for containers with
  `RestartPolicy=no` that will not survive a reboot
- **HTTP** — status *and* body, with `expect` / `reject` strings, because 200 is not proof
- **log freshness** — `max_age_h`, roughly 1.5× the job's own interval
- **stall patterns** — several per job; a job has more than one way to do nothing
- **error density** — a clean exit with a log full of `ERROR` is still a failure
- **n8n executions** — proof a workflow actually ran, not that the container answered

---

## What it can repair

Six actions. That is the entire set, and the registry can only *name* one — it
can never supply a shell command:

| Action | Does |
|---|---|
| `launchctl_kickstart` | restart a launchd job |
| `docker_start` / `docker_restart` | bring a container back |
| `open_app` | launch a desktop app another tool depends on |
| `run` | run a script, only from directories on an allow-list |
| `escalate` | tell a human, with the reason and the command to run |

### The guardrails matter more than the actions

- Acts only on a problem the watcher has **confirmed twice**. One bad probe
  restarts nothing.
- **3 attempts per job per 24h**, then it escalates and stops. An auto-healer
  that retries forever turns a small outage into a loud one.
- **Exponential backoff** between attempts.
- **3 actions per run, maximum** — a cascade must not become an action storm.
- **Verifies** by re-running the same check, then records `fixed` or not.
- Never touches a `disabled` job or an `unknown` result.
- Anything needing a person — a QR scan, a DNS record, a paused cloud task — is
  declared `escalate` up front and never attempted.
- Every action, successful or not, is appended to `medic-audit.jsonl`.

---

## Install

```bash
git clone https://github.com/alivaibhavv/fleet.git ~/.fleet
cd ~/.fleet
cp registry.example.json registry.json     # then edit it for your machine
python3 watch.py --now                     # current status, alerts nothing
python3 medic.py --dry                     # what it would repair, touches nothing
```

Schedule both with launchd (macOS) or cron/systemd timers (Linux):

```bash
python3 watch.py     # every 15 min — detect, alert on transitions
python3 medic.py     # every 15 min — repair confirmed problems, verify
```

Example launchd plists are in [`docs/scheduling.md`](docs/scheduling.md).

### Prove it works before you trust it

```bash
python3 selftest.py
```

Builds a Docker container and a launchd agent that are deliberately **not
running**, runs the real checks and the real remedies against them, asserts
each one was detected, healed and verified, exercises all five guardrails, then
removes both. Nothing real is touched.

```
14 passed, 0 failed
```

---

## Configuring it

One entry per job in `registry.json`. Watch a job through a signal it already
produces, so nothing you run has to be modified to be covered.

```json
{
  "name": "Nightly report",
  "launchd": "com.example.report",
  "log": "~/logs/report.log",
  "max_age_h": 26,
  "stall": {
    "patterns": ["nothing to send", "HTTP 4"],
    "tail_lines": 60,
    "min_hits": 3,
    "note": "exits 0 and sends nothing"
  },
  "remedy": {
    "action": "launchctl_kickstart",
    "args": { "label": "com.example.report" },
    "max_attempts_per_day": 2,
    "cooldown_minutes": 45,
    "verify_wait": 60
  }
}
```

Every field is documented in
[`docs/registry-schema.md`](docs/registry-schema.md), and
[`registry.example.json`](registry.example.json) shows one job per feature.

> `registry.json` is git-ignored on purpose. It describes *your* machine —
> container names, internal URLs, origin IP addresses. Publishing it is how
> people accidentally hand out the origin address sitting behind their CDN.

---

## Alerting

Telegram via `curl`, plus a macOS desktop notification, so a single flaky path
out is never the reason you did not hear about an outage. Alerts fire on
**transitions only** — a job down for a week sends two messages, not 672.

Set `TELEGRAM_TOKEN` and `TELEGRAM_ALERT_CHAT_ID` in the env file named at the
top of `watch.py`. With no token it degrades to the desktop notification and
the status file.

---

## Files

```
watch.py               detection and alerting (read-only)
medic.py               repair, verification, audit trail
selftest.py            end-to-end proof on disposable resources
newagent.py            scaffold a new scheduled agent, with a model guardrail
bin/websearch          SearXNG-backed search that fails loudly, not silently
registry.example.json  one job per feature, copy it to registry.json
rotate_logs.sh         copy-then-truncate rotation for launchd-held logs
backup.sh              nightly backup of state that cannot be regenerated
tests/                 25 unit tests, no side effects
docs/                  schema and scheduling
```

### Two small things worth stealing

`rotate_logs.sh` rotates by **copy-then-truncate**, never rename. launchd and
long-running gateways hold open file descriptors on their logs; renaming the
file sends every later write into the renamed copy and leaves the live log
empty forever.

`backup.sh` copies SQLite with `.backup`, and always takes the `-wal` sidecar.
A plain `cp` of a live database that has not checkpointed restores the state of
whenever it last did — in the fleet this came from, that was two weeks earlier.

---

## Contributing

Issues and pull requests welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).
New remedies are held to one standard: **a remedy must be something you would
be happy to have run unattended at 4am, three times, and then stop.**

## License

MIT — see [LICENSE](LICENSE).
