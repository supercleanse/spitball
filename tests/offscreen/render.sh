#!/usr/bin/env bash
# Renders the settings overlay's card offscreen, one PNG per section, without
# touching the live shell or desktop: Quickshell runs with
# QT_QPA_PLATFORM=offscreen against tests/offscreen/fake_spitball.py, so no
# window maps, no real config is read, and nothing is written outside the
# output directory.
#
# Usage: tests/offscreen/render.sh --fake-data <out-dir>
#
# --fake-data is required on purpose: it's the explicit acknowledgment that
# every value on screen is canned (see fake_spitball.py). There is no mode
# that renders against the real CLI.
#
# Needs: quickshell, and Omarchy's shell kit at $OMARCHY_PATH/shell (default
# /usr/share/omarchy/shell) for the qs.Commons / qs.Ui imports.
set -euo pipefail

usage() {
  echo "usage: $0 --fake-data <out-dir>" >&2
  exit 2
}

[[ $# -eq 2 && "$1" == "--fake-data" ]] || usage
OUT=$(realpath -m "$2")
mkdir -p "$OUT"

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SHELL_KIT=${OMARCHY_PATH:-/usr/share/omarchy}/shell
command -v quickshell >/dev/null || { echo "quickshell not found" >&2; exit 1; }
[[ -d "$SHELL_KIT/Commons" && -d "$SHELL_KIT/Ui" ]] || { echo "Omarchy shell kit not found at $SHELL_KIT" >&2; exit 1; }

# Quickshell resolves `qs.Commons` / `qs.Ui` relative to the config root, so
# the harness runs from a scratch root that links the kit and the plugin's
# own files side by side. The scratch root lives under the output dir.
STAGE="$OUT/.harness"
rm -rf "$STAGE"
mkdir -p "$STAGE/runtime"
ln -s "$SHELL_KIT/Commons" "$STAGE/Commons"
ln -s "$SHELL_KIT/Ui" "$STAGE/Ui"
ln -s "$ROOT/settings" "$STAGE/settings"
ln -s "$ROOT/Model.js" "$STAGE/Model.js"
ln -s "$ROOT/manifest.json" "$STAGE/manifest.json"
cp "$ROOT/tests/offscreen/harness.qml" "$STAGE/shell.qml"

export QT_QPA_PLATFORM=offscreen
export SPITBALL_SHOT_DIR="$OUT"
export SPITBALL_FAKE_CLI="$ROOT/tests/offscreen/fake_spitball.py"
export SPITBALL_FAKE_RUNTIME="$STAGE/runtime"
export SPITBALL_FAKE_LOG="$OUT/fake-cli-calls.log"
: > "$SPITBALL_FAKE_LOG"

LOG="$OUT/quickshell.log"
set +e
timeout 40 quickshell -p "$STAGE/shell.qml" >"$LOG" 2>&1
rc=$?
set -e
sed -i 's/\x1b\[[0-9;]*m//g' "$LOG"

if grep -qE "ERROR|Failed to load|is not a type|Cannot assign|ReferenceError|TypeError|Unable to assign" "$LOG"; then
  echo "harness: QML errors (see $LOG):" >&2
  grep -E "ERROR|Failed to load|is not a type|Cannot assign|ReferenceError|TypeError|Unable to assign" "$LOG" >&2
  exit 1
fi

count=$(ls "$OUT"/settings-*.png 2>/dev/null | wc -l)
echo "harness: $count screenshot(s) in $OUT (quickshell exit $rc)"
[[ $count -gt 0 ]]
