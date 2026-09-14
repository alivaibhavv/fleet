# Scheduling

Run the watcher and the medic on the same interval. The medic only acts on
problems the watcher has already confirmed twice, so the watcher naturally
leads and no offset is required.

## macOS (launchd)

`~/Library/LaunchAgents/ai.fleet.watch.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.fleet.watch</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>/Users/YOU/.fleet/watch.py</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/Users/YOU/.fleet/watch.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/.fleet/watch.err.log</string>
</dict>
</plist>
```

The medic plist is identical with `ai.fleet.medic`, `medic.py` and
`medic.log`.

```bash
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.fleet.watch.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.fleet.medic.plist
launchctl list | grep ai.fleet
```

### The PATH line is not optional

A launchd job does not inherit your shell's PATH. Docker Desktop installs its
binary at `/Applications/Docker.app/Contents/Resources/bin/docker`, which is on
nobody's default PATH.

This is not hypothetical: it is exactly how every Docker check in the fleet
this came from silently returned "unknown" and inherited a stale `ok` for
months. `watch.py` now resolves the docker binary by absolute path regardless,
but set the PATH anyway — anything you add later will thank you.

## Linux (systemd timers)

`~/.config/systemd/user/fleet-watch.service`:

```ini
[Unit]
Description=fleet watch

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 %h/.fleet/watch.py
```

`~/.config/systemd/user/fleet-watch.timer`:

```ini
[Unit]
Description=Run fleet watch every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now fleet-watch.timer fleet-medic.timer
```

## cron

```cron
*/15 * * * * /usr/bin/python3 $HOME/.fleet/watch.py >> $HOME/.fleet/watch.log 2>&1
*/15 * * * * /usr/bin/python3 $HOME/.fleet/medic.py >> $HOME/.fleet/medic.log 2>&1
```

cron's PATH is even smaller than launchd's. Set `PATH=` at the top of the
crontab.

## Housekeeping

Both scripts append to their logs forever. `rotate_logs.sh` caps every log at
5 MB × 2 generations; schedule it daily.

It rotates by **copy-then-truncate, never rename**. launchd and long-running
gateways hold open file descriptors on their log paths — rename the file and
every later write follows the descriptor into the renamed copy, leaving the
live log permanently empty. Truncating in place keeps the descriptor valid.

## Single instance

Both scripts take a lock file and exit quietly if another run is in progress,
so overlapping schedules cannot corrupt the transition state. A lock older than
the timeout is treated as stale and reclaimed.
