#!/usr/bin/env sh
# doc-fresh-verify.sh (harbor#1287) — is every generated file current? One verdict line per file, and the exact fix for a stale one.
#
#   sh bin/doc-fresh-verify.sh [--table FILE] [--root DIR]    exit 0 all fresh (or skipped/unchecked), 1 any stale, 2 usage
#
# WHY. 7 of 12 failed PR runs 2026-10-14..17 were a stale generated file (docs/generated/status.md, docs/context-map.md, a derived
# register or CODEOWNERS block), each found only after the 15-20 min suite pool. Every one of them has a generator
# --check that answers in seconds. The files and their checks are contracts/fresh-checks.tsv; the checks run in parallel,
# so the wall time is the slowest one (docs-gen, ~11 s), not the sum.
#
# A check that exits other than 0 or 1 could not judge (no python3, no git): UNCHECKED is a WARN line and does not fail, since
# a false red here is a push nobody can make. Skipped rows have nothing to judge in this tree (an export has no dev layer).
set -eu
case "$0" in */*) SRC="${0%/*}" ;; *) SRC=. ;; esac
. "$SRC/library-log.sh"
log_init fresh-check
_up=..; case "$(CDPATH= cd -- "$SRC" && pwd)" in */vendor/naming) _up=../.. ;; esac   # vendored: the repo is two levels up (harbor#1434)
ROOT="${HARBOR_SCOPE_ROOT:-$(CDPATH= cd -- "$SRC/$_up" && pwd)}"
TABLE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --table) [ $# -ge 2 ] || { echo "fresh-check: --table needs a file" >&2; exit 2; }; TABLE="$2"; shift ;;
    --root) [ $# -ge 2 ] || { echo "fresh-check: --root needs a directory" >&2; exit 2; }; ROOT="$2"; shift ;;
    -h|--help) awk 'NR<2{next} /^#/{ sub(/^# ?/,""); if ($0=="") exit; print; next } {exit}' "$0"; exit 0 ;;
    *) echo "fresh-check: unknown argument $1 (sh $0 --help)" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$TABLE" ] || TABLE="$ROOT/contracts/fresh-checks.tsv"
[ -f "$TABLE" ] || { echo "fresh-check: no table at $TABLE" >&2; exit 2; }
cd "$ROOT"
OUT="$(mktemp -d "${TMPDIR:-/tmp}/fresh-check.XXXXXX")" || exit 2
trap 'rm -rf "$OUT"' EXIT

TAB="$(printf '\t')"; n=0
while IFS="$TAB" read -r f chk fix needs; do
  case "$f" in ''|\#*|-) continue ;; esac
  n=$((n + 1)); needs="${needs:-$f}"
  printf '%s\n' "$f" > "$OUT/$n.file"; printf '%s\n' "$fix" > "$OUT/$n.fix"
  if [ ! -e "$needs" ] && ! git ls-files --error-unmatch -- "$needs" >/dev/null 2>&1; then
    echo skip > "$OUT/$n.rc"; continue
  fi
  ( rc=0; HARBOR_DOC_IN_FRESH_CHECK=1 sh -c "$chk" > "$OUT/$n.out" 2>&1 || rc=$?; echo "$rc" > "$OUT/$n.rc" ) &
done < "$TABLE"
wait

stale=0; i=1
while [ "$i" -le "$n" ]; do
  f="$(cat "$OUT/$i.file")"; rc="$(cat "$OUT/$i.rc" 2>/dev/null || echo 255)"
  case "$rc" in
    0)    log_ok "$f is current" ;;
    skip) log_skip "$f: nothing to judge in this tree" ;;
    1)    stale=$((stale + 1)); log_fail "$f is STALE"; log_fix "$(cat "$OUT/$i.fix")" ;;
    *)    log_warn "$f UNCHECKED: the check exited $rc ($(grep -v '^$' "$OUT/$i.out" 2>/dev/null | tail -1 | cut -c1-120))" ;;
  esac
  i=$((i + 1))
done
log_end || :
[ "$stale" -eq 0 ] || { echo "fresh-check: $stale generated file(s) STALE: run each fix above, commit, push." >&2; exit 1; }
