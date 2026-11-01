#!/bin/sh
# doc-check.sh -- docs front matter and generated-file headers over this repo, judged by the standard's own scripts.
#
#   NAMING_STANDARD_REPO=<checkout> sh gates/doc-check.sh [--strict]
#
# The judges are bin/doc-shape.sh (docs/**/*.md carry kind, owner and status; a generated-by header equals its
# contracts/fresh-checks.tsv row) and bin/doc-fresh-verify.sh (is each generated file current) in the checkout that
# owns the naming standard, as gates/naming-check.sh reads bin/lint-name.sh. No checkout: exit 3, never a pass.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
STANDARD="${NAMING_STANDARD_REPO:-}"
[ -n "$STANDARD" ] && [ -f "$STANDARD/bin/doc-shape.sh" ] && [ -f "$STANDARD/bin/doc-fresh-verify.sh" ] || {
  printf 'doc-check: UNCHECKED, set NAMING_STANDARD_REPO to the checkout that owns the naming standard\n' >&2
  exit 3
}
rc=0
sh "$STANDARD/bin/doc-shape.sh" --root "$ROOT" --registry "$STANDARD/standards/naming" "$@" || rc=1
HARBOR_SCOPE_ROOT="$ROOT" sh "$STANDARD/bin/doc-fresh-verify.sh" --table "$ROOT/contracts/fresh-checks.tsv" || rc=1
exit "$rc"
