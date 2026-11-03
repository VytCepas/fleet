#!/usr/bin/env sh
# doc-check.sh -- docs front matter and generated-file headers, judged by the standard's doc-shape.sh.
#
#   sh gates/doc-check.sh             report findings (the controls are born warn: exit 0)
#   sh gates/doc-check.sh --strict    any finding is red, exit 1
#
# THE JUDGES are vendor/naming/doc-shape.sh (docs/**/*.md carry kind, owner and status; a generated-by header
# equals its contracts/fresh-checks.tsv row) and vendor/naming/doc-fresh-verify.sh (is each generated file
# current). Both ship in the vendored package, so CI needs no checkout of the standard. DOC_ROOT=<dir>
# judges <dir> while the judges stay the ones beside this script. No vendored package: UNCHECKED, exit 3.
set -eu
PKG_HOME=$(cd "$(dirname "$0")/.." && pwd)
ROOT=$(cd "${DOC_ROOT:-$PKG_HOME}" && pwd)
V="$PKG_HOME/vendor/naming"
for f in doc-shape.sh doc-fresh-verify.sh; do
  [ -f "$V/$f" ] || { printf 'doc-check: UNCHECKED, vendor/naming/%s is missing (re-vendor: sh <standard checkout>/bin/standard-vendor.sh .)\n' "$f" >&2; exit 3; }
done
rc=0
sh "$V/doc-shape.sh" --root "$ROOT" --registry "$V/registry" "$@" || rc=1
if [ -f "$ROOT/contracts/fresh-checks.tsv" ]; then
  sh "$V/doc-fresh-verify.sh" --root "$ROOT" || rc=1
fi
exit "$rc"
