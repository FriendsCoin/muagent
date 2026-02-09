#!/usr/bin/env bash
set -euo pipefail

# Update visual_posting.mode_weights in config/settings.yaml
# and optionally restart services.
#
# Usage examples:
#   deploy/set_visual_weights.sh --url 0.55 --ascii 0.25 --audio 0.20 --video 0.00
#   deploy/set_visual_weights.sh --url 0.6 --ascii 0.2 --audio 0.2 --video 0 --no-restart
#   deploy/set_visual_weights.sh --dry-run --url 0.55 --ascii 0.25 --audio 0.20 --video 0.00

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SETTINGS_FILE="${SETTINGS_FILE:-$ROOT_DIR/config/settings.yaml}"

URL_W="0.55"
ASCII_W="0.25"
AUDIO_W="0.20"
VIDEO_W="0.00"
RESTART_SERVICES="1"
DRY_RUN="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url) URL_W="${2:-}"; shift 2 ;;
    --ascii) ASCII_W="${2:-}"; shift 2 ;;
    --audio) AUDIO_W="${2:-}"; shift 2 ;;
    --video) VIDEO_W="${2:-}"; shift 2 ;;
    --settings) SETTINGS_FILE="${2:-}"; shift 2 ;;
    --no-restart) RESTART_SERVICES="0"; shift 1 ;;
    --dry-run) DRY_RUN="1"; shift 1 ;;
    -h|--help)
      sed -n '1,18p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

PY_BIN=""
PY_ARGS=()
if command -v python3 >/dev/null 2>&1; then
  PY_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PY_BIN="python"
elif command -v py >/dev/null 2>&1; then
  PY_BIN="py"
  PY_ARGS=(-3)
else
  echo "No Python interpreter found (python3/python/py)." >&2
  exit 1
fi

"$PY_BIN" "${PY_ARGS[@]}" - "$SETTINGS_FILE" "$URL_W" "$ASCII_W" "$AUDIO_W" "$VIDEO_W" "$DRY_RUN" <<'PY'
import re
import sys
from pathlib import Path

settings_path = Path(sys.argv[1])
url_w, ascii_w, audio_w, video_w = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
dry_run = sys.argv[6] == "1"

if not settings_path.exists():
    raise SystemExit(f"settings file not found: {settings_path}")

num = re.compile(r"^(?:0(?:\.\d+)?|1(?:\.0+)?)$")
for name, value in [
    ("url", url_w),
    ("ascii", ascii_w),
    ("audio", audio_w),
    ("video", video_w),
]:
    if not num.match(value):
        raise SystemExit(f"invalid weight for {name}: {value} (expected 0..1)")

lines = settings_path.read_text(encoding="utf-8").splitlines(keepends=True)

visual_idx = None
for i, line in enumerate(lines):
    if re.match(r"^visual_posting:\s*$", line):
        visual_idx = i
        break
if visual_idx is None:
    raise SystemExit("Could not find top-level 'visual_posting:' block")

mode_idx = None
for i in range(visual_idx + 1, len(lines)):
    line = lines[i]
    if line.strip() and not line.startswith("  "):
        break
    if re.match(r"^  mode_weights:\s*$", line):
        mode_idx = i
        break
if mode_idx is None:
    raise SystemExit("Could not find 'visual_posting.mode_weights' block")

end_idx = mode_idx + 1
while end_idx < len(lines):
    line = lines[end_idx]
    if line.strip() == "":
        end_idx += 1
        continue
    if not line.startswith("    "):
        break
    end_idx += 1

replacement = [
    "  mode_weights:\n",
    f"    url: {url_w}\n",
    f"    ascii: {ascii_w}\n",
    f"    audio: {audio_w}\n",
    f"    video: {video_w}\n",
]

new_lines = lines[:mode_idx] + replacement + lines[end_idx:]

if dry_run:
    print(f"[DRY RUN] Would update {settings_path}")
    print("".join(replacement), end="")
else:
    settings_path.write_text("".join(new_lines), encoding="utf-8")
    print(f"Updated {settings_path}")
    print("".join(replacement), end="")
PY

if [[ "$DRY_RUN" == "1" ]]; then
  exit 0
fi

if [[ "$RESTART_SERVICES" == "1" ]]; then
  if command -v systemctl >/dev/null 2>&1; then
    systemctl restart trickster-agent || true
    systemctl restart trickster-admin || true
    echo "Service states:"
    systemctl is-active trickster-agent || true
    systemctl is-active trickster-admin || true
  else
    echo "systemctl not found; skipped service restart."
  fi
fi
