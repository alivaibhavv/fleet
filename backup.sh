#!/bin/bash
# Nightly backup of the fleet's irreplaceable state.
#
# Added 2026-09-14. cryptonite-backup.sh existed but nothing ever ran it, and
# OpenClaw's own backup_runs table was empty. Everything below existed in exactly
# one copy: losing the WhatsApp auth folder means re-pairing by QR, losing the
# OpenClaw sqlite means losing cron jobs, sessions and memory.
#
# SQLite files are copied with `.backup`, not cp — a plain copy of a live
# database with an active -wal is not guaranteed to be restorable.

set -u
# >>> EDIT FOR YOUR MACHINE: the paths below are one fleet as an example. <<<
DEST="$HOME/fleet-backups"
STAMP=$(date '+%Y%m%d-%H%M')
OUT="$DEST/$STAMP"
KEEP_DAYS=14

mkdir -p "$OUT"
log() { echo "$(date '+%F %T') $*"; }
log "backup starting -> $OUT"

sqlite_backup() {
    src="$1"; name="$2"
    [ -f "$src" ] || { log "SKIP $name (not found)"; return; }
    if /usr/bin/sqlite3 "$src" ".backup '$OUT/$name'" 2>/dev/null; then
        log "ok  $name ($(du -h "$OUT/$name" | cut -f1))"
    else
        cp "$src" "$OUT/$name" && log "ok  $name (plain copy fallback)"
    fi
}

# --- live databases ---
sqlite_backup "$HOME/.openclaw/state/openclaw.sqlite" "openclaw-state.sqlite"
DOCKER=/Applications/Docker.app/Contents/Resources/bin/docker
# The -wal sidecar is not optional: n8n last checkpointed its main db file on
# 30 Aug, so a copy of database.sqlite alone restores a two-week-old workflow set.
if $DOCKER cp n8n:/home/node/.n8n/database.sqlite "$OUT/n8n-database.sqlite" 2>/dev/null; then
    $DOCKER cp n8n:/home/node/.n8n/database.sqlite-wal "$OUT/n8n-database.sqlite-wal" 2>/dev/null
    $DOCKER cp n8n:/home/node/.n8n/database.sqlite-shm "$OUT/n8n-database.sqlite-shm" 2>/dev/null
    log "ok  n8n-database.sqlite (+wal)"
else
    log "SKIP n8n (container unreachable)"
fi
# A portable, self-contained export of the workflows as well - restoring from
# JSON is far less fragile than restoring a sqlite file into a new n8n version.
$DOCKER exec n8n n8n export:workflow --all --output=/tmp/wf.json >/dev/null 2>&1 \
    && $DOCKER cp n8n:/tmp/wf.json "$OUT/n8n-workflows.json" 2>/dev/null \
    && log "ok  n8n-workflows.json"

# --- credentials and pairings that cannot be regenerated without a human ---
[ -d "$HOME/dev/wa-digest/data/auth" ] && \
    tar czf "$OUT/wa-digest-auth.tgz" -C "$HOME/dev/wa-digest/data" auth 2>/dev/null \
    && log "ok  wa-digest-auth.tgz"

# --- configuration ---
tar czf "$OUT/config.tgz" \
    -C "$HOME" \
    .openclaw/openclaw.json \
    .fleet/registry.json .fleet/watch.py .fleet/state.json \
    .codex/config.toml .codex/AGENTS.md \
    2>/dev/null && log "ok  config.tgz"

tar czf "$OUT/launchagents.tgz" -C "$HOME/Library" LaunchAgents 2>/dev/null \
    && log "ok  launchagents.tgz"

# --- agent scripts ---
tar czf "$OUT/agent-scripts.tgz" \
    -C "$HOME" \
    --exclude='*/venv/*' --exclude='*/node_modules/*' --exclude='*.log' \
    cryptonite-agent .hermes/scripts \
    2>/dev/null && log "ok  agent-scripts.tgz"

SIZE=$(du -sh "$OUT" | cut -f1)
log "backup complete: $SIZE in $OUT"

# --- retention ---
find "$DEST" -maxdepth 1 -type d -name '20*' -mtime +$KEEP_DAYS -print -exec rm -rf {} + 2>/dev/null \
    | while read -r old; do log "pruned $old"; done

log "kept $(ls -1d "$DEST"/20* 2>/dev/null | wc -l | tr -d ' ') backup set(s)"
