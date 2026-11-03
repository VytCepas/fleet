#!/usr/bin/env sh
# lint-renderer.sh (harbor#978 item 8, rule name.renderer-used): every writer in standards/naming/writers.tsv
# calls the renderer, and a script that matches a `pattern` row but is no `writer` row is a finding.
#
#   sh lint-renderer.sh [--root DIR]
#
# Each finding prints WARN with the rule id. The rule's mode is controls.tsv's: warn never fails the run,
# block exits 1, shadow is silent. Exit 2 on a usage error.
set -eu

case "$0" in */*) SRC="${0%/*}" ;; *) SRC=. ;; esac
ROOT="${HARBOR_SCOPE_ROOT:-$(CDPATH= cd -- "$SRC/.." && pwd)}"
. "$SRC/library-log.sh"
log_init lint-renderer
die() { echo "lint-renderer: $1" >&2; exit 2; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) [ "$#" -ge 2 ] || die "--root wants a directory"; ROOT="$2"; shift ;;
    -h|--help) awk 'NR < 2 { next } !/^#/ { exit } { sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
    *) die "unknown argument '$1'" ;;
  esac
  shift
done
REG="$ROOT/standards/naming/writers.tsv"; CONTROLS="$ROOT/standards/naming/controls.tsv"
[ -f "$REG" ] || die "no standards/naming/writers.tsv under $ROOT"
TMPDIR="${TMPDIR:-/tmp}"; export TMPDIR
RULE=name.renderer-used
MODE="$(awk -F'\t' -v id="$RULE" '$1 == id { print $2; found = 1; exit } END { if (!found) print "warn" }' "$CONTROLS" 2>/dev/null || echo warn)"
BLOCKED=0
finding() {
  case "$MODE" in
    block) log_fail "[$RULE] $1"; log_fix "call lint-name.sh --render for the name, or register the writer in standards/naming/writers.tsv"; BLOCKED=1 ;;
    shadow) : ;;
    *) log_warn "[$RULE] $1" ;;
  esac
}

log_head "renderer"
# The census: each writer row names a file that must call the renderer.
# A surface with a grammar (a `rule` cell in surfaces.tsv) is rendered; one without can only be checked, so its
# writer calls `lint-name.sh --check` (harbor#978: `--render branch` answers "no grammar").
SURF="$ROOT/standards/naming/surfaces.tsv"
awk -F'\t' '$0 !~ /^#/ && $2 == "writer" { print $3 "\t" $4 }' "$REG" | while IFS='	' read -r _f _s; do
  [ -f "$ROOT/$_f" ] || { echo "MISSING $_f"; continue; }
  _g="$(awk -F'\t' -v s="$_s" '$0 !~ /^#/ && $1 == s { print ($6 == "" || $6 == "-") ? "check" : "render"; exit }' "$SURF" 2>/dev/null || :)"
  case "${_g:-render}" in check) _flag='--check' ;; *) _flag='--render' ;; esac
  grep -q -e 'lint-name\.sh' "$ROOT/$_f" && grep -q -e "$_flag" "$ROOT/$_f" || echo "NORENDER $_f $_flag"
done > "$TMPDIR/lint-renderer.$$"
while IFS=' ' read -r _k _f _flag; do
  case "$_k" in
    MISSING) finding "$_f is a registered writer but is not there" ;;
    NORENDER) finding "$_f writes a name without calling the renderer (lint-name.sh $_flag)" ;;
  esac
done < "$TMPDIR/lint-renderer.$$"
# The scan: a creating command in a script that no writer row names. A comment line is not a command.
awk -F'\t' '$0 !~ /^#/ && $2 == "writer" { print $3 }' "$REG" | sort > "$TMPDIR/lint-renderer.w.$$"
awk -F'\t' '$0 !~ /^#/ && $2 == "pattern" { print $3 }' "$REG" > "$TMPDIR/lint-renderer.p.$$"
find "$ROOT/bin" "$ROOT/hooks" -type f \( -name '*.sh' -o -name '*.py' -o -name '*.ts' \) ! -name '*test*' 2>/dev/null > "$TMPDIR/lint-renderer.f.$$" || :
: > "$TMPDIR/lint-renderer.s.$$"
while IFS= read -r _p; do
  _r="${_p#"$ROOT"/}"
  grep -qxF -e "$_r" "$TMPDIR/lint-renderer.w.$$" && continue
  grep -v '^[[:space:]]*#' "$_p" > "$TMPDIR/lint-renderer.b.$$" || :
  while IFS= read -r _re; do
    if grep -qE -e "^[[:space:]]*([A-Za-z_]+=[^ ]* )*$_re" "$TMPDIR/lint-renderer.b.$$"; then echo "$_r" >> "$TMPDIR/lint-renderer.s.$$"; break; fi
  done < "$TMPDIR/lint-renderer.p.$$"
done < "$TMPDIR/lint-renderer.f.$$"
while IFS= read -r _p; do finding "$_p creates a named thing but is in no writer row (standards/naming/writers.tsv)"; done < "$TMPDIR/lint-renderer.s.$$"
rm -f "$TMPDIR"/lint-renderer.*."$$" "$TMPDIR/lint-renderer.$$"
log_end || :
[ "$BLOCKED" -eq 0 ]
