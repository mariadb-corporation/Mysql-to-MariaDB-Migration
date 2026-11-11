#!/usr/bin/env bash
# Emit third-party-licenses.txt from the vendored dependency tree.
# Reads orchestrator/requirements.lock as the source of truth; fails if any
# pinned package yields no license text.
#
#   usage: build/gen_licenses.sh <vendor-dir> <out-file>
set -euo pipefail

VENDOR="${1:?usage: gen_licenses.sh <vendor-dir> <out-file>}"
OUT="${2:?usage: gen_licenses.sh <vendor-dir> <out-file>}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK="${HERE}/../orchestrator/requirements.lock"
HEADER="${HERE}/license-header.txt"
EXTRAS="${HERE}/license-extras"

[[ -d "$VENDOR" ]] || { echo "vendor dir not found: $VENDOR" >&2; exit 1; }
[[ -f "$LOCK"   ]] || { echo "lock file not found: $LOCK" >&2; exit 1; }
[[ -f "$HEADER" ]] || { echo "header not found: $HEADER" >&2; exit 1; }

cat "$HEADER" > "$OUT"
missing=0
count=0

while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="$(echo "$line" | tr -d '[:space:]')"
    [[ -z "$line" ]] && continue
    [[ "$line" == *"=="* ]] || continue

    pkg="${line%%==*}"
    ver="${line##*==}"
    # dist-info dirs normalise '-' and '.' to '_'
    norm="$(echo "$pkg" | tr '.-' '__')"

    d="$(find "$VENDOR" -maxdepth 1 -type d -iname "${norm}-${ver}.dist-info" | head -1)"
    if [[ -z "$d" ]]; then
        echo "ERROR: no dist-info for ${pkg}==${ver} in ${VENDOR}" >&2
        missing=1
        continue
    fi

    found=0
    shopt -s nullglob
    for f in "$d"/licenses/* "$d"/LICENSE* "$d"/COPYING* "$d"/NOTICE*; do
        [[ -f "$f" ]] || continue
        printf '\n===== %s %s =====\n--- %s ---\n\n' \
            "$pkg" "$ver" "$(basename "$f")" >> "$OUT"
        cat "$f" >> "$OUT"
        found=1
    done
    # files upstream ships only in the sdist (e.g. Pygments AUTHORS)
    for x in "$EXTRAS"/"${pkg}"-*; do
        [[ -f "$x" ]] || continue
        printf '\n--- %s (%s) ---\n\n' "$(basename "$x")" "$pkg" >> "$OUT"
        cat "$x" >> "$OUT"
        found=1
    done
    shopt -u nullglob

    if [[ "$found" -eq 0 ]]; then
        echo "ERROR: no license text for ${pkg}==${ver}" >&2
        missing=1
    else
        count=$((count + 1))
    fi
done < "$LOCK"

[[ "$missing" -eq 0 ]] || exit 1
echo "wrote $OUT (${count} packages)"
