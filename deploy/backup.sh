#!/bin/sh
# Ежедневный бэкап базы смен. В crontab:
#   0 3 * * * /opt/zavodBot/deploy/backup.sh
set -eu
DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$DIR/backups"
mkdir -p "$OUT"
sqlite3 "$DIR/shifts.db" ".backup '$OUT/shifts-$(date +%F).db'"
# храним две недели
find "$OUT" -name 'shifts-*.db' -mtime +14 -delete
