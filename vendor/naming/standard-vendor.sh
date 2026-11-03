#!/usr/bin/env sh
# standard-vendor.sh (harbor#967 item 4, decision harbor#1002) — vendors the naming package into a repo.
#
#   sh bin/standard-vendor.sh <repo>            write <repo>/vendor/naming/: lint-name.sh, lint-renderer.sh, standard-register.sh, library-log.sh,
#                                               registry/*.tsv and MANIFEST.tsv (header + one row per file)
#   sh bin/standard-vendor.sh --check <repo>    report drift from harbor; writes nothing
#   sh vendor/naming/standard-vendor.sh --check --offline <repo>
#                                               a workload's CI, with no harbor: every vendored file against its own
#                                               manifest row (edited, missing, extra); no stale or version judgement
#   options: --source <dir> (the harbor tree to vendor from; default this checkout)
#   exit 0 written / current · 1 drift (stale, edited, missing or extra file) · 2 usage · 3 unchecked (no manifest, no source)
# This file ships in the package (harbor#967): the workload's own copy is what its CI runs, offline.
#
# The vendored lint runs offline and needs no flags: bare, it judges the repo that carries it with the registry beside it
# (workload names only, harbor#1415); a name at its birth: sh vendor/naming/lint-name.sh --check <surface> <name>
# MANIFEST.tsv opens with `#` header lines (generated, harbor commit, standard_version) then `path sha256 source` rows.
# --check says three different things: `stale` (the manifest differs from harbor now: upgrade), `edited` (the file differs
# from its own manifest row: someone edited a generated layer) and `missing`/`extra`. The harbor commit is the pin the
# fleet reads for lag; drift itself is judged on content, never on the commit, so vendoring into harbor does not chase its own HEAD.
set -eu
LC_ALL=C; export LC_ALL
case "$0" in */*) SRC="${0%/*}" ;; *) SRC=. ;; esac
SRC="$(CDPATH= cd -- "$SRC" && pwd -P)"
. "$SRC/library-log.sh"
# shellcheck disable=SC2034 # LOG_FD: read by library-log.sh
LOG_FD=2; log_init standard-vendor

usage() { awk 'NR < 2 { next } !/^#/ { exit } { sub(/^# ?/, ""); print }' "$SRC/standard-vendor.sh"; }
die2() { log_fail "$1"; log_fix "$2"; log_end || :; exit 2; }

MODE=write; OFFLINE=0; REPO=""; HSRC="$(CDPATH= cd -- "$SRC/.." && pwd -P)"
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --check) MODE=check ;;
    --offline) OFFLINE=1 ;;
    --source) [ $# -ge 2 ] || die2 "--source wants a directory" "sh $SRC/standard-vendor.sh --source ~/port/harbor <repo>"; HSRC="$2"; shift ;;
    -*) die2 "unknown flag: $1" "sh $SRC/standard-vendor.sh --help" ;;
    *) [ -z "$REPO" ] || die2 "one repo at a time" "sh $SRC/standard-vendor.sh <repo>"; REPO="$1" ;;
  esac
  shift
done
[ -n "$REPO" ] || die2 "no repo given" "sh $SRC/standard-vendor.sh <repo-path>"
[ -d "$REPO" ] || die2 "no such directory: $REPO" "sh $SRC/standard-vendor.sh <an existing repo>"
HSRC="$(CDPATH= cd -- "$HSRC" && pwd -P)"; REPO="$(CDPATH= cd -- "$REPO" && pwd -P)"
DEST="$REPO/vendor/naming"; MAN="$DEST/MANIFEST.tsv"
[ "$OFFLINE" = 0 ] || [ "$MODE" = check ] || die2 "--offline only checks: writing needs a harbor tree" "sh <a harbor checkout>/bin/standard-vendor.sh $REPO"

if command -v shasum >/dev/null 2>&1; then sha() { shasum -a 256 "$1" | cut -c1-64; }; else sha() { sha256sum "$1" | cut -c1-64; }; fi

# The package: <path under vendor/naming> TAB <source path under the harbor tree>, one per line. The kit manifest
# (standards/naming/kit.tsv, harbor#1436) names it: its vendored=yes tool and register rows, so "yard vendors it" is one list.
# A tree without the manifest (an older harbor) is vendored the way it was: the fixed tools and every standards/naming/*.tsv.
package() {
  if [ -f "$HSRC/standards/naming/kit.tsv" ]; then
    sh "$HSRC/bin/standard-kit.sh" --root "$HSRC" --registry "$HSRC/standards/naming" --package; return
  fi
  printf 'lint-name.sh\tbin/lint-name.sh\nlint-renderer.sh\tbin/lint-renderer.sh\nlibrary-log.sh\tbin/library-log.sh\n'
  printf 'standard-vendor.sh\tbin/standard-vendor.sh\nstandard-register.sh\tbin/standard-register.sh\n'
  printf 'doc-shape.sh\tbin/doc-shape.sh\ndoc-fresh-verify.sh\tbin/doc-fresh-verify.sh\n'   # harbor#1434, #1435
  for _f in "$HSRC"/standards/naming/*.tsv; do
    [ -f "$_f" ] || continue
    printf 'registry/%s\tstandards/naming/%s\n' "${_f##*/}" "${_f##*/}"
  done
}
[ "$OFFLINE" = 1 ] || for _need in bin/lint-name.sh bin/lint-renderer.sh bin/library-log.sh bin/standard-vendor.sh bin/standard-register.sh bin/doc-shape.sh bin/doc-fresh-verify.sh bin/standard-kit.sh standards/naming/registers.tsv; do
  [ -f "$HSRC/$_need" ] || { log_fail "no $_need under $HSRC (unchecked)"; log_fix "sh $SRC/standard-vendor.sh --source <a harbor checkout> $REPO"; log_end || :; exit 3; }
done
SV="$(sed -n 's/^# standard_version: *//p' "$HSRC/standards/naming/registers.tsv" 2>/dev/null | head -n 1)" || SV=""
[ -n "$SV" ] || [ "$OFFLINE" = 1 ] || { log_fail "registers.tsv carries no standard_version (unchecked)"; log_fix "restore the '# standard_version:' line in $HSRC/standards/naming/registers.tsv"; log_end || :; exit 3; }
COMMIT="$(git -C "$HSRC" rev-parse HEAD 2>/dev/null || echo unknown)"

if [ "$MODE" = write ]; then
  rm -rf "$DEST.new"; mkdir -p "$DEST.new/registry"
  {
    # harbor's own row says the repo-relative command; any other repo's row names the checkout it is run from (harbor#1434)
    if [ "$REPO" = "$HSRC" ]; then printf '# generated-by: sh bin/standard-vendor.sh .\n'; else printf '# generated-by: sh <standard checkout>/bin/standard-vendor.sh .\n'; fi
    printf '# MANIFEST.tsv — GENERATED by bin/standard-vendor.sh, do not edit: re-run it (harbor#967, #1002).\n'
    printf '# harbor_commit: %s\n# standard_version: %s\n# columns: path\tsha256\tsource\n' "$COMMIT" "$SV"
    package | while IFS="$(printf '\t')" read -r _p _s; do
      cp "$HSRC/$_s" "$DEST.new/$_p"
      printf '%s\t%s\t%s\n' "$_p" "$(sha "$DEST.new/$_p")" "$_s"
    done
  } > "$DEST.new/MANIFEST.tsv"
  chmod +x "$DEST.new/lint-name.sh" "$DEST.new/lint-renderer.sh" "$DEST.new/standard-vendor.sh" "$DEST.new/standard-register.sh" "$DEST.new/standard-kit.sh" 2>/dev/null || :
  rm -rf "$DEST"; mkdir -p "$REPO/vendor"; mv "$DEST.new" "$DEST"
  _n="$(grep -vc '^#' "$MAN")"
  log_ok "vendored naming@$SV ($_n files) from harbor ${COMMIT%"${COMMIT#???????}"} into $DEST"
  log_end || :; exit 0
fi

# --check
[ -f "$MAN" ] || { log_fail "no $MAN: nothing is vendored here (unchecked)"; log_fix "sh $SRC/standard-vendor.sh $REPO"; log_end || :; exit 3; }
PIN="$(sed -n 's/^# harbor_commit: *//p' "$MAN" | head -n 1)"; PINV="$(sed -n 's/^# standard_version: *//p' "$MAN" | head -n 1)"
DRIFT=0
if [ "$OFFLINE" = 1 ]; then REFIX="re-vendor from harbor: sh <a harbor checkout>/bin/standard-vendor.sh $REPO"; else REFIX="sh $SRC/standard-vendor.sh $REPO"; fi
bad() { DRIFT=$((DRIFT + 1)); log_fail "$1"; log_fix "$REFIX"; }
TAB="$(printf '\t')"
# Every manifest row against the file on disk (edited, missing) and against harbor now (stale).
grep -v '^#' "$MAN" | { while IFS="$TAB" read -r _p _h _s; do
    [ -f "$DEST/$_p" ] || { echo "missing $_p"; continue; }
    [ "$(sha "$DEST/$_p")" = "$_h" ] || echo "edited $_p"
    [ "$OFFLINE" = 1 ] && continue
    if [ -f "$HSRC/$_s" ]; then [ "$(sha "$HSRC/$_s")" = "$_h" ] || echo "stale $_p"; else echo "stale $_p (gone from harbor)"; fi
  done
  # A file harbor now ships that the manifest lacks, and a vendored file no row names.
  [ "$OFFLINE" = 1 ] || package | while IFS="$TAB" read -r _p _s; do grep -q "^$_p$TAB" "$MAN" || echo "new $_p"; done
  (cd "$DEST" && find . -type f ! -name MANIFEST.tsv | sed 's|^\./||') | while read -r _p; do
    grep -q "^$_p$TAB" "$MAN" || echo "extra $_p"; done
} > "${TMPDIR:-/tmp}/standard-vendor.$$" || :
while read -r _k _rest; do bad "$_k: vendor/naming/$_rest"; done < "${TMPDIR:-/tmp}/standard-vendor.$$"
rm -f "${TMPDIR:-/tmp}/standard-vendor.$$"
[ "$OFFLINE" = 1 ] || [ "$PINV" = "$SV" ] || bad "standard_version: pinned $PINV, harbor is at $SV"
if [ "$DRIFT" -eq 0 ] && [ "$OFFLINE" = 1 ]; then
  log_ok "naming@$PINV: every vendored file matches its manifest row (offline; currency with harbor not judged)"; log_end || :; exit 0
fi
if [ "$DRIFT" -eq 0 ]; then
  log_ok "naming@$PINV is current with harbor (pinned at ${PIN%"${PIN#???????}"})"; log_end || :; exit 0
fi
log_note "pinned at ${PIN%"${PIN#???????}"}; $DRIFT drift finding(s)" 2>/dev/null || :
log_end || :; exit 1
