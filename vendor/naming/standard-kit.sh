#!/usr/bin/env sh
# standard-kit.sh (harbor#1436, #1423 T18.13; check id `standard.kit-complete`) - what a repo of each kind inherits.
#
#   sh standard-kit.sh --check                 the kit against the registers, both ways: every register in schemas.tsv and every
#                                              control family in controls.tsv has a row, and every row names something that exists
#   sh standard-kit.sh --render <kind>         the kind's items as TSV: id item ref share vendored (yard renders exactly these)
#   sh standard-kit.sh --package               the vendored package yard copies: <path in vendor/naming> TAB <source in harbor>
#   sh standard-kit.sh --carries <repo> [--kind <kind>]   does the repo hold every item its kind lists (the kind is the
#                                              repo-kind: line of .agents/config.yaml unless given)
#   sh standard-kit.sh --core                  the core-share registers under standards/naming, one file name per line (what a clean box gets)
#   sh standard-kit.sh --core-tools            the tools of the core share (bin/ file names), the engines and readers a clean box keeps
#   sh standard-kit.sh --core-file <name>      that register as the core share ships it: schemas.tsv, controls.tsv, kit.tsv and registers.tsv
#                                              cut to the core share, exemptions.tsv empty (a new system has no legacy), every other file whole
#   options: --registry <dir> (default standards/naming, else the registry beside a vendored copy) --root <dir>
#
# Exit 0 complete / carries everything, 1 a finding (one line each, `kit: <what>`), 2 usage, 3 unchecked (no kit.tsv).
set -u
LC_ALL=C; export LC_ALL
case "$0" in */*) SRC="${0%/*}" ;; *) SRC=. ;; esac
SRC="$(CDPATH= cd -- "$SRC" && pwd -P)"
# shellcheck disable=SC2034 # LOG_FD: read by library-log.sh
LOG_FD=2; . "$SRC/library-log.sh"; log_init standard-kit
TAB="$(printf '\t')"

die() { log_fail "$1"; log_fix "${2:-sh $0 --help}"; log_end || :; exit 2; }
usage() { awk 'NR < 2 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$SRC/standard-kit.sh"; }

MODE=''; KIND=''; REPO=''; REG=''; ROOT=''
while [ $# -gt 0 ]; do
  case "$1" in
    --check|--package|--core|--core-tools) MODE="$1"; shift ;;
    --core-file) MODE="$1"; [ $# -ge 2 ] || die "--core-file takes a register file name"; KIND="$2"; shift 2 ;;
    --render) MODE="$1"; [ $# -ge 2 ] || die "--render takes a repo kind"; KIND="$2"; shift 2 ;;
    --carries) MODE="$1"; [ $# -ge 2 ] || die "--carries takes a repo directory"; REPO="$2"; shift 2 ;;
    --kind) [ $# -ge 2 ] || die "--kind takes a repo kind"; KIND="$2"; shift 2 ;;
    --registry) [ $# -ge 2 ] || die "--registry takes a directory"; REG="$2"; shift 2 ;;
    --root) [ $# -ge 2 ] || die "--root takes a directory"; ROOT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[ -n "$MODE" ] || { usage >&2; exit 2; }

if [ -z "$ROOT" ]; then ROOT="${HARBOR_SCOPE_ROOT:-$(CDPATH= cd -- "$SRC/.." && pwd -P)}"; fi
if [ -z "$REG" ]; then
  if [ -f "$ROOT/standards/naming/kit.tsv" ]; then REG="$ROOT/standards/naming"
  elif [ -f "$SRC/registry/kit.tsv" ]; then REG="$SRC/registry"   # a vendored copy judges the registry it shipped with
  else REG="$ROOT/standards/naming"; fi
fi
KIT="$REG/kit.tsv"
[ -f "$KIT" ] || { log_note "no kit.tsv at $KIT: nothing was compared (unchecked)"; log_end || :; exit 3; }

# The data rows of a register: the header row is the first non-comment line.
rows() { awk -F'\t' '/^#/ || /^[[:space:]]*$/ { next } !h++ { next } { print }' "$1"; }

kit_rows() { # <kind> -> id TAB item TAB ref TAB share TAB vendored for the rows the kind inherits
  rows "$KIT" | awk -F'\t' -v k="$1" '{ n = split($4, a, " "); hit = 0
    for (i = 1; i <= n; i++) if (a[i] == "*" || a[i] == k) hit = 1
    if (hit) printf "%s\t%s\t%s\t%s\t%s\n", $1, $2, $3, $5, $6 }'
}

case "$MODE" in
--render)
  [ -n "$KIND" ] || die "--render takes a repo kind"
  _known="$(rows "$REG/repo-kinds.tsv" 2>/dev/null | cut -f1)"
  [ -z "$_known" ] || printf '%s\n' "$_known" | grep -qx "$KIND" || { log_fail "'$KIND' is not a repo kind (repo-kinds.tsv: $(printf '%s' "$_known" | tr '\n' ' '))"; log_fix "sh $0 --render <one of the kinds above>"; log_end || :; exit 2; }
  kit_rows "$KIND"; exit 0 ;;

--package)
  # The vendored package, derived from the kit: tools and registers with vendored=yes. The source of a register is its schemas.tsv path.
  rows "$KIT" | awk -F'\t' '$6 == "yes" && $2 == "tool" { n = split($3, p, "/"); printf "%s\t%s\n", p[n], $3 }
    $6 == "yes" && $2 == "register" && $3 ~ /^standards\/naming\/[^*?]+\.tsv$/ { n = split($3, p, "/"); printf "registry/%s\t%s\n", p[n], $3 }'
  exit 0 ;;

--core)
  rows "$KIT" | awk -F'\t' '$2 == "register" && $5 == "core" && $3 ~ /^standards\/naming\/[^*?]+\.tsv$/ { n = split($3, p, "/"); print p[n] }'; exit 0 ;;

--core-tools)
  rows "$KIT" | awk -F'\t' '$2 == "tool" && $5 == "core" { n = split($3, p, "/"); print p[n] }'; exit 0 ;;

--core-file)
  _f="$KIND"; [ -f "$REG/$_f" ] || { log_fail "no register $_f in $REG"; log_fix "sh $0 --core   # lists the core-share registers"; log_end || :; exit 2; }
  sh "$SRC/standard-kit.sh" --registry "$REG" --root "$ROOT" --core 2>/dev/null | grep -qx "$_f" || { log_fail "$_f is not a core-share register (kit.tsv)"; log_fix "sh $0 --core   # lists the core-share registers"; log_end || :; exit 1; }
  # The core tools are the engines a clean box keeps; a control whose engine is anywhere else has no engine there, and no test is shipped.
  _tools="$(rows "$KIT" | awk -F'\t' '$2 == "tool" && $5 == "core" { n = split($3, p, "/"); print p[n] }' | tr '\n' ' ')"
  _fams="$(rows "$KIT" | awk -F'\t' '$2 == "control" && $5 == "core" { printf "%s ", $3 }')"
  _paths="$(rows "$KIT" | awk -F'\t' '$2 == "register" && $5 == "core" && $3 ~ /^standards\/naming\// { printf "%s ", $3 }')"
  case "$_f" in
    schemas.tsv) awk -F'\t' -v keep=" $_paths" '/^#/ || /^[[:space:]]*$/ || $1 == "id" { print; next } index(keep, " " $1 " ") { print }' "$REG/$_f" ;;
    controls.tsv) awk -F'\t' -v OFS='\t' -v fams=" $_fams" -v tools=" $_tools" '/^#/ || /^[[:space:]]*$/ || $1 == "id" { print; next }
        { split($1, a, "."); if (!index(fams, " " a[1] " ")) next
          n = split($4, p, "/"); if ($4 != "-" && !index(tools, " " p[n] " ")) $4 = "-"
          $11 = "-"; if ($8 == "stable") $8 = "experimental"; print }' "$REG/$_f" ;;   # no proof ships, so no row claims it watched one red (harbor#1422)
    kit.tsv) awk -F'\t' '/^#/ || /^[[:space:]]*$/ || $1 == "id" { print; next } $5 == "core" && ($2 != "register" || $3 ~ /^standards\/naming\//) { print }' "$REG/$_f" ;;
    registers.tsv) awk -F'\t' -v OFS='\t' '/^#/ || /^[[:space:]]*$/ || $1 == "id" { print; next } { $4 = "-"; print }' "$REG/$_f" ;;
    exemptions.tsv) awk -F'\t' '/^#/ || /^[[:space:]]*$/ || $1 == "id" { print }' "$REG/$_f" ;;
    *) cat "$REG/$_f" ;;
  esac
  exit 0 ;;

--check)
  F="${TMPDIR:-/tmp}/standard-kit.$$"; : > "$F.f"
  say() { printf 'kit: %s\n' "$1" >> "$F.f"; }
  rows "$REG/schemas.tsv" 2>/dev/null | cut -f1 | sort > "$F.regs"
  rows "$REG/controls.tsv" 2>/dev/null | cut -f1 | sed 's/\..*//' | sort -u > "$F.fams"
  rows "$REG/repo-kinds.tsv" 2>/dev/null | cut -f1 | sort > "$F.kinds"
  rows "$REG/versions.tsv" 2>/dev/null | cut -f2 | sort -u > "$F.axes"
  [ -s "$F.regs" ] || { log_note "no schemas.tsv beside the kit: nothing was compared (unchecked)"; rm -f "$F".*; log_end || :; exit 3; }
  rows "$KIT" | awk -F'\t' '$2 == "register" { print $3 }' | sort > "$F.kregs"
  rows "$KIT" | awk -F'\t' '$2 == "control" { print $3 }' | sort > "$F.kfams"
  comm -23 "$F.regs" "$F.kregs" | while IFS= read -r _x; do say "register $_x has no kit row (add a register row to standards/naming/kit.tsv)"; done
  comm -13 "$F.regs" "$F.kregs" | while IFS= read -r _x; do say "kit names the register $_x and schemas.tsv has no such row"; done
  comm -23 "$F.fams" "$F.kfams" | while IFS= read -r _x; do say "control family $_x has no kit row (add a control row to standards/naming/kit.tsv)"; done
  comm -13 "$F.fams" "$F.kfams" | while IFS= read -r _x; do say "kit names the control family $_x and controls.tsv has none"; done
  rows "$KIT" | awk -F'\t' -v kinds="$F.kinds" -v axes="$F.axes" -v bin="$ROOT/bin" '
    BEGIN { while ((getline l < kinds) > 0) K[l] = 1; while ((getline l < axes) > 0) A[l] = 1
            split("register control tool check template pin", it, " "); for (i in it) IT[it[i]] = 1
            split("core hub workload personal", sh, " "); for (i in sh) SH[sh[i]] = 1 }
    { id = $1
      if (NF != 7) { printf "kit: row %s has %d columns, not 7\n", (id == "" ? "?" : id), NF; next }
      if (seen[id]++) printf "kit: id %s is used twice\n", id
      if (!($2 in IT)) printf "kit: %s names an item %s outside register, control, tool, check, template, pin\n", id, $2
      if (index(id, $2 ".") != 1) printf "kit: %s does not start with its item %s.\n", id, $2
      if (!($5 in SH)) printf "kit: %s names the share %s outside core, hub, workload, personal\n", id, $5
      if ($6 != "yes" && $6 != "no") printf "kit: %s has vendored %s, not yes or no\n", id, $6
      if ($4 == "") printf "kit: %s lists no repo kind (write * for every kind)\n", id
      n = split($4, a, " "); for (i = 1; i <= n; i++) if (a[i] != "*" && !(a[i] in K)) printf "kit: %s names the repo kind %s and repo-kinds.tsv has no such id\n", id, a[i]
      if ($2 == "pin" && !($3 in A)) printf "kit: %s names the axis %s and versions.tsv has no row of it\n", id, $3
      if ($2 == "tool") { if ($3 !~ /^bin\/[^\/]+$/) printf "kit: %s ref %s is not a bin/ file\n", id, $3; else { f = $3; sub(/^bin\//, "", f); if (system("test -d \"" bin "\" && ! test -f \"" bin "/" f "\"") == 0) printf "kit: %s names bin/%s and the tree has no such file\n", id, f } }
      if ($6 == "yes" && $2 == "register" && $3 !~ /^(standards\/naming|vendor\/naming)\//) printf "kit: %s is vendored yes and %s is not a register the package carries\n", id, $3
      if ($6 == "yes" && ($2 == "check" || $2 == "template")) printf "kit: %s is a %s: yard renders it, so vendored is no\n", id, $2 }' >> "$F.f"
  _n="$(grep -c . "$F.f" || :)"
  if [ "$_n" -eq 0 ]; then log_ok "the kit covers every register ($(grep -c . "$F.regs")) and control family ($(grep -c . "$F.fams")) and every row resolves"; rm -f "$F".*; log_end || :; exit 0; fi
  sed 's/^/  /' "$F.f" >&2; rm -f "$F".*
  log_fail "$_n kit finding(s)"; log_fix "edit standards/naming/kit.tsv: one row per register and per control family"; log_end || :; exit 1 ;;

--carries)
  [ -d "$REPO" ] || die "no such directory: $REPO"
  REPO="$(CDPATH= cd -- "$REPO" && pwd -P)"
  if [ -z "$KIND" ]; then KIND="$(sed -n 's/^repo-kind:[[:space:]]*//p' "$REPO/.agents/config.yaml" 2>/dev/null | head -n 1 | tr -d "\"' \r")"; fi
  [ -n "$KIND" ] || { log_fail "$REPO declares no repo-kind (its .agents/config.yaml has no repo-kind: line)"; log_fix "add 'repo-kind: <kind>' to .agents/config.yaml, or pass --kind"; log_end || :; exit 3; }
  V="$REPO/vendor/naming"
  F="${TMPDIR:-/tmp}/standard-kit.$$"; : > "$F.f"; N=0
  kit_rows "$KIND" > "$F.rows"
  [ -s "$F.rows" ] || { log_fail "kit.tsv lists nothing for the kind '$KIND'"; log_fix "sh $0 --render $KIND"; rm -f "$F".*; log_end || :; exit 3; }
  while IFS="$TAB" read -r _id _item _ref _share _vend; do
    N=$((N + 1)); _miss=''
    case "$_item" in
      register) [ "$_vend" = yes ] && { _b="${_ref##*/}"; [ -f "$V/registry/$_b" ] || _miss="vendor/naming/registry/$_b"; } ;;
      tool)     [ "$_vend" = yes ] && { _b="${_ref##*/}"; [ -f "$V/$_b" ] || _miss="vendor/naming/$_b"; } ;;
      control)  [ "$_vend" = yes ] && { awk -F'\t' -v f="$_ref." 'index($1, f) == 1 { found = 1 } END { exit !found }' "$V/registry/controls.tsv" 2>/dev/null || _miss="a $_ref.* row in vendor/naming/registry/controls.tsv"; } ;;
      pin)      awk -F'\t' -v a="$_ref" '$2 == a { found = 1 } END { exit !found }' "$V/registry/versions.tsv" 2>/dev/null || _miss="a $_ref row in vendor/naming/registry/versions.tsv" ;;
      template|check) [ -e "$REPO/$_ref" ] || _miss="$_ref" ;;
    esac
    [ -z "$_miss" ] || printf 'kit: %s lacks %s\n' "$_id" "$_miss" >> "$F.f"
  done < "$F.rows"
  _n="$(grep -c . "$F.f" || :)"
  if [ "$_n" -eq 0 ]; then log_ok "$REPO carries all $N items its kind '$KIND' lists"; rm -f "$F".*; log_end || :; exit 0; fi
  sed 's/^/  /' "$F.f" >&2; rm -f "$F".*
  log_fail "$REPO lacks $_n of $N items its kind '$KIND' lists"; log_fix "yard renders them: harbor yard new --kind $KIND --adopt $REPO"; log_end || :; exit 1 ;;
esac
