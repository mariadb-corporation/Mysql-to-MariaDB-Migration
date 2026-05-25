#!/usr/bin/env bash
# scripts/12_two_step_dispatch.sh
#
# Variant dispatcher for the two_step data load. The orchestrator calls
# this single entry point. Based on TWO_STEP_VARIANT (set by mariadb-migrator
# at prompt time), it execs the variant-specific implementation:
#
#   TWO_STEP_VARIANT=single_pass (default, also "")  → 12_two_step_sqldata.sh
#   TWO_STEP_VARIANT=resumable                       → 12_two_step_sqldata_resumable.sh
#
# Splitting the variants into separate scripts keeps the well-tested
# single_pass code path untouched by resumable changes, and makes a side-
# by-side diff of the two variants straightforward.

set -euo pipefail

variant="${TWO_STEP_VARIANT:-single_pass}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "$variant" in
  single_pass|"")
    exec "$script_dir/12_two_step_sqldata.sh" "$@"
    ;;
  resumable)
    exec "$script_dir/12_two_step_sqldata_resumable.sh" "$@"
    ;;
  *)
    echo "ERROR: Unknown TWO_STEP_VARIANT: '$variant'" >&2
    echo "       Expected one of: single_pass, resumable" >&2
    exit 1
    ;;
esac
