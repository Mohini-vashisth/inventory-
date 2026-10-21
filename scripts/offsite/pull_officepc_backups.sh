#!/usr/bin/env bash
# Runs on the off-site Linux laptop ("homelab"), NOT on the office PC.
# Pulls the office PC's db_backups\ folder over Tailscale and keeps a copy here.
#
# The office PC only lets this laptop's key run one fixed command (a tar of
# db_backups, see administrators_authorized_keys there), so this script needs
# no arguments and cannot ask the office PC to do anything else.
set -euo pipefail

HOST=100.84.40.23                      # office PC "mdw" on the techteammdw tailnet
REMOTE_USER=matta
KEY="$HOME/.ssh/officepc_backup"
DEST="$HOME/backups/officepc"
KEEP_DAYS=60                           # database snapshots older than this are deleted here
LOG="$DEST/pull.log"

mkdir -p "$DEST/db_backups"
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# Never run two pulls at once (e.g. a slow run overlapping the next one).
exec 9>"$DEST/.lock"
flock -n 9 || { log "SKIP another pull is still running"; exit 0; }

STAGE=$(mktemp -d "$DEST/.stage.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT

# 1. Download the archive into a staging folder, never straight into the kept copy.
#    Retry a few times: right after a boot the network / Tailscale may not be up yet
#    (the catch-up run after a power cut fires at exactly that moment).
ok=0
for attempt in 1 2 3 4 5; do
    if ssh -T -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=30 \
            "$REMOTE_USER@$HOST" > "$STAGE/pull.tgz" 2> "$STAGE/ssh.err"; then
        ok=1; break
    fi
    sleep 60
done
if [ "$ok" -ne 1 ]; then
    err=$(tr '\n' ' ' < "$STAGE/ssh.err")
    log "FAIL ssh after 5 tries: $err"
    # The office PC being off or offline is expected (it is switched off at night);
    # anything else (refused key, wrong command, ...) is a real problem worth an email.
    if echo "$err" | grep -qi -E 'timed out|no route to host|network is unreachable|could not resolve|connection refused'; then
        echo "unreachable" > "$DEST/last_status"
    else
        echo "error: ssh failed: $err" > "$DEST/last_status"
    fi
    exit 1
fi
if ! tar -tzf "$STAGE/pull.tgz" > /dev/null 2>&1; then
    log "FAIL archive is not a valid tar.gz ($(stat -c%s "$STAGE/pull.tgz") bytes)"
    echo "error: the downloaded archive is not a valid tar.gz" > "$DEST/last_status"
    exit 1
fi
tar -xzf "$STAGE/pull.tgz" -C "$STAGE"

# 2. Check every database snapshot opens and passes SQLite's own integrity check.
bad=()
for f in "$STAGE"/db_backups/db_*.sqlite3; do
    [ -e "$f" ] || continue
    if ! python3 -I - "$f" <<'PY'
import sqlite3, sys
# immutable=1: read the file as-is and create no -wal/-shm files beside it.
con = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro&immutable=1", uri=True)
sys.exit(0 if con.execute("PRAGMA integrity_check").fetchone()[0] == "ok" else 1)
PY
    then
        bad+=("$(basename "$f")")
    fi
done

# 3. Copy into the kept folder. No --delete: the office PC prunes at 30 days,
#    this copy keeps 60 and must not mirror that deletion. Corrupt snapshots are skipped.
excludes=(--exclude '*.sqlite3-wal' --exclude '*.sqlite3-shm')
for b in "${bad[@]}"; do excludes+=(--exclude "$b"); done
rsync -a "${excludes[@]}" "$STAGE/db_backups/" "$DEST/db_backups/"

# 4. Prune old snapshots (mtime is the snapshot time, preserved by rsync -a).
find "$DEST/db_backups" -maxdepth 1 -name 'db_*.sqlite3' -mtime +"$KEEP_DAYS" -delete

count=$(find "$DEST/db_backups" -maxdepth 1 -name 'db_*.sqlite3' | wc -l)
latest=$(ls -1 "$DEST/db_backups" | grep '^db_.*\.sqlite3$' | tail -1)
if [ "${#bad[@]}" -gt 0 ]; then
    log "WARN skipped corrupt snapshots: ${bad[*]}"
    log "OK-WITH-WARNINGS $count snapshots kept, newest $latest"
    echo "error: corrupt snapshot(s) on the office PC: ${bad[*]}" > "$DEST/last_status"
    exit 1
fi
date +%s > "$DEST/last_success"
echo "ok" > "$DEST/last_status"
log "OK $count snapshots kept, newest $latest"
