# registry.json — field reference

One object per job in `jobs`. A job needs a `name` and at least one signal.
Checks run in the order below and the first non-`ok` result wins, so put the
cheapest, most decisive signal on a job rather than all of them.

## Identity

| Field | Type | Meaning |
|---|---|---|
| `name` | string, required | Unique. Used as the key in state and in alerts. |
| `_why` | string | Free-text note to your future self. Never read by the code. |
| `disabled` | bool | Switched off on purpose. Reports `disabled`, never alerts, never counts as a problem — and never silently passes as `ok`. |

## Signals

### `http`
```json
"http": {
  "url": "https://example.com/sitemap.xml",
  "timeout": 25,
  "expect": "<urlset",
  "reject": "Parked Domain name",
  "note": "sitemap is not returning real XML"
}
```
Fails on a non-2xx/3xx status, on `reject` appearing in the body, or on
`expect` being absent from it. **Use `expect`/`reject` on anything public.** A
status-only check reports a parking page or a soft-404 as healthy; both are
served with 200.

`note` replaces the generic failure text in the alert.

### `launchd`
```json
"launchd": "com.example.job",
"must_be_running": true
```
`must_be_running` is for daemons. Leave it off for scheduled jobs, which are
correctly not running most of the time.

A non-zero exit code is only reported when nothing is running now — otherwise
KeepAlive has already recovered and the stale code would alarm forever.

### `docker`
```json
"docker": "example-service",
"require_restart_policy": true
```
Reads the container's real `State.Running`. `require_restart_policy` flags a
container that is running with `RestartPolicy=no`: fine today, gone after the
next reboot.

### `log` + `max_age_h`
```json
"log": "~/logs/job.log",
"max_age_h": 2
```
Silence past the window is `stale`. Set it to about 1.5× the job's own
interval. A missing log file is `failed`.

Do **not** set `max_age_h` on an idle daemon — a quiet gateway writes nothing
and is perfectly healthy. Use `must_be_running` for those.

### `stall`
```json
"stall": {
  "patterns": ["nothing to send", "HTTP 4", "no draft in"],
  "tail_lines": 60,
  "min_hits": 3,
  "note": "exits 0 and produces nothing"
}
```
The expensive failure: on time, exit 0, no output. Give it **every** phrase
that means "did nothing" — a job has more than one way to do nothing, and
watching for only one of them is how six days of a different no-op scored `ok`.

`pattern` (singular) is still accepted.

### `error_lines`
```json
"error_lines": { "tail_lines": 40, "token": "ERROR", "max": 3 }
```
A clean exit with a log full of errors is still a failure.

### `n8n_executions`
```json
"n8n_executions": true
```
Asserts n8n has recorded at least one workflow execution. Copies the SQLite
file **and its `-wal` sidecar** — without the sidecar you read the state of the
last checkpoint, which can be weeks old.

### `cron_job`
```json
"cron_job": "<openclaw job id>",
"watch_max_age_h": 26
```
Reads an OpenClaw cron job's run history, for jobs that no longer write a log.

## `remedy`

```json
"remedy": {
  "action": "launchctl_kickstart",
  "args": { "label": "com.example.job" },
  "max_attempts_per_day": 2,
  "cooldown_minutes": 45,
  "verify_wait": 60
}
```

| Action | `args` |
|---|---|
| `launchctl_kickstart` | `{"label": "com.example.job"}` |
| `docker_start` | `{"container": "name"}` |
| `docker_restart` | `{"container": "name"}` |
| `open_app` | `{"app": "AppName"}` |
| `run` | `{"script": "~/.fleet/something.sh"}` — allow-listed directories only |
| `escalate` | no args; requires `reason` |

| Tuning | Default | Meaning |
|---|---|---|
| `max_attempts_per_day` | 3 | Then escalate and stop trying. |
| `cooldown_minutes` | 20 | Minimum gap; multiplied by the attempt number. |
| `verify_wait` | 12 | Seconds before re-checking. Raise it for slow starters. |

### Escalate rather than guess

```json
"remedy": {
  "action": "escalate",
  "reason": "the session is logged out — re-pairing needs a QR code from your phone: rm -rf ~/app/auth && node ~/app/listen.js"
}
```

Put the actual command in `reason`. The alert is read on a phone by someone who
has lost the context; a reason that says "check the logs" wastes the alert.

Escalate whenever a fix needs a human decision, a credential, a second device,
or a change outside this machine — DNS, a hosting console, a paused cloud task.

## The standard for a new remedy

> Something you would be happy to have run unattended at 4am, three times, and
> then stop.

If that sentence makes you hesitate, it is an `escalate`.
