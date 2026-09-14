#!/bin/bash
# Log rotation for the agent fleet.
#
# Added 2026-09-14. Before this, nothing rotated: launchd appends to these files
# forever and pm2-logrotate only ever touches pm2 apps, of which there are none.
# The fleet was carrying 46 MB, 6.5 MB of it a single repeating warning line.
#
# Method is copy-then-truncate, NOT move. launchd and the long-running gateways
# hold open file descriptors on these paths; renaming the file would send every
# subsequent write into the renamed copy and leave the live path empty forever.
# Truncating in place keeps the fd valid.
#
# Keeps 2 generations. Anything over MAX_MB is rotated.

MAX_MB=5
KEEP=2
STAMP=$(date '+%F %T')

# >>> EDIT FOR YOUR MACHINE: the paths below are one fleet as an example. <<<
LOGS=(
  "$HOME/.hermes/logs/gateway.error.log"
  "$HOME/.hermes/logs/gateway.log"
  "$HOME/.hermes/logs/agent.log"
  "$HOME/.hermes/logs/errors.log"
  "$HOME/.hermes/logs/watchdog.log"
  "$HOME/Library/Logs/openclaw/gateway.log"
  "$HOME/Library/Logs/openclaw/gateway.err.log"
  "$HOME/cryptonite-agent/logs/cron.log"
  "$HOME/cryptonite-agent/logs/publisher.log"
  "$HOME/cryptonite-agent/logs/doctor.log"
  "$HOME/cryptonite-agent/scanner.log"
  "$HOME/dev/aura8-clipping-agency/logs/engine_out.log"
  "$HOME/dev/aura8-clipping-agency/logs/watchdog_out.log"
  "$HOME/.fleet/watch.log"
  "/tmp/aura8_jarvis.log"
  "/tmp/aura8_heartbeat.log"
)

rotated=0
for f in "${LOGS[@]}"; do
    [ -f "$f" ] || continue
    size_mb=$(( $(stat -f %z "$f") / 1048576 ))
    [ "$size_mb" -lt "$MAX_MB" ] && continue

    i=$KEEP
    while [ "$i" -gt 1 ]; do
        [ -f "$f.$((i-1))" ] && mv -f "$f.$((i-1))" "$f.$i"
        i=$((i-1))
    done
    cp "$f" "$f.1" && : > "$f"
    echo "$STAMP rotated $f (${size_mb}MB)"
    rotated=$((rotated+1))
done

echo "$STAMP rotate pass complete — $rotated file(s) rotated"
