#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$PROJECT_DIR/data"
BACKUP_DIR="$PROJECT_DIR/backups"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

mkdir -p "$BACKUP_DIR"

ts="$(date -u +%Y%m%d-%H%M%S)"
archive="$BACKUP_DIR/data-$ts.tar.gz"

if [[ ! -d "$DATA_DIR" ]]; then
  echo "[backup] data dir not found: $DATA_DIR"
  exit 0
fi

tar -czf "$archive" -C "$PROJECT_DIR" data
echo "[backup] created: $archive"

find "$BACKUP_DIR" -type f -name "data-*.tar.gz" -mtime +"$RETENTION_DAYS" -delete
echo "[backup] retention: deleted files older than $RETENTION_DAYS days"
