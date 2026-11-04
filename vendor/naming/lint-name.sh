#!/usr/bin/env sh
# lint-name.sh (harbor#950) — the naming lint: judges names against standards/naming/*.tsv and generates its views.
#
#   sh bin/lint-name.sh                           judge every tracked name in the tree
#   sh bin/lint-name.sh --changed                 judge only paths added or changed since the base
#   sh bin/lint-name.sh --added                   judge only the paths the branch ADDS since the base (git diff
#                                                 --diff-filter=A, a rename counting as an add): a name the branch
#                                                 brings in and no legacy row covers; the registry is not re-judged
#   sh bin/lint-name.sh --check <surface> <name>  judge one name; the scaffolder calls this at a name's birth.
#                                                 `path.test` is the test surface under its second name and `case` is
#                                                 path judged for name.case alone (surfaces.tsv, view); `repo-kind`
#                                                 takes a directory: its .agents/config.yaml names the kind, and the
#                                                 entries repo-kinds.tsv requires of it must be there; `overlay` takes a
#                                                 directory and judges the repo overlay at its root (below)
#   sh bin/lint-name.sh --self-test               every fail fixture goes red, every pass stays green, every rule
#                                                 goes red on every surface controls.tsv declares for it, and every
#                                                 grammar surface's example survives parse then render unchanged
#     --rule <id>                                 (repeatable) limit it to those rules: their fail fixtures, the pass fixtures
#                                                 of the surfaces they declare (judged for those rules only) and their
#                                                 coverage, not the other rules, the examples or the full census (harbor#667).
#                                                 Still runs the grammar, register and validator checks. Not a gate: the
#                                                 full self-test stays the default and the one a tree is judged by
#   sh bin/lint-name.sh --render <surface> k=v..  a tuple rendered by surfaces.tsv's grammar, e.g.
#                                                 --render bin family=admission object=bash ext=sh
#   sh bin/lint-name.sh --parse <surface> <name>  a name parsed back into its slots, one k=v per line
#   sh bin/lint-name.sh --gen <view>              glossary | naming-guide | cli-tree | labels | vale | exemptions | vendors |
#                                                 issue-taxonomy,
#                                                 written under build/ (untracked)
#   sh bin/lint-name.sh --list                    every lint rule id, its mode and title: controls.tsv's, then
#                                                 bin/lint-conventions.sh's (harbor#957), one list with no id twice
#   sh bin/lint-name.sh --help
#
#   --format findings   also print one `<A|V> TAB <rule> TAB <subject>` record per finding on stdout
#   --root <dir>        the tree to judge (default: this checkout; a vendored copy judges the repo that carries it)
#   --registry <dir>    the registry (default: <root>/standards/naming; a vendored copy's own registry/ when the root has none).
#                       A root with no standards/naming/ is a workload: the rules that judge the registry itself stay silent (harbor#1415)
#   --base <ref>        the base for --changed, --added and name.exemption-grow (default: $HARBOR_CI_BASE or origin/main)
#   --today YYYY-MM-DD  the date alias and exemption expiry is judged against (default: $HARBOR_LINT_TODAY, else today in UTC, D-51)
#   --exemptions-base <file>   the base exemptions.tsv for name.exemption-grow (default: read from git)
#   HARBOR_SCOPE_PRIVATE=<dir>  the private layer whose terms.tsv overlays client and system terms (D-85; default: $PORT_ROOT/harbor-private)
#   <root>/naming-words.tsv, <root>/naming-families.tsv   the repo overlay (harbor#967): a repo's own words and families,
#                               in words.tsv's and terms.tsv's row shape, read from the judged root and never from a root
#                               that holds standards/naming/ (standards/naming.md §14)
#
#   exit (D-49) 0 pass, warn and shadow findings allowed · 1 a block finding, or a failed self-test · 2 usage · 3 unchecked
#   LINT_REDLOG=<file> appends the id of every rule that goes red, as bin/lint-conventions.sh does.
#   mode (D-29, D-64): block fails the run, warn prints, shadow only records (LINT_REDLOG and the count).
#
# DELIBERATELY ONE FILE (docs/explanation/modularity.md): one POSIX sh + awk program so the scaffolder can
# vendor it into any repo with no toolchain (standards/naming.md §10.2). The awk does the judging;
# the sh around it only collects names and formats. Every rule is a controls.tsv row, every term a
# terms.tsv row and every surface's grammar a surfaces.tsv cell: nothing about the vocabulary or the
# grammar is written here, so changing a term or a renderer changes the result with no edit to this file.
#
# WHY NO INLINE IGNORES. A name the standard does not judge is a row in exemptions.tsv, with a
# kind and a reason. Legacy rows were generated from each rule's own first scan and only shrink;
# rows naming the registry, this file or its fixtures are refused (name.exemption-self) and never
# applied, so the lint cannot be talked out of judging itself.
set -eu

# ${0%/*}, not dirname: a hook runs this (context-rules/user/name-note.sh), and a failed fork would make HERE the cwd (harbor#810).
case "$0" in */*) HERE="${0%/*}" ;; *) HERE=. ;; esac
HERE="$(CDPATH='' cd -- "$HERE" && pwd)"
SELF="$HERE/${0##*/}"
# shellcheck disable=SC2034 # LOG_FD: read by library-log.sh
LOG_FD=2   # stdout carries --check, --format findings and --gen output; the log goes to stderr
. "$HERE/library-log.sh"
log_init lint-name

usage() { awk 'NR < 2 { next } /^# DELIBERATELY/ || !/^#/ { exit } { sub(/^# ?/, ""); print }' "$SELF"; }

ACTION=tree; FORMAT=""; ROOT=""; REG=""; BASE="${HARBOR_CI_BASE:-origin/main}"; TODAY=""; EXBASE=""
RULES=""      # --rule ids, space-terminated; empty = the full self-test
UNCHECKED=3   # D-49: pass 0, fail 1, usage 2, unchecked 3
SURFACE=""; NAME=""; VIEW=""; SLOTS=""
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --changed) ACTION=changed ;;
    --added) ACTION=added ;;
    --self-test) ACTION=self-test ;;
    --rule) [ $# -ge 2 ] || { log_fail "--rule needs a rule id"; log_fix "sh $SELF --self-test --rule name.case"; log_end || :; exit 2; }
            RULES="$RULES$2 "; shift ;;
    --list) ACTION=list ;;
    --check) ACTION=check
             [ $# -ge 3 ] || { log_fail "--check needs <surface> <name>"; log_fix "sh $SELF --check path docs/reports/2026-10-03-review.md"; log_end || :; exit 2; }
             SURFACE="$2"; NAME="$3"; shift 2 ;;
    --parse) ACTION=parse
             [ $# -ge 3 ] || { log_fail "--parse needs <surface> <name>"; log_fix "sh $SELF --parse bin admission-bash.sh"; log_end || :; exit 2; }
             SURFACE="$2"; NAME="$3"; shift 2 ;;
    --render) ACTION=render
              [ $# -ge 2 ] || { log_fail "--render needs <surface> and k=v slots"; log_fix "sh $SELF --render bin family=admission object=bash ext=sh"; log_end || :; exit 2; }
              SURFACE="$2"; shift 2
              while [ $# -gt 0 ]; do
                case "$1" in -*) break ;; esac
                SLOTS="$SLOTS$1 "; shift
              done
              continue ;;
    --gen) ACTION=gen
           [ $# -ge 2 ] || { log_fail "--gen needs a view"; log_fix "sh $SELF --gen glossary"; log_end || :; exit 2; }
           VIEW="$2"; shift ;;
    --format) [ "${2:-}" = findings ] || { log_fail "--format takes only: findings"; log_fix "sh $SELF --format findings"; log_end || :; exit 2; }
              FORMAT=findings; shift ;;
    --root|--registry|--base|--today|--exemptions-base)
      [ $# -ge 2 ] || { log_fail "$1 needs a value"; log_fix "sh $SELF --help"; log_end || :; exit 2; }
      case "$1" in
        --root) ROOT="$2" ;; --registry) REG="$2" ;; --base) BASE="$2" ;;
        --today) TODAY="$2" ;; --exemptions-base) EXBASE="$2" ;;
      esac; shift ;;
    *) log_fail "unknown argument: $1"; log_fix "sh $SELF --help"; log_end || :; exit 2 ;;
  esac
  shift
done

[ -z "$RULES" ] || [ "$ACTION" = self-test ] || { log_fail "--rule only limits --self-test"; log_fix "sh $SELF --self-test --rule name.case"; log_end || :; exit 2; }

# A vendored copy (vendor/naming/, MANIFEST.tsv beside it) judges the repo that carries it, with the registry beside it
# (harbor#967, #1415): bare, it finds both; harbor's own bin/ has neither file, so its defaults are unchanged.
VENDORED=0; [ ! -f "$HERE/MANIFEST.tsv" ] || [ ! -f "$HERE/registry/registers.tsv" ] || VENDORED=1
if [ "$VENDORED" = 1 ] && [ -z "$ROOT" ]; then ROOT="$HERE/../.."; fi
ROOT="${ROOT:-$(CDPATH='' cd -- "$HERE/.." && pwd)}"
ROOT="$(CDPATH='' cd -- "$ROOT" 2>/dev/null && pwd)" || { log_fail "no such root: $ROOT"; log_fix "sh $SELF --root <dir>"; log_end || :; exit 2; }
if [ -z "$REG" ] && [ "$VENDORED" = 1 ] && [ ! -f "$ROOT/standards/naming/registers.tsv" ]; then REG="$HERE/registry"; fi
REG="${REG:-$ROOT/standards/naming}"
TODAY="${TODAY:-${HARBOR_LINT_TODAY:-$(date -u +%Y-%m-%d)}}"   # D-51: UTC for every date; the env pins it for a caller that cannot pass --today
case "$TODAY" in [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;; *)
  log_fail "--today is not YYYY-MM-DD: $TODAY"; log_fix "sh $SELF --today 2026-10-04"; log_end || :; exit 2 ;;
esac
T="$(mktemp -d "${TMPDIR:-/tmp}/lint-name.XXXXXX")"
trap 'rm -rf "$T"' EXIT INT TERM HUP

# The registry this run reads: --check registry <dir> overlays <dir>/<file> on the registry, so a
# fixture holds only the one file it breaks. A registry that cannot be read is UNCHECKED, never clean.
OVERLAY=""
[ "$ACTION" != check ] || [ "$SURFACE" != registry ] || OVERLAY="$NAME"
# A fixture file that opens with `#` replaces the registry's; one that holds rows only is appended
# below a `# fixture rows` line, so most fixtures are one row and never drift from the registry.
reg_file() { # <file> -> the path this run reads it from
  case "$OVERLAY" in /*) _o="$OVERLAY" ;; '') _o="" ;; *) _o="$ROOT/$OVERLAY" ;; esac
  if [ -z "$_o" ] || [ ! -f "$_o/$1" ]; then printf '%s' "$REG/$1"; return 0; fi
  case "$(head -c 1 "$_o/$1")" in '#') printf '%s' "$_o/$1"; return 0 ;; esac
  mkdir -p "$T/reg"
  [ -f "$T/reg/$1" ] || { cat "$REG/$1"; printf '%s\n' '# fixture rows'; cat "$_o/$1"; } > "$T/reg/$1"
  printf '%s' "$T/reg/$1"
}
_od=""
if [ -n "$OVERLAY" ]; then
  case "$OVERLAY" in /*) _od="$OVERLAY" ;; *) _od="$ROOT/$OVERLAY" ;; esac
  [ -d "$_od" ] || { log_fail "no registry fixture at $OVERLAY"; log_fix "ls $ROOT/fixtures/naming/registry"; log_end || :; exit 2; }
  # A fixture for name.exemption-grow carries the base it grew from, so the result never depends on git.
  # Its base is the registry's own exemptions plus the rows in base/exemptions.tsv.
  if [ -z "$EXBASE" ] && [ -f "$_od/base/exemptions.tsv" ]; then
    cat "$REG/exemptions.tsv" "$_od/base/exemptions.tsv" > "$T/exbase-fixture.tsv"; EXBASE="$T/exbase-fixture.tsv"
  fi
fi
# The private overlay (D-85): client and system terms that live in the operator's harbor-private, never in core.
# Absent is fine; present but unreadable is UNCHECKED. --self-test is hermetic: it never reads the box's overlay.
PRIV="${HARBOR_SCOPE_PRIVATE:-${PORT_ROOT:-$HOME/port}/harbor-private}/terms.tsv"
{ [ "$ACTION" != self-test ] && [ -f "$PRIV" ]; } || PRIV=""
[ "$ACTION" != self-test ] || { HARBOR_SCOPE_PRIVATE="$T/no-private"; export HARBOR_SCOPE_PRIVATE; }
[ -z "$PRIV" ] || [ -r "$PRIV" ] || { log_fail "the private overlay $PRIV is not readable — nothing was judged"; log_fix "ls -l $PRIV"; log_end || :; exit "$UNCHECKED"; }
# A pack's system terms (harbor#950): <config dir>/terms.d/<repo-id>.tsv, written by bin/pack.sh install, read with the
# private overlay's rules and class `system` only, so a workload registers its own systems and core names none.
PACKT=""
# --self-test's fixture runs are children of this one: they inherit an empty config dir, as they inherit no private root.
[ "$ACTION" != self-test ] || { CLAUDE_CONFIG_DIR="$T/no-config"; export CLAUDE_CONFIG_DIR; }
if [ "$ACTION" != self-test ]; then
  for _pt in "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/terms.d/*.tsv; do
    [ -f "$_pt" ] || continue
    case "$_pt" in *[[:space:]]*) log_fail "a pack overlay path holds whitespace: $_pt — nothing was judged"; log_nofix "bin/lint-name.sh"; log_end || :; exit "$UNCHECKED" ;; esac
    [ -r "$_pt" ] || { log_fail "the pack overlay $_pt is not readable — nothing was judged"; log_fix "ls -l $_pt"; log_end || :; exit "$UNCHECKED"; }
    PACKT="$PACKT${PACKT:+ }$_pt"
  done
fi
# The repo overlay (harbor#967): a repo outside harbor registers its own words and families at the root it is judged
# from, so it stops exempting its domain names and core stays free of them. A root holding standards/naming/ registers
# there instead: its overlay is refused, never read. Absent is fine; present but unreadable is UNCHECKED.
OVDIR="$ROOT"
if [ "$ACTION" = check ] && [ "$SURFACE" = overlay ]; then
  case "$NAME" in /*) OVDIR="$NAME" ;; *) OVDIR="$ROOT/$NAME" ;; esac
  [ -d "$OVDIR" ] || { log_fail "no such directory: $NAME"; log_fix "sh $SELF --check overlay <dir>, the root whose naming-words.tsv and naming-families.tsv are judged"; log_end || :; exit 2; }
fi
OVHOME=0; [ ! -f "$OVDIR/standards/naming/registers.tsv" ] || OVHOME=1
OVW=""; OVF=""
for _ov in naming-words.tsv naming-families.tsv; do
  [ -e "$OVDIR/$_ov" ] || continue
  { [ -f "$OVDIR/$_ov" ] && [ -r "$OVDIR/$_ov" ]; } || { log_fail "the repo overlay $OVDIR/$_ov is not a readable file — nothing was judged"; log_fix "ls -l $OVDIR/$_ov"; log_end || :; exit "$UNCHECKED"; }
  if [ "$_ov" = naming-words.tsv ]; then OVW="$OVDIR/$_ov"; else OVF="$OVDIR/$_ov"; fi
done
REL_REG="${REG#"$ROOT"/}"
# A root with no standards/naming/ of its own is judged by harbor's registry, not the owner of it: the registry-owner rules
# (regrule) judge harbor's controls and exemptions, so a workload run leaves them to harbor (harbor#967, #1415).
FOREIGN=0
case "$ACTION" in tree|changed|added) [ -f "$ROOT/standards/naming/registers.tsv" ] || FOREIGN=1 ;; esac
# THE FILES THIS RUN READS ARE registers.tsv's `file` column (harbor#950), not a list written here: terms.tsv
# first, because every other file's classes are checked against it, then each named file once in row order.
# A register is its file plus its row, so a new one needs no edit to this file. Two guards keep that honest:
# the judge below has a handler for exactly the files named in the loop (one `sh == "<file>"` branch each, so a
# new handler is added to both), so a registers.tsv that stopped naming one would silently stop judging it, and
# that is UNCHECKED; and a standards/naming/*.tsv that no row names is a file never read, which the judge
# reports (name.registry-shape, UNLISTED).
_rg="$(reg_file registers.tsv)"
[ -r "$_rg" ] || { log_fail "the registry has no readable registers.tsv — nothing was judged"; log_fix "ls $REG"; log_end || :; exit "$UNCHECKED"; }
FILES="$(awk -F'\t' '/^#/ || /^[ \t]*$/ { next } !h++ { next } $2 ~ /^[a-z][a-z0-9-]*\.tsv$/ && !s[$2]++ { printf "%s ", $2 }' "$_rg")"
for _f in terms.tsv aliases.tsv exemptions.tsv controls.tsv surfaces.tsv kinds.tsv repo-kinds.tsv registers.tsv renames.tsv words.tsv; do
  case " $FILES " in *" $_f "*) ;; *)
    log_fail "registers.tsv has no row naming $_f, which the judge reads — nothing was judged"
    log_fix "add its row to $REL_REG/registers.tsv (id, file, select, census, note)"; log_end || :; exit "$UNCHECKED" ;;
  esac
done
FILES="terms.tsv $(printf '%s\n' $FILES | awk '$0 != "terms.tsv"' | tr '\n' ' ')"; FILES="${FILES% }"
for _f in $FILES; do
  [ -r "$(reg_file "$_f")" ] || { log_fail "the registry has no readable $_f — nothing was judged"; log_fix "ls $REG"; log_end || :; exit "$UNCHECKED"; }
done
UNLISTED="$( { find "$REG" -maxdepth 1 -type f -name '*.tsv'; [ -z "$_od" ] || find "$_od" -maxdepth 1 -type f -name '*.tsv'; } 2>/dev/null \
  | sed 's#.*/##' | LC_ALL=C sort -u | awk -v f=" $FILES " 'index(f, " " $0 " ") == 0 { printf "%s ", $0 }')"

# ── the judge ────────────────────────────────────────────────────────────────
# Reads the registry files registers.tsv names, then one record per name: F <path> <mode> (a tracked file),
# X <path> (exists only, for registry checks), E <NAME> (an env token), C <surface> <name>.
# Prints F <rule> <level> <subject> <message> per finding, then S <suppressed> <legacy rows>.
JUDGE='
function q(s) { return "`" s "`" }
function jdn(s,   y, m, d, a) {
  y = substr(s, 1, 4) + 0; m = substr(s, 6, 2) + 0; d = substr(s, 9, 2) + 0
  a = int((14 - m) / 12); y = y + 4800 - a; m = m + 12 * a - 3
  return d + int((153 * m + 2) / 5) + 365 * y + int(y / 4) - int(y / 100) + int(y / 400) - 32045
}
function isdate(s) { return s ~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]$/ }
function kebab(s) { return s ~ /^[a-z][a-z0-9]*(-[a-z][a-z0-9]*)*$/ }
function col(f, name) { return ((f SUBSEP name) in COL) ? $(COL[f, name]) : "" }
function base(p) { sub(/.*\//, "", p); return p }
function selfpath(s) {
  return s ~ /^standards\/naming(\/|\.md$|$)/ || s == "bin/lint-name.sh" || s ~ /^fixtures\/naming(\/|$)/
}
# An overlay finding is always block: a malformed private layer must not read as clean, even under --check (D-85).
function emitb(rule, subject, msg) { if ((rule SUBSEP subject) in DONE) return; DONE[rule SUBSEP subject] = 1; print "F", rule, "block", subject, msg }
function emitw(rule, subject, msg) { if ((rule SUBSEP subject) in DONE) return; DONE[rule SUBSEP subject] = 1; print "F", rule, "warn", subject, msg }
function regrule(r) { return r ~ /^name\.(registry-|exemption-|alias-expired|control-|surface-)/ || r ~ /^standard\.(register-declared|reference-resolves|seam-registered|version-registered|env-registered|script-registered)$/ }
function emit(rule, subject, msg,   k, e) {
  if (mode == "check" && regrule(rule) && surface != "registry") return
  if (mode == "registry" && !regrule(rule)) return
  if ((mode == "added" || (foreign == 1 && rule !~ /^standard\./)) && regrule(rule)) return   # --added judges names a branch brings in, a foreign root leaves the registry to harbor, but its own registers are judged (harbor#1424)
  if (mode == "check" && (surface in VIEWR) && VIEWR[surface] != "" && index("," VIEWR[surface] ",", "," rule ",") == 0) return   # a view of a surface for some rules only
  k = rule SUBSEP subject
  if (k in DONE) return
  DONE[k] = 1
  if (!regrule(rule)) { e = exempt(rule, subject); if (e != "") { USED[e] = 1; NSUP++; return } }
  print "F", rule, ((FORCEWARN == 1) ? "warn" : (rule in MODE) ? MODE[rule] : "warn"), subject, msg
}
function exempt(rule, subject,   k, i, b, b2) {
  if (selfpath(subject)) return ""
  k = rule SUBSEP subject
  if (k in EXACT) return EXACT[k]
  if (subject in RENWIN) return RENWIN[subject]   # an env name in its dual-read window (renames.tsv, harbor#961 W5)
  for (i = 1; i <= NPRE; i++) if ((PRER[i] == rule || PRER[i] == "*") && index(subject, PRES[i]) == 1) return PREI[i]
  b = base(subject); b2 = b; sub(/\.tmpl$/, "", b2)
  for (i = 1; i <= NGLOB; i++) if ((GLR[i] == rule || GLR[i] == "*") && (b ~ GLB[i] || b2 ~ GLB[i])) return GLI[i]
  return ""
}
function allowed(a, ctx,   n, i, S) {
  n = split(ASURF[a], S, ",")
  for (i = 1; i <= n; i++) if (S[i] == ctx) return 1
  return 0
}
# Every alias, wherever its words fall in the name (D-38). An abbreviation alias is never allowed.
function alias_check(subj, W, from, n, ctx,   a, k, j, m, ok) {
  for (a in AL) {
    m = AWN[a]
    for (k = from; k + m - 1 <= n; k++) {
      ok = 1
      for (j = 1; j <= m; j++) if (W[k + j - 1] != AWORD[a, j]) { ok = 0; break }
      if (!ok) continue
      if (AKD[a] == "abbreviation")
        emit("name.abbreviation", subj, q(a) " is an unregistered abbreviation: spell out " q(ATERM[a]) " (D-18)")
      else if (AKD[a] == "spelling")
        emit("name.alias", subj, q(a) " is a UK spelling: write " q(ATERM[a]) ", the standard is US English (D-16)")
      else if (!allowed(a, ctx))
        emit("name.alias", subj, q(a) " is a " AKD[a] " alias" (ATERM[a] == "-" ? "" : " of " q(ATERM[a])) ", not allowed on " ctx " (D-38, standards/naming/aliases.tsv)")
      break
    }
  }
}
# A word of a name is known when it is a term, a word of a multi-word term, an alias (judged by its own rule),
# a registered word, a private-overlay term or a repo-overlay word or family. KW is empty only if words.tsv is absent,
# and then nothing is flagged.
function twload(   k, j, nwk, WK) {   # TW: the words of every term; TWC: of the core terms only
  if (!NKW_LOADED) { for (k in T) { nwk = split(k, WK, "-"); for (j = 1; j <= nwk; j++) { TW[WK[j]] = 1; if (!(k in OVT)) TWC[WK[j]] = 1 } }; NKW_LOADED = 1 }
}
function wordknown(w) {
  twload()
  return (length(w) < 2) || (w in KW) || (w in T) || (w in TW) || (w in AL) || (w in OVT) || (w in RW) || (!HAVEWORDS)
}
# A core term: one terms.tsv names, not the private overlay, so a repo overlay is judged the same on every box.
function coreterm(w) { return (w in T) && !(w in OVT) }
# One row of the repo overlay (harbor#967), at f line FNR: registers the row, or emits why it does not.
function overlay_row(f, ofn, ofam,   id, sj, i, cl, st, df) {
  id = $1; sj = "overlay:" ofn ":" id
  if (NF != HDR[f]) { emitb("name.overlay-shape", "overlay:" ofn ":line" FNR, ofn " line " FNR " has " NF " columns, the header has " HDR[f]); return }
  if (id !~ /^[a-z][a-z0-9]*$/) { emitb("name.overlay-shape", sj, ofn " line " FNR ": " q(id) " is not one lowercase word, so no name could carry it (§6.3)"); return }
  if (id in RSEEN) { emitb("name.overlay-shape", sj, ofn " line " FNR " repeats " q(id) " of " RSEEN[id] ": one row per word"); return }
  RSEEN[id] = ofn " line " FNR
  if (id in AL) { emitb("name.overlay-shadow", sj, q(id) " is a harbor " AKD[id] " alias" (ATERM[id] == "-" ? "" : " of " q(ATERM[id])) ": an overlay cannot register it (aliases.tsv)"); return }
  twload()
  if (ofam) {
    cl = col(f, "class"); st = col(f, "stability"); df = col(f, "definition")
    if (cl != "family") { emitb("name.overlay-shape", sj, ofn " line " FNR ": class " q(cl) " is not family: the overlay adds families only"); return }
    if (st != "experimental" && st != "stable" && st != "deprecated") { emitb("name.overlay-shape", sj, ofn " line " FNR ": stability " q(st) " is not experimental, stable or deprecated (D-19)"); return }
    if (df == "" || df == "-") { emitb("name.overlay-shape", sj, ofn " line " FNR ": a family says what it holds: the definition is empty"); return }
    # a word harbor parses into a slot keeps that one meaning: a family that is also a verb, a domain or a system is two parses (§3)
    for (i = 1; i <= 4; i++) if (coreterm(id) && ((OGRAM[i] SUBSEP id) in IS)) {
      emitb("name.overlay-shadow", sj, q(id) " is a harbor " OGRAM[i] (OGRAM[i] == "family" ? ": the overlay cannot redefine it, use it as it is" : ": a family of the same word would parse two ways (§3)")); return }
    IS["family", id] = 1; RW[id] = 1; return
  }
  if (coreterm(id)) { emitb("name.overlay-shadow", sj, q(id) " is already a harbor term (terms.tsv): delete the row"); return }
  if (id in KW) { emitb("name.overlay-shadow", sj, q(id) " is already in the harbor words.tsv: delete the row"); return }
  if (id in TWC) { emitb("name.overlay-shadow", sj, q(id) " is already a word of a harbor term (terms.tsv): delete the row"); return }
  RW[id] = 1
}
# The words of one name: the date prefix and a decision-record number are not words (§6.3).
function word_check(subj, W, n, path,   i, start) {
  start = 1
  if (n >= 4 && W[1] ~ /^[0-9][0-9][0-9][0-9]$/ && W[2] ~ /^[0-9][0-9]$/ && W[3] ~ /^[0-9][0-9]$/) start = 4
  else if (path ~ /^docs\/decisions\// && W[1] ~ /^[0-9]+$/) start = 2
  for (i = start; i + 2 <= n; i++)
    if (W[i] ~ /^[0-9][0-9][0-9][0-9]$/ && W[i + 1] ~ /^[0-9][0-9]$/ && W[i + 2] ~ /^[0-9][0-9]$/) {
      emit("name.date", subj, "a date sits inside the name; a date is a YYYY-MM-DD- prefix (§6.3)"); return start
    }
  for (i = start; i <= n; i++) {
    if (W[i] == "") { emit("name.word", subj, "an empty word: a leading, trailing or doubled separator (§6.3)"); continue }
    if (W[i] ~ /^[0-9]/) emit("name.word", subj, "the word " q(W[i]) " starts with a digit (§6.3)")
    else if (W[i] != "" && W[i] !~ /[0-9]/ && !wordknown(W[i]))
      emit("name.word-unregistered", subj, q(W[i]) " is in no registry: a name is built of terms and registered English words, so spell it out or add a row to standards/naming/words.tsv (D-18)")
  }
  return start
}
# One path segment: case, separator, extension chain, words, aliases.
function seg_check(subj, seg, isleaf, ctx, path, fmode,   s, n, P, i, stem, py, W, nw, start) {
  if (seg ~ /[A-Z]/) emit("name.case", subj, "uppercase in " q(seg) " (D-1)")
  s = tolower(seg)
  if (substr(s, 1, 1) == ".") s = substr(s, 2)
  n = split(s, P, ".")
  stem = P[1]
  py = (isleaf && n == 2 && P[2] == "py" && fmode != "100755" && path !~ /^bin\//)
  if (py) { if (stem ~ /[^a-z0-9_]/) emit("name.separator", subj, "an importable Python module is snake case: " q(seg) " (D-41)") }
  else if (stem ~ /[^a-z0-9-]/) emit("name.separator", subj, "a character other than a-z, 0-9 and - in " q(seg) " (§6.2)")
  for (i = 2; i <= n; i++)
    if (!(("extension" SUBSEP P[i]) in IS)) { emit("name.extension", subj, q(P[i]) " in " q(seg) " is not a registered extension (§6.3)"); break }
  gsub(/^_+|_+$/, "", stem); gsub(/_+/, "_", stem)
  nw = split(stem, W, /[-_]/)
  start = word_check(subj, W, nw, path)
  alias_check(subj, W, start, nw, ctx)
}
# ── the grammar: a surfaces.tsv renderer cell, parsed back from a name and rendered from a tuple ──
# A cell with a `rule` is a template of literals, <slots> and [optional groups]; the same tokens
# serve tmatch (a name to its slots) and trender (slots to a name), so the two cannot drift.
function slotclass(nm,   n) {
  n = tolower(nm)
  if (n == "family") return "family"
  if (n == "family-or-domain") return "family|domain"
  if (n == "verb") return "verb"
  if (n == "system" || n == "repo") return "system"
  if (n == "object" || n == "subject" || n == "aspect" || n == "name" || n == "id" || n == "ext") return "free"
  return "?"
}
function tcompile(sid, t,   i, c, lit, nm, j, n, opt, k, grp) {
  n = 0; lit = ""; opt = 0; grp = 0; TJ[sid] = (t ~ /_/) ? "_" : "-"; TERR[sid] = ""
  for (i = 1; i <= length(t); i++) {
    c = substr(t, i, 1)
    if (c == "<") {
      j = index(substr(t, i), ">")
      if (j == 0) { TERR[sid] = "an unclosed <"; return }
      if (lit != "") { n++; TK[sid, n] = "L"; TV[sid, n] = lit; TO[sid, n] = opt; lit = "" }
      nm = substr(t, i + 1, j - 2)
      n++; TK[sid, n] = "S"; TV[sid, n] = nm; TO[sid, n] = opt
      i += j - 1
    } else if (c == "[" || c == "]") {
      if (lit != "") { n++; TK[sid, n] = "L"; TV[sid, n] = lit; TO[sid, n] = opt; lit = "" }
      if (c == "[") opt = ++grp; else opt = 0
    } else lit = lit c
  }
  if (lit != "") { n++; TK[sid, n] = "L"; TV[sid, n] = lit; TO[sid, n] = opt }
  NTK[sid] = n; TEXT[sid] = (n > 0 && TK[sid, n] == "S" && tolower(TV[sid, n]) == "ext")
  for (k = 1; k <= n; k++) {
    if (TK[sid, k] == "S" && slotclass(TV[sid, k]) == "?") { TERR[sid] = "the slot <" TV[sid, k] "> is not a slot (family verb system family-or-domain object subject aspect name id ext)"; return }
    if (TK[sid, k] == "S" && k < n && TK[sid, k + 1] == "S" && !TO[sid, k + 1]) { TERR[sid] = "two slots with no literal between them: <" TV[sid, k] "><" TV[sid, k + 1] ">"; return }
  }
}
function slotok(cls, cand, sid,   ctx) {
  if (cls == "family") return (("family" SUBSEP cand) in IS)
  if (cls == "family|domain") return ((("family" SUBSEP cand) in IS) || (("domain" SUBSEP cand) in IS))
  if (cls == "verb") return (("verb" SUBSEP cand) in IS)
  ctx = (sid == "env") ? "env-prefix" : sid
  return ((("system" SUBSEP cand) in IS) || ((cand in AL) && allowed(cand, ctx)))
}
function clsname(cls) { return (cls == "family|domain") ? "family or domain" : cls }
# "" when s (lowercase) parses, else why not. VAL[<slot>] holds each slot. upto = a slot name: stop after it.
function tmatch(sid, s, upto,   n, i, j, k, p, e, nm, cls, nx, optend, lit, seg, W, nw, kmax, cand, found, J, a, bb, R, c, tail, vn, len, gend, take) {
  delete VAL
  if (!(sid in TJ)) return "surfaces.tsv has no grammar for " q(sid)
  if (TERR[sid] != "") return TERR[sid]
  n = NTK[sid]; p = 1; J = TJ[sid]
  gend = 0; take = 0
  for (i = 1; i <= n; i++) {
    if (TO[sid, i]) {
      if (i > gend) {   # a group no free slot has consumed: taken when the name carries its first literal
        gend = i; while (gend + 1 <= n && TO[sid, gend + 1] == TO[sid, i]) gend++
        take = (p <= length(s) && TK[sid, i] == "L" && substr(s, p, length(TV[sid, i])) == TV[sid, i])
      }
      if (!take) continue
    }
    if (TK[sid, i] == "L") {
      lit = TV[sid, i]
      if (substr(s, p, length(lit)) != lit) return "expected " q(lit) " at " q(substr(s, p) == "" ? "the end" : substr(s, p))
      p += length(lit); continue
    }
    nm = tolower(TV[sid, i]); cls = slotclass(nm)
    optend = i; j = i + 1
    while (j <= n && TO[sid, j]) { optend = j; j++ }
    nx = (j <= n) ? j : 0
    if (cls != "free") {
      R = length(s) + 1
      a = index(substr(s, p), "/"); if (a) R = p + a - 1
      bb = index(substr(s, p), "."); if (bb && p + bb - 1 < R) R = p + bb - 1
      seg = substr(s, p, R - p)
      nw = split(seg, W, J)
      if (nw == 0) return "no " nm " in " q(s)
      kmax = (cls == "system") ? nw : 1
      found = 0
      for (k = kmax; k >= 1 && !found; k--) {
        cand = W[1]; for (a = 2; a <= k; a++) cand = cand "-" W[a]
        if (slotok(cls, cand, sid)) { found = 1; VAL[nm] = cand; len = k - 1; for (a = 1; a <= k; a++) len += length(W[a]) }
      }
      if (!found) return q(W[1]) " is not a registered " clsname(cls)
      p += len
    } else {
      if (nx == 0) e = length(s) + 1
      else {
        lit = TV[sid, nx]
        if (nx == n) { e = length(s) - length(lit) + 1; if (e < p || substr(s, e) != lit) return "expected " q(lit) " at the end" }
        else { a = index(substr(s, p), lit); if (a == 0) return "expected " q(lit); e = p + a - 1 }
      }
      seg = substr(s, p, e - p)
      if (optend > i && TK[sid, i + 1] == "L" && TK[sid, i + 2] == "S") {
        c = TV[sid, i + 1]; vn = tolower(TV[sid, i + 2]); a = 0
        for (bb = length(seg) - length(c) + 1; bb >= 1; bb--) if (substr(seg, bb, length(c)) == c) { a = bb; break }
        if (a > 0) { tail = substr(seg, a + length(c)); if (slotok(slotclass(vn), tail, sid)) { VAL[vn] = tail; seg = substr(seg, 1, a - 1) } }
      }
      if (seg == "") return "no " nm " in " q(s)
      if (index(seg, "/")) return "the " nm " " q(seg) " has an extra path segment"
      if (nm == "ext" && (index(seg, ".") || seg !~ /^[a-z0-9]+$/)) return "the extension " q(seg) " is not one lowercase element"
      if ((nm == "subject" || nm == "aspect") && !kebab(seg)) return "the " nm " " q(seg) " is not lowercase kebab"
      VAL[nm] = seg; if (J == "_") gsub(/_/, "-", VAL[nm])
      p = e; i = optend
    }
    if (upto != "" && nm == upto) {
      # the slot is only the family when the literal after it follows (tests/admission-extra/x is not tests/admission/)
      if (i < n && TK[sid, i + 1] == "L" && !TO[sid, i + 1] && substr(s, p, length(TV[sid, i + 1])) != TV[sid, i + 1])
        return "expected " q(TV[sid, i + 1]) " after " q(VAL[nm]) " at " q(substr(s, p) == "" ? "the end" : substr(s, p))
      return ""
    }
  }
  if (p <= length(s)) return "unexpected " q(substr(s, p))
  return ""
}
# the slots in template order, "k=v k=v"
function tslots(sid,   i, nm, out) {
  out = ""
  for (i = 1; i <= NTK[sid]; i++) if (TK[sid, i] == "S") { nm = tolower(TV[sid, i]); if (nm in VAL) out = out (out == "" ? "" : " ") nm "=" VAL[nm] }
  return out
}
# V holds slot -> value; returns the name, or "" with RERR set when a required slot has no value
function trender(sid, V,   n, i, g, out, nm, v, ok, k, J, part) {
  n = NTK[sid]; J = TJ[sid]; out = ""; RERR = ""
  for (i = 1; i <= n; i++) {
    if (TO[sid, i]) {
      g = i; while (g + 1 <= n && TO[sid, g + 1] == TO[sid, i]) g++
      ok = 1; part = ""
      for (k = i; k <= g; k++) if (TK[sid, k] == "S" && !(tolower(TV[sid, k]) in V)) ok = 0
      if (ok) for (k = i; k <= g; k++) {
        if (TK[sid, k] == "L") part = part TV[sid, k]
        else { nm = tolower(TV[sid, k]); v = V[nm]; gsub(/-/, J, v); part = part ((TV[sid, k] == toupper(TV[sid, k])) ? toupper(v) : v) }
      }
      out = out part; i = g; continue
    }
    if (TK[sid, i] == "L") { out = out TV[sid, i]; continue }
    nm = tolower(TV[sid, i])
    if (!(nm in V)) { RERR = "the slot <" nm "> has no value"; return "" }
    v = V[nm]; gsub(/-/, J, v)
    out = out ((TV[sid, i] == toupper(TV[sid, i])) ? toupper(v) : v)
  }
  return out
}
# ── harbor#1259: the validators, rendered from the same compiled grammar (tcompile) and term sets (slotok) ──
# A validator is an allowlist of shape and family, never a second parser: a registered slot is the words slotok
# accepts (aliases allowed on the surface included), a free slot is lowercase words joined by the grammar joiner.
# Vocabulary (the object slot, D-38) is not in it; the lint denylists and alias rules still judge that.
function vlit(s,   o, i, c) {   # a literal, escaped for an ERE outside a bracket expression
  o = ""
  for (i = 1; i <= length(s); i++) { c = substr(s, i, 1); o = o (index("\\^$.[]|()*+?{}", c) ? "\\" c : c) }
  return o
}
function valts(sid, cls, up,   k, KP, n, m, i, j, w, t, ctx, J, out, seen) {   # the words of a slot class, as the surface spells them
  J = TJ[sid]; ctx = (sid == "env") ? "env-prefix" : sid; n = 0; m = 0
  delete WL; delete VW; delete seen
  for (k in IS) {
    split(k, KP, SUBSEP)
    if ((KP[1] == "family" && (cls == "family" || cls == "family|domain")) || (KP[1] == "domain" && cls == "family|domain") \
        || (KP[1] == "verb" && cls == "verb") || (KP[1] == "system" && cls == "system")) WL[++n] = KP[2]
  }
  if (cls == "system") for (k in AL) if (allowed(k, ctx)) WL[++n] = k
  for (i = 1; i <= n; i++) {
    w = WL[i]; gsub(/-/, J, w); if (up) w = toupper(w)
    if (!(w in seen)) { seen[w] = 1; VW[++m] = w }
  }
  if (m == 0) { VERR = "no term of class " cls " (the grammar of " sid " needs one)"; return "" }
  for (i = 2; i <= m; i++) for (j = i; j > 1 && VW[j - 1] > VW[j]; j--) { t = VW[j]; VW[j] = VW[j - 1]; VW[j - 1] = t }
  out = ""; for (i = 1; i <= m; i++) out = out (i > 1 ? "|" : "") vlit(VW[i])
  return "(" out ")"
}
function vslot(sid, nm, up,   J, w) {   # one slot: its words, or a free word-run joined by the surface joiner
  J = TJ[sid]
  if (slotclass(nm) != "free") return valts(sid, slotclass(nm), up)
  w = up ? "[A-Z][A-Z0-9]*" : "[a-z][a-z0-9]*"
  return (nm == "ext") ? w : w "(" J w ")*"
}
function vtok(sid, i) {
  if (TK[sid, i] == "L") return vlit(TV[sid, i])
  return vslot(sid, tolower(TV[sid, i]), TV[sid, i] == toupper(TV[sid, i]))
}
function vre(sid, from, to,   i, g, k, out, part) {   # tokens from..to; an optional group is (..)? as the template spells it
  out = ""
  for (i = from; i <= to; i++) {
    if (TO[sid, i]) {
      g = i; while (g + 1 <= to && TO[sid, g + 1] == TO[sid, i]) g++
      part = ""; for (k = i; k <= g; k++) part = part vtok(sid, k)
      out = out "(" part ")?"; i = g; continue
    }
    out = out vtok(sid, i)
  }
  return out
}
function dfs(u,   n, D, i, v) {
  COLOR[u] = 1
  n = split(DEP[u], D, ",")
  for (i = 1; i <= n; i++) {
    v = D[i]; if (v == "" || v == "-") continue
    if (COLOR[v] == 1) emit("name.registry-term", "term:" u, "the families " q(u) " and " q(v) " depend on each other in a cycle (M3, D-60)")
    else if (COLOR[v] == "") dfs(v)
  }
  COLOR[u] = 2
}
function shape_msg(sid, why) { return why " — the grammar of " sid " is " q(REND[sid]) " (standards/naming/surfaces.tsv)" }
function launchd_check(subj, s,   r, n, P, i, W, nw) {
  r = tmatch("launchd", s, "")
  if (r != "") emit(SRULE["launchd"], subj, shape_msg("launchd", r))
  n = split(s, P, ".")
  if (n < 4 || P[1] != "com" || P[n] != "plist") return
  W[1] = P[2]; alias_check(subj, W, 1, 1, "launchd")
  for (i = 3; i < n; i++) {
    if (!kebab(P[i])) emit(SRULE["launchd"], subj, shape_msg("launchd", "the label segment " q(P[i]) " is not lowercase kebab"))
    nw = split(P[i], W, /[-_]/)
    alias_check(subj, W, 1, nw, "launchd-id")
  }
}
function bin_check(subj, leaf,   s, r) {
  s = tolower(leaf)
  if (index(s, ".") == 0 && TEXT["bin"]) return   # a PATH command has no extension and needs no family (D-11)
  r = tmatch("bin", s, "")
  if (r != "") emit(SRULE["bin"], subj, shape_msg("bin", r))
}
function test_check(path,   n, S, leaf, istest, py, probe, r, i) {
  n = split(path, S, "/"); leaf = S[n]
  istest = (leaf ~ /-test\.[a-z0-9]+$/ || leaf ~ /\.test\./ || leaf ~ /^test_.*\.py$/ || leaf ~ /_test\.[a-z0-9]+$/)
  if (istest) {
    # D-14: Python keeps test_<aspect>.py, which the grammar reads as <aspect>.test.py
    py = (leaf ~ /^test_.*\.py$/); probe = path
    if (py) { probe = ""; for (i = 1; i < n; i++) probe = probe S[i] "/"; probe = probe "x.test.py" }
    r = tmatch("test", probe, "")
    if (r == "" && py && leaf !~ /^test_[a-z][a-z0-9_]*\.py$/) r = "the Python test " q(leaf) " is not test_<aspect>.py"
    if (r != "") emit(SRULE["test"], path, shape_msg("test", r))
  } else if (n >= 3) {
    r = tmatch("test", path, "family")
    if (r != "") emit(SRULE["test"], "tests/" S[2], shape_msg("test", r))
  }
}
# harbor#1301: an MCP tool as Claude Code names it. Snake throughout, because the harness joins server and tool with __.
function mcp_check(subj, name,   r) {
  if (name !~ /^mcp__[a-z][a-z0-9_]*__[a-z][a-z0-9_]*$/) emit(SRULE["mcp-tool"], subj, "an MCP tool is mcp__<server>__<tool>, lowercase snake: " q(name) " (harbor#1301)")
  r = tmatch("mcp-tool", name, "")
  if (r != "") emit(SRULE["mcp-tool"], subj, shape_msg("mcp-tool", r))
}
# harbor#957: a release tag is v<semver> and its release title is the tag; no other tag shape names a release.
function release_check(subj, name) {
  if (name !~ /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$/)
    emit("name.release-tag", subj, "a release tag is v<major>.<minor>.<patch> (SemVer 2.0), and the release title is the tag: " q(name) " (harbor#957)")
}
# harbor#1001: one field grammar for a JSON key, a registry column and a config key: snake; an instant ends _at, a day _date,
# a duration _s or _ms, a size _bytes, an id _id, a boolean starts is_ or has_. Retired spellings are listed.
function field_check(subj, name,   bad, last) {
  if (name !~ /^[a-z][a-z0-9]*(_[a-z0-9]+)*$/) { emit("name.field", subj, "a field is lowercase snake_case: " q(name) " (harbor#1001)"); return }
  last = name; sub(/^.*_/, "", last)
  if (last ~ /^(ts|time|timestamp|datetime|tstamp)$/) bad = "an instant ends _at, a day ends _date"
  else if (last ~ /^(sec|secs|second|seconds)$/) bad = "seconds end _s"
  else if (last ~ /^(msec|millis|milliseconds)$/) bad = "milliseconds end _ms"
  else if (last ~ /^(kb|mb|gb|size)$/) bad = "a size ends _bytes"
  else if (last ~ /^(uid|ident)$/) bad = "an identifier ends _id"
  else if (last == "flag") bad = "a boolean starts is_ or has_"
  if (bad != "") emit("name.field", subj, q(name) ": " bad " (harbor#1001)")
}
# harbor#978 (D-21): a commit subject, which is also a PR title, is <type>(<family>): <subject>, the type a commit-type term.
# Scope = a register domain: the same family-or-domain set a name slot takes (operator, 2026-10-27).
function commit_check(subj, name,   t, sc, n, S, i, rest) {
  if (name !~ /^[a-z][a-z0-9]*(\([^()]*\))?!?: ./) {
    emit("name.commit", subj, "a commit subject is <type>(<domain>): <subject>: " q(name) " (D-21)"); return
  }
  t = name; sub(/[(!:].*$/, "", t)
  if (!(("commit-type" SUBSEP t) in IS)) emit("name.commit", subj, "the type " q(t) " is not a registered commit type (terms.tsv, class commit-type)")
  if (name !~ /^[a-z][a-z0-9]*\(/) { emit("name.commit", subj, "the subject has no (<family>) scope: " q(name) " (D-21)"); return }
  sc = name; sub(/^[^(]*\(/, "", sc); sub(/\).*$/, "", sc)
  n = split(sc, S, ",")
  for (i = 1; i <= n; i++)
    if (!(("family" SUBSEP S[i]) in IS) && !(("domain" SUBSEP S[i]) in IS)) emit("name.commit", subj, "the scope " q(S[i]) " is not a registered domain (terms.tsv, class family or domain; D-21)")
}
# harbor#978 (D-21): a branch is <type>/<issue>-<slug>, lowercase kebab, the type a commit-type term.
function branch_check(subj, name,   t) {
  if (name !~ /^[a-z][a-z0-9]*\/[0-9]+-[a-z0-9]+(-[a-z0-9]+)*$/) {
    emit("name.branch", subj, "a branch is <type>/<issue>-<slug>, lowercase kebab: " q(name) " (D-21)"); return
  }
  t = name; sub(/\/.*$/, "", t)
  if (!(("commit-type" SUBSEP t) in IS)) emit("name.branch", subj, "the type " q(t) " is not a registered commit type (terms.tsv, class commit-type)")
}
function skill_check(subj, name,   r) {
  r = tmatch("skill", tolower(name), "")
  if (r != "") emit(SRULE["skill"], subj, shape_msg("skill", r))
}
function id_check(subj, id, ctx,   n, P, i, W, nw) {
  n = split(id, P, ".")
  if (n < 2) emit("name.rule-id", subj, "an id is <family>.<slug>, at least two .-separated segments (D-10)")
  if (!(("family" SUBSEP P[1]) in IS) && !(("subject" SUBSEP P[1]) in IS))
    emit("name.rule-id", subj, q(P[1]) " is not a registered family or lint subject (D-10)")
  for (i = 1; i <= n; i++) {
    if (!kebab(P[i])) emit("name.rule-id", subj, "the segment " q(P[i]) " is not lowercase kebab (D-10)")
    nw = split(tolower(P[i]), W, /[-_]/)
    alias_check(subj, W, 1, nw, ctx)
  }
}
function env_check(subj, name,   low, r, i, n, rest, W, sysv) {
  if (name !~ /^[A-Z][A-Z0-9_]*$/) emit("name.env", subj, "an env var is SCREAMING_SNAKE: " q(name) " (D-15)")
  low = tolower(name)
  r = tmatch("env", low, "")
  sysv = ("repo" in VAL) ? VAL["repo"] : ""
  if (sysv == "") {
    n = split(low, W, "_")
    alias_check(subj, W, 1, 1, "env-prefix")
    rest = substr(low, length(W[1]) + 2)
  } else { gsub(/-/, "_", sysv); rest = substr(low, length(sysv) + 2) }
  if (r != "") emit(SRULE["env"], subj, shape_msg("env", r))
  n = split(rest, W, "_")
  for (i = 1; i <= n; i++) {
    if (W[i] == "") emit("name.word", subj, "an empty word: a doubled or trailing _ (§6.3)")
    else if (W[i] ~ /^[0-9]/) emit("name.word", subj, "the word " q(W[i]) " starts with a digit (§6.3)")
  }
  alias_check(subj, W, 1, n, "env")
}
function leafctx(S, n,   leaf) {
  leaf = tolower(S[n])
  if (n == 2 && S[1] == "bin") return (index(leaf, ".") ? "bin" : "cli")
  if (S[1] == "systemd") return "systemd"
  return "path"
}
function file_rec(path, fmode,   n, S, i, acc) {
  n = split(path, S, "/")
  acc = ""
  for (i = 1; i <= n; i++) {
    acc = (i == 1) ? S[i] : acc "/" S[i]
    EXISTS[acc] = 1
    if (mode == "registry") continue
    if (i < n) { if (!(acc in DIRDONE)) { DIRDONE[acc] = 1; seg_check(acc, S[i], 0, "path", acc, "") } }
    else seg_check(path, S[i], 1, leafctx(S, n), path, fmode)
  }
  if (mode == "registry") return
  if (tolower(S[n]) ~ /\.plist$/ && (tolower(S[n]) ~ /^com\./ || S[1] == "launchd")) launchd_check(path, tolower(S[n]))
  if (n == 2 && S[1] == "bin") bin_check(path, S[2])
  if (S[1] == "tests") test_check(path)
  if (S[1] == "skills" && n >= 3 && !(("skills/" S[2]) in SKDONE)) { SKDONE["skills/" S[2]] = 1; skill_check("skills/" S[2], S[2]) }
  if (S[1] == "vendor" && S[2] == "skills" && n >= 4 && !(("vendor/skills/" S[3]) in SKDONE)) {
    SKDONE["vendor/skills/" S[3]] = 1; skill_check("vendor/skills/" S[3], S[3])
  }
}
function check_rec(sf, nm,   p) {
  if (sf in VIEWOF) sf = VIEWOF[sf]   # path.test is test, case is path: surfaces.tsv says so, not this function
  if (sf == "path" || sf == "directory" || sf == "ts-file" || sf == "fixture") file_rec(nm, "")
  else if (sf == "bin") { p = nm; if (p !~ /^bin\//) p = "bin/" p; file_rec(p, "") }
  else if (sf == "test") { p = nm; if (p !~ /^tests\//) p = "tests/" p; file_rec(p, "") }
  else if (sf == "systemd") { p = nm; if (p !~ /^systemd\//) p = "systemd/" p; file_rec(p, "") }
  else if (sf == "launchd") { p = nm; if (p !~ /\.plist$/) p = p ".plist"; if (p !~ /^launchd\//) p = "launchd/" p; file_rec(p, "") }
  else if (sf == "skill") { seg_check("skills/" nm, nm, 0, "skill", "skills/" nm, ""); skill_check("skills/" nm, nm) }
  else if (sf == "env") env_check("env:" nm, nm)
  else if (sf == "mcp-tool") mcp_check("mcp-tool:" nm, nm)
  else if (sf == "release-tag") release_check("release-tag:" nm, nm)
  else if (sf == "field") field_check("field:" nm, nm)
  else if (sf == "commit") commit_check("commit:" nm, nm)
  else if (sf == "branch") branch_check("branch:" nm, nm)
  else if (sf == "rule-id" || sf == "check-id") id_check(sf ":" nm, nm, sf)
}
function shape(f, msg) { emit("name.registry-shape", REL[f], msg) }
# One fact the shell read off a directory: the kind its .agents/config.yaml declares, then each entry that kind
# requires and whether it is there (absent | file where a directory is wanted | dir where a file is wanted | ok).
function repokind_check(label, kind, tok, state,   sj) {
  sj = "repo-kind:" label
  if (tok ~ /^just:/) {   # a verb the kind requires of the justfile (verbs.tsv, harbor#1428); the subject carries no path, so a repo exempts it by verb
    if (state == "absent" && (kind in RKREQ)) emit("name.repo-verb", "repo-verb:" substr(tok, 6), "a " q(kind) " repo carries the verb " q(substr(tok, 6)) " in its justfile and this one has none (standards/naming/verbs.tsv)")
    return
  }
  if (kind == "") emit("name.repo-kind", sj, "this directory declares no repo-kind: its .agents/config.yaml needs a `repo-kind: <id>` line naming a row of repo-kinds.tsv")
  else if (!(kind in RKREQ)) emit("name.repo-kind", sj, "its kind " q(kind) " is not a row of repo-kinds.tsv")
  else if (state == "absent") emit("name.repo-kind", sj ":" tok, "a " q(kind) " repo carries " q(tok) " and this one has none (standards/naming/repo-kinds.tsv)")
  else if (state == "file") emit("name.repo-kind", sj ":" tok, q(tok) " is a directory in a " q(kind) " repo and this one has a file by that name (standards/naming/repo-kinds.tsv)")
  else if (state == "dir") emit("name.repo-kind", sj ":" tok, q(tok) " is a file in a " q(kind) " repo and this one has a directory by that name (standards/naming/repo-kinds.tsv)")
}
BEGIN {
  FS = "\t"; OFS = "\t"
  _npk = split(packt, _PK, " "); for (i = 1; i <= _npk; i++) ISPACK[_PK[i]] = 1   # the pack overlays (harbor#950)
  split("family verb domain client abbreviation kind layer concept system extension subject level mode outcome decision severity priority state control-type moment function lifecycle share relation modality tier strength commit-type", L, " ")
  for (i in L) CLASS[L[i]] = 1
  split("codename legacy banned abbreviation spelling", L, " "); for (i in L) AKIND[L[i]] = 1
  split("tool-required world-name legacy", L, " "); for (i in L) EKIND[L[i]] = 1
  split("family verb domain system", OGRAM, " ")   # the classes a name slot parses (slotclass); a repo family takes none of them
  TODAYN = jdn(today)
  if (exbase != "") {
    while ((getline l < exbase) > 0) {
      if (l ~ /^#/ || l == "") continue
      split(l, b, "\t"); if (b[1] == "id") continue
      if (b[4] == "legacy") BASEEX[b[2] SUBSEP b[3]] = 1
    }
    close(exbase); HASBASE = 1
  }
}
FILENAME != recfile {
  f = FILENAME
  if ((priv != "" && f == priv) || (f in ISPACK)) {
    if (!(f in SHORT)) { SHORT[f] = "terms.tsv"; REL[f] = (f in ISPACK) ? "terms.d/" base(f) " (pack overlay, harbor#950)" : "harbor-private/terms.tsv (private overlay, D-85)" }
    if (FNR == 1 && index($0, "\357\273\277") == 1) { $0 = substr($0, length("\357\273\277") + 1); emitw("name.registry-shape", "private-terms:bom", REL[f] " starts with a byte-order mark; it was stripped and the file read (D-85)") }
    if (sub(/\r$/, "")) { $0 = $0; if (!PCRLF) { PCRLF = 1; emitw("name.registry-shape", "private-terms:crlf", REL[f] " has CRLF line endings; they were stripped and the file read (D-85)") } }
    if ($0 ~ /^#/ || $0 ~ /^[ \t]*$/) next
    if (!(f in HDR)) {
      if ($0 != THDR) { emitb("name.registry-shape", "private-terms:header", REL[f] ": the header row differs from standards/naming/terms.tsv: the overlay carries the same columns (D-85)"); PBAD[f] = 1 }
      HDR[f] = NF; HDR1[f] = $1; for (i = 1; i <= NF; i++) COL[f, $i] = i; next
    }
    if (f in PBAD) next
    if (NF != HDR[f]) { emitb("name.registry-shape", "private-terms:line" FNR, REL[f] " line " FNR " has " NF " columns, the header has " HDR[f]); next }
    if ($1 in OVT) { emitb("name.registry-duplicate", "term:" $1, REL[f] " line " FNR " repeats the term " q($1) " of " OVT[$1]); next }
    if ($1 in T) { emitb("name.registry-duplicate", "term:" $1, REL[f] " line " FNR " repeats the core term " q($1)); next }
    if ($1 in AL) { emitb("name.registry-duplicate", "term:" $1, REL[f] " line " FNR ": " q($1) " is a core alias, and the overlay cannot make it a term (D-85)"); next }
    if ($1 !~ /^[a-z][a-z0-9]*(-[a-z0-9]+)*$/) { emitb("name.registry-shape", "private-term:" $1, REL[f] " line " FNR ": " q($1) " is not a lowercase kebab term, so the overlay would never match it (D-85)"); next }
    np = split(col(f, "class"), PC, ",")
    if (f in ISPACK) { if (col(f, "class") != "system") { emitb("name.registry-shape", "private-term:" $1, REL[f] " line " FNR ": class " q(col(f, "class")) " is not system: a pack registers its system terms only (harbor#950)"); next } }
    else for (i = 1; i <= np; i++) if (PC[i] != "client" && PC[i] != "system") {
      emitb("name.registry-shape", "private-term:" $1, REL[f] " line " FNR ": class " q(PC[i]) " is not client or system: the overlay adds only those (D-85)"); next }
    OVT[$1] = REL[f]
  }
  else if ((ovw != "" && f == ovw) || (ovf != "" && f == ovf)) {
    # The repo overlay (harbor#967), in the row shape of words.tsv and terms.tsv. Every finding is block and none is
    # exempted, as for the private overlay: a malformed or shadowing overlay must not read as clean.
    ofn = base(f); ofam = (f == ovf)
    if (ovhome) { emitb("name.overlay-shape", "overlay:" ofn, ofn " sits beside standards/naming/: this tree registers its words and families there, so the overlay was not read (harbor#967)"); next }
    if (FNR == 1 && index($0, "\357\273\277") == 1) { $0 = substr($0, length("\357\273\277") + 1); emitb("name.overlay-shape", "overlay:" ofn ":bom", ofn " starts with a byte-order mark: save it as plain UTF-8") }
    if (sub(/\r$/, "")) { $0 = $0; emitb("name.overlay-shape", "overlay:" ofn ":crlf", ofn " has CRLF line endings: save it with LF") }
    if ($0 ~ /^#/ || $0 ~ /^[ \t]*$/) next
    if (!(f in HDR)) {
      HDR[f] = NF; for (i = 1; i <= NF; i++) COL[f, $i] = i
      if ($0 != (ofam ? THDR : WHDR)) { OBAD[f] = 1; emitb("name.overlay-shape", "overlay:" ofn ":header", ofn ": the header row is not the one of " (ofam ? "terms.tsv" : "words.tsv") ", " q(ofam ? THDR : WHDR) ", so no row was read") }
      next
    }
    if (!(f in OBAD)) overlay_row(f, ofn, ofam)
    next
  }
  else if (FNR == 1) {
    NREG++; REGF[NREG] = f; SHORT[f] = base(f); REL[f] = (index(f, root "/") == 1) ? substr(f, length(root) + 2) : f
    if (index($0, "# " SHORT[f]) != 1) shape(f, "line 1 is not `# " SHORT[f] " …`, the house header (§4)")
  }
  if ($0 ~ /^#/) {
    if ($0 == "# fixture rows") FIXROWS[f] = 1
    if ($0 ~ /WHY/) WHY[f] = 1
    if (match($0, /standard_version: *[0-9]+\.[0-9]+\.[0-9]+/)) { v = substr($0, RSTART, RLENGTH); sub(/.*: */, "", v); SV[f] = v }
    next
  }
  if ($0 ~ /^[ \t]*$/) next
  if (!(f in HDR)) { if (SHORT[f] == "terms.tsv") THDR = $0; if (SHORT[f] == "words.tsv") WHDR = $0; HDR[f] = NF; HDR1[f] = $1; for (i = 1; i <= NF; i++) { COL[f, $i] = i; field_check("column:" SHORT[f] ":" $i, $i) }; next }
  if (NF != HDR[f]) shape(f, "line " FNR " has " NF " columns, the header has " HDR[f])
  sh = SHORT[f]; reg = sh; sub(/s?\.tsv$/, "", reg); sub(/ie$/, "y", reg)
  id = $1
  if ((f SUBSEP id) in SEEN) emit("name.registry-duplicate", reg ":" id, REL[f] " line " FNR " repeats the id " q(id))
  SEEN[f, id] = 1
  if (sh == "terms.tsv") {
    T[id] = 1; BROADER[id] = col(f, "broader"); PREF[id] = col(f, "pref"); DEP[id] = col(f, "depends_on")
    if (!kebab(id)) emit("name.registry-term", "term:" id, q(id) " is not a lowercase kebab term (§1.1)")
    nc = split(col(f, "class"), C, ",")
    if (nc == 0) emit("name.registry-term", "term:" id, q(id) " has no class (§1.1)")
    for (i = 1; i <= nc; i++) {
      if (!(C[i] in CLASS)) emit("name.registry-term", "term:" id, q(C[i]) " is not a class (§1.1)")
      IS[C[i], id] = 1
    }
    if ((("family" SUBSEP id) in IS) && (("verb" SUBSEP id) in IS))
      emit("name.registry-overlap", "term:" id, q(id) " is both a family and a verb, so a name could not be parsed back into its tuple (§3)")
    if (((("family" SUBSEP id) in IS) || (("verb" SUBSEP id) in IS)) && id ~ /-/)
      emit("name.registry-term", "term:" id, "a family or verb is one word: " q(id) " (§3)")
    if (("system" SUBSEP id) in IS) SYS[++NSYS] = id
    if (("level" SUBSEP id) in IS) LVL[++NLVL] = id
    if (("mode" SUBSEP id) in IS) { MODES[++NMODES] = id; na = split(col(f, "alt"), AA, "; "); for (i = 1; i <= na; i++) if (AA[i] != "-" && AA[i] != "") LEGMODE[AA[i]] = id }
    st = col(f, "stability")
    if (st != "experimental" && st != "stable" && st != "deprecated") emit("name.registry-term", "term:" id, "stability " q(st) " is not experimental, stable or deprecated (D-19)")
    if (st == "deprecated" && !(col(f, "successor") ~ /^[a-z]/)) emit("name.registry-term", "term:" id, "a deprecated term names its successor (D-19)")
  } else if (sh == "words.tsv") {
    KW[id] = 1; HAVEWORDS = 1   # an English or technical word a name may carry beyond the terms (D-18, harbor#984)
  } else if (sh == "aliases.tsv") {
    AL[id] = 1; AKD[id] = col(f, "kind"); ATERM[id] = col(f, "term"); ASURF[id] = col(f, "surfaces"); AREM[id] = col(f, "remove_by")
    AWN[id] = split(id, AW, "-"); for (i = 1; i <= AWN[id]; i++) AWORD[id, i] = AW[i]
    if (!kebab(id)) emit("name.registry-term", "alias:" id, q(id) " is not a lowercase kebab alias")
    if (!(AKD[id] in AKIND)) emit("name.registry-term", "alias:" id, "kind " q(AKD[id]) " is not codename, legacy, banned, abbreviation or spelling")
    if (AKD[id] == "legacy" && !isdate(AREM[id])) emit("name.registry-term", "alias:" id, "a legacy alias has a remove_by date")
    if (AKD[id] == "codename" && AKD[id] != "" && AREM[id] != "never") emit("name.registry-term", "alias:" id, "a codename never expires: remove_by is never")
    if (AKD[id] == "spelling" && (AREM[id] != "never" || ASURF[id] != "-")) emit("name.registry-term", "alias:" id, "a spelling is allowed nowhere and never expires: surfaces -, remove_by never (D-16)")
    if (isdate(AREM[id]) && TODAYN >= jdn(AREM[id]) - 14) AEXP[++NAEXP] = id   # judged at END, once controls.tsv has set the mode of the rule
    if (AKD[id] == "codename") for (i = 1; i <= split(ASURF[id], SS, ","); i++) if (SS[i] == "env-prefix") SYS[++NSYS] = id
  } else if (sh == "exemptions.tsv") {
    er = col(f, "rule"); es = col(f, "subject"); ek = col(f, "kind"); ed = col(f, "remove_by")
    if (!(ek in EKIND)) emit("name.registry-shape", "exemption:" id, "kind " q(ek) " is not tool-required, world-name or legacy")
    if (ek == "legacy" && !isdate(ed)) emit("name.registry-shape", "exemption:" id, "a legacy row has a remove_by date")
    gb = es; if (gb ~ /^\*\*\//) gb = substr(gb, 4)
    if (selfpath(es) || (es ~ /\/$/ && (index("standards/naming/", es) == 1 || index("bin/lint-name.sh", es) == 1 || index("fixtures/naming/", es) == 1)) \
        || (es ~ /^\*\*\// && (index(" " files " lint-name.sh naming.md ", " " gb " ") > 0))) {
      emit("name.exemption-self", "exemption:" id, "names " q(es) ": the registry, its lint and its fixtures cannot be exempted (§10.4)"); next
    }
    if (HASBASE && ek == "legacy" && !((er SUBSEP es) in BASEEX))
      emit("name.exemption-grow", "exemption:" id, "a legacy row the base does not have: the list only shrinks (§10.3)")
    if (ek == "legacy") { NLEG++; LEGACY[id] = es; if (f in FIXROWS) FIXLEG[id] = 1 }
    if ((ek == "legacy" && (nolegacy == 1 || mode == "check")) || (isdate(ed) && TODAYN > jdn(ed))) next
    if (er == "*" && ek == "legacy") emit("name.registry-shape", "exemption:" id, "a legacy row names one rule, never *")
    if (es ~ /^\*\*\//) { g = substr(es, 4); gsub(/\./, "[.]", g); gsub(/\*/, "[^/]*", g)
      NGLOB++; GLR[NGLOB] = er; GLB[NGLOB] = "^" g "$"; GLI[NGLOB] = id }
    else if (es ~ /\/$/) { NPRE++; PRER[NPRE] = er; PRES[NPRE] = es; PREI[NPRE] = id }
    else EXACT[er, es] = id
  } else if (sh == "renames.tsv") {
    # An env row with a remove_by date is a dual-read window: until that date every reader reads the new name, then the
    # old one, so the old name stays in the tree on purpose and the row, not an exemptions.tsv row, is what it rests on.
    # Past the date it is judged again, which is when bin/standard-rename.sh --apply collapses the reads. A --check
    # judges a name at its birth, and a first scan (--gen exemptions) judges the tree bare, so neither applies it.
    ro = col(f, "old"); rd = col(f, "remove_by")
    if (col(f, "surface") == "env" && ro ~ /^env:/ && isdate(rd) && TODAYN <= jdn(rd) && mode != "check" && nolegacy != 1) RENWIN[ro] = id
  } else if (sh == "controls.tsv") {
    RULE[++NRULE] = id; ctype = col(f, "type"); cm = col(f, "mode"); mm = (cm in LEGMODE) ? LEGMODE[cm] : cm
    MODE[id] = mm; CTYPE[id] = ctype; CENG[id] = col(f, "engine"); CSURF[id] = col(f, "surfaces"); CRED[id] = col(f, "red_proof")
    COWN[id] = col(f, "owner"); CMOM[id] = col(f, "moment"); CFUN[id] = col(f, "function"); CSTAB[id] = col(f, "stability"); CSUCC[id] = col(f, "successor")
    CTYPES[ctype] = 1; KEY[ctype, id] = 1; if (col(f, "emitted") != "-" && col(f, "emitted") != "") KEY[ctype, col(f, "emitted")] = 1
    if (!(("mode" SUBSEP mm) in IS)) {
      ml = ""; for (i = 1; i <= NMODES; i++) ml = ml (i > 1 ? (i == NMODES ? " or " : ", ") : "") MODES[i]
      emit("name.control-shape", "control:" id ":mode", "mode " q(cm) " is not " ml " (D-29)")
    }
    if (!((("control-type") SUBSEP ctype) in IS)) emit("name.control-shape", "control:" id ":type", "type " q(ctype) " is not a control-type term (D-62)")
    if (!(("moment" SUBSEP CMOM[id]) in IS)) emit("name.control-shape", "control:" id ":moment", "moment " q(CMOM[id]) " is not a moment term")
    if (!(("function" SUBSEP CFUN[id]) in IS)) emit("name.control-shape", "control:" id ":function", "function " q(CFUN[id]) " is not a function term")
    if (!(("family" SUBSEP COWN[id]) in IS)) emit("name.control-shape", "control:" id ":owner", "owner " q(COWN[id]) " is not a registered family (09a C4)")
    if (CSTAB[id] != "experimental" && CSTAB[id] != "stable" && CSTAB[id] != "deprecated") emit("name.control-shape", "control:" id ":stability", "stability " q(CSTAB[id]) " is not experimental, stable or deprecated (D-19)")
    if (CSTAB[id] == "deprecated" && CSUCC[id] == "-") emit("name.control-shape", "control:" id ":successor", "a deprecated control names its successor (D-19)")
    if (CSTAB[id] == "stable" && CRED[id] == "-") emit("name.control-shape", "control:" id ":stability", "a stable control names the suite that watched it red: stable is derived from an observed red proof (harbor#1422)")
  } else if (sh == "surfaces.tsv") {
    SURF[id] = 1; SRULE[id] = col(f, "rule"); REND[id] = col(f, "renderer"); SEX[id] = col(f, "example"); SCHK[id] = col(f, "checked")
    vw = col(f, "view")   # - | <surface>[:<rule>,<rule>]: this id is that surface, for those rules only when some are listed
    if (vw != "" && vw != "-") { vk = index(vw, ":"); VIEWOF[id] = (vk ? substr(vw, 1, vk - 1) : vw); VIEWR[id] = (vk ? substr(vw, vk + 1) : "") }
    if (SRULE[id] != "" && SRULE[id] != "-") {
      tcompile(id, REND[id])
      if (TERR[id] != "") emit("name.surface-grammar", "surface:" id, shape_msg(id, TERR[id]))
    }
  } else if (sh == "kinds.tsv") {
    KIND[id] = 1
    if (!(("kind" SUBSEP id) in IS)) emit("name.registry-shape", "kind:" id ":id", q(id) " is not a term of class kind (§4)")
    if (!(("layer" SUBSEP col(f, "layer")) in IS)) emit("name.registry-shape", "kind:" id ":layer", "layer " q(col(f, "layer")) " is not a layer term (§5)")
    if (!(("level" SUBSEP col(f, "level")) in IS)) emit("name.registry-shape", "kind:" id ":level", "level " q(col(f, "level")) " is not a level term (D-57)")
    if (!(("lifecycle" SUBSEP col(f, "lifecycle")) in IS)) emit("name.registry-shape", "kind:" id ":lifecycle", "lifecycle " q(col(f, "lifecycle")) " is not a lifecycle term (09a S5)")
    ns = split(col(f, "share"), SH, ",")
    if (ns == 0) emit("name.registry-shape", "kind:" id ":share", "share is empty: a kind names the set of core, hub, workload or personal it may take (D-83)")
    for (i = 1; i <= ns; i++) if (!(("share" SUBSEP SH[i]) in IS)) emit("name.registry-shape", "kind:" id ":share", "share " q(SH[i]) " is not a share term: core, hub, workload or personal (D-83)")
    KSURF[id] = col(f, "surface")   # judged in END: the file order is registers.tsv rows, so surfaces.tsv may come after kinds.tsv
    nrr = split(col(f, "required_relations"), RR, " ")
    for (i = 1; i <= nrr; i++) {   # <relation>[:<min>..<max>]; standard-shape.sh judges the bound (harbor#996 G2)
      rn = RR[i]; rb = ""; hb = index(rn, ":") > 0   # hb, not rb != "": `notifies:` is a bound, and an empty one
      if (hb) { rb = substr(rn, index(rn, ":") + 1); rn = substr(rn, 1, index(rn, ":") - 1) }
      if (rn != "-" && !(("relation" SUBSEP rn) in IS)) emit("name.registry-shape", "kind:" id ":relations", "relation " q(rn) " is not a term of class relation (research/15 §3)")
      if (hb && (rb !~ /^[0-9]+\.\.([0-9]+|\*)$/ || (rb !~ /\*$/ && substr(rb, 1, index(rb, ".") - 1) + 0 > substr(rb, index(rb, "..") + 2) + 0)))
        emit("name.registry-shape", "kind:" id ":relations", "bound " q(RR[i]) " is not <min>..<max>, min no more than max, max a number or * (harbor#996 G2)")
    }
  } else if (sh == "repo-kinds.tsv") {
    RKREQ[id] = col(f, "required")
  } else if (sh == "registers.tsv") {
    REGR[id] = 1; RFILE[id] = col(f, "file")
    if (RFILE[id] != "-" && RFILE[id] !~ /^[a-z][a-z0-9-]*\.tsv$/) emit("name.registry-shape", "register:" id, "file " q(RFILE[id]) " is not the name of a registry file: a bare lowercase kebab .tsv beside registers.tsv")
  }
  next
}
$1 == "F" { file_rec($2, $3); next }
$1 == "X" { file_rec($2, ""); next }
$1 == "G" { emit($2, $3, $4); next }
$1 == "E" { env_check("env:" $2, $2); next }
$1 == "C" { check_rec($2, $3); next }
$1 == "D" { repokind_check($2, $3, $4, $5); next }
$1 == "K" { if (!(($2 SUBSEP $3) in KEY)) emit("name.control-unregistered", $2 ":" $3, "the tree carries the " $2 " " q($3) " and controls.tsv has no row for it (D-62)"); next }
$1 == "M" { emit("name.control-unregistered", $2 ":census", "the census " q($3) " of the " $2 " controls found no id: its source is missing or empty, so nothing was held against controls.tsv (D-62)"); next }
$1 == "P" { r = tmatch($2, tolower($3), ""); if (r == "") print "P", "ok", tslots($2); else print "P", "err", r; next }
$1 == "R" { if (!($2 in TJ)) { print "R", "err", "surfaces.tsv has no grammar for " q($2); next }
            delete V; for (i = 3; i <= NF; i++) { kv = $i; eq = index(kv, "="); V[substr(kv, 1, eq - 1)] = substr(kv, eq + 1) }
            o = trender($2, V); if (o == "") print "R", "err", RERR; else print "R", "ok", o; next }
$1 == "T" { sid = $2; if (!(sid in TJ)) { print "T", sid, "err", "surfaces.tsv has no grammar for " q(sid); next } ex = SEX[sid]; r = tmatch(sid, tolower(ex), "")
            if (r != "") { print "T", sid, "err", "the example " q(ex) " does not parse: " r; next }
            t1 = tslots(sid); delete V; for (k in VAL) V[k] = VAL[k]
            o = trender(sid, V)
            if (o == "") { print "T", sid, "err", "render of the parsed example failed: " RERR; next }
            if (o != ex) { print "T", sid, "err", "render(parse(" q(ex) ")) is " q(o); next }
            r = tmatch(sid, tolower(o), "")
            if (r != "" || tslots(sid) != t1) { print "T", sid, "err", "parse(render(t)) differs from t for " q(ex); next }
            print "T", sid, "ok"; next }
# harbor#1259: one anchored ERE per grammar-backed surface. A Python test (D-14) and a PATH command (D-11) are the two
# forms the lint judges outside the grammar shape, so the validator takes them too, as test_check and bin_check do.
$1 == "V" { sid = $2
  if (!(sid in TJ)) { print "V", sid, "err", "surfaces.tsv has no grammar for " q(sid); next }
  if (TERR[sid] != "") { print "V", sid, "err", TERR[sid]; next }
  VERR = ""; n = NTK[sid]; body = vre(sid, 1, n)
  if (sid == "test") {
    ap = 0; for (i = 1; i <= n; i++) if (TK[sid, i] == "S" && tolower(TV[sid, i]) == "aspect") ap = i
    if (ap == 0) VERR = "the test grammar has no <aspect> slot for the Python form (D-14)"
    else body = "(" body ")|(" vre(sid, 1, ap - 1) "test_[a-z][a-z0-9_]*\\.py)"
  }
  if (sid == "bin" && TEXT[sid]) body = "(" body ")|([a-z][a-z0-9]*(-[a-z][a-z0-9]*)*)"
  if (VERR != "") { print "V", sid, "err", VERR; next }
  print "V", sid, "ok", "^(" body ")$"; next }
END {
  # An alias warns until its remove_by date and blocks from it (operator decision 2026-10-29, harbor#968).
  for (i = 1; i <= NAEXP; i++) {
    id = AEXP[i]; FORCEWARN = (TODAYN < jdn(AREM[id]))
    emit("name.alias-expired", "alias:" id, q(id) " is removed by " AREM[id] (TODAYN >= jdn(AREM[id]) ? ", which has passed" : ", within 14 days") ": rename what still uses it (§10.3)")
  }
  FORCEWARN = 0
  for (i = 1; i <= NREG; i++) {
    f = REGF[i]
    if (!(f in WHY)) shape(f, "no `# WHY` line in the header (§4)")
    if (!(f in SV)) shape(f, "no `# standard_version: X.Y.Z` line in the header (D-28)")
    else if (SV[REGF[1]] != "" && SV[f] != SV[REGF[1]]) shape(f, "standard_version " SV[f] " differs from " SV[REGF[1]] " in " REL[REGF[1]] " (D-28)")
    if (HDR1[f] != "id") shape(f, "the header row does not start with the id column (§4)")
  }
  for (id in T) if (BROADER[id] != "-" && BROADER[id] != "" && !(BROADER[id] in T))
    emit("name.registry-term", "term:" id, "broader " q(BROADER[id]) " is not a term (§1.1)")
  for (a in AL) {
    if (a in T) emit("name.registry-overlap", "alias:" a, q(a) " is both an alias and a term: labels are disjoint (§1.1)")
    if (AKD[a] != "abbreviation" && AKD[a] != "spelling" && ATERM[a] != "-" && !(ATERM[a] in T)) emit("name.registry-term", "alias:" a, "its term " q(ATERM[a]) " is not a term")
  }
  # terms: depends_on edges between families, and the level chain (D-57)
  for (id in T) {
    nd = split(DEP[id], DD, ",")
    # harbor#1001: a severity sets one default priority; its depends_on names that priority term, one severity per priority
    if (("severity" SUBSEP id) in IS) {
      if (nd != 1 || !(("priority" SUBSEP DD[1]) in IS)) emit("name.registry-term", "term:" id, "a severity names the one priority it sets in depends_on (p0..p3, harbor#1001)")
      else if (SEVOF[DD[1]] != "") emit("name.registry-term", "term:" id, "the priority " q(DD[1]) " is already set by " q(SEVOF[DD[1]]) ": one priority per severity (harbor#1001)")
      else SEVOF[DD[1]] = id
      continue
    }
    for (i = 1; i <= nd; i++) {
      d = DD[i]; if (d == "" || d == "-") continue
      if (!(("family" SUBSEP d) in IS)) emit("name.registry-term", "term:" id, "depends_on " q(d) " is not a family (M3, D-60)")
      else if (!(("family" SUBSEP id) in IS)) emit("name.registry-term", "term:" id, "only a family has depends_on (M3, D-60)")
    }
  }
  for (id in T) if (DEP[id] != "" && DEP[id] != "-" && COLOR[id] == "") dfs(id)
  nroots = 0
  for (i = 1; i <= NLVL; i++) {
    l = LVL[i]; lp = BROADER[l]
    if (lp == "-" || lp == "") { nroots++; continue }
    if (!(("level" SUBSEP lp) in IS)) emit("name.registry-term", "term:" l, "the level " q(l) " sits under " q(lp) ", which is not a level: levels contain levels (D-57)")
    else { LCHILD[lp]++; if (LCHILD[lp] == 2) emit("name.registry-term", "term:" lp, "two levels share the parent " q(lp) ": one parent per level (D-57)") }
  }
  if (NLVL > 0 && nroots != 1) emit("name.registry-term", "term:level", "the levels have " nroots " roots, not one (D-57)")
  # controls: references resolve, and what a control names exists in the tree
  for (i = 1; i <= NRULE; i++) {
    id = RULE[i]
    if (CSUCC[id] != "-" && CSUCC[id] != "" && !(CSUCC[id] in CTYPE)) emit("name.control-shape", "control:" id ":successor", "successor " q(CSUCC[id]) " is not a control (D-19)")
    if (CSURF[id] != "-") { ns = split(CSURF[id], SS2, ","); for (j = 1; j <= ns; j++) if (!(SS2[j] in SURF)) emit("name.control-shape", "control:" id ":surfaces", "surface " q(SS2[j]) " is not a row of surfaces.tsv") }
    if (mode == "tree" || mode == "registry") {
      if (CENG[id] != "-" && !(CENG[id] in EXISTS)) emit("name.control-shape", "control:" id ":engine", "engine " q(CENG[id]) " is not in the tree")
      if (CRED[id] != "-" && !(CRED[id] in EXISTS)) emit("name.control-shape", "control:" id ":red_proof", "red_proof " q(CRED[id]) " is not in the tree")
    }
  }
  for (sid in VIEWOF) {
    if (!(VIEWOF[sid] in SURF)) emit("name.registry-shape", "surface:" sid ":view", "view " q(VIEWOF[sid]) " is not a row of surfaces.tsv")
    else if (VIEWOF[sid] in VIEWOF) emit("name.registry-shape", "surface:" sid ":view", "view " q(VIEWOF[sid]) " is itself a view: a view is of a surface judged by its own code")
    nv = split(VIEWR[sid], VR, ",")
    for (j = 1; j <= nv; j++) if (VR[j] != "" && !(VR[j] in CTYPE)) emit("name.registry-shape", "surface:" sid ":view-rule:" VR[j], "view rule " q(VR[j]) " is not a row of controls.tsv")
  }
  for (sid in SURF) if (SRULE[sid] != "-" && SRULE[sid] != "" && !(SRULE[sid] in CTYPE))
    emit("name.surface-grammar", "surface:" sid, "its rule " q(SRULE[sid]) " is not a row of controls.tsv")
  for (id in KSURF) if (!(KSURF[id] in SURF)) emit("name.registry-shape", "kind:" id ":surface", "surface " q(KSURF[id]) " is not a row of surfaces.tsv")
  for (id in T) if ((("kind" SUBSEP id) in IS) && !(id in KIND)) emit("name.registry-shape", "kind:" id, "the kind term " q(id) " has no row in kinds.tsv (§4)")
  for (t in CTYPES) if (!(t in REGR)) emit("name.registry-shape", "register:" t, "controls.tsv holds " t " controls and registers.tsv has no " q(t) " register (D-20)")
  nu = split(unlisted, UL, " ")
  for (i = 1; i <= nu; i++) emit("name.registry-shape", "register-file:" UL[i], q(UL[i]) " is in the registry and registers.tsv has no row naming it, so the lint never read it: a register is its file plus its row (harbor#950)")
  if (mode == "tree" || mode == "registry")
    for (i = 1; i <= NRULE; i++) id_check("rule:" RULE[i], RULE[i], "rule-id")
  if (mode == "tree")
    for (e in LEGACY) if (!(e in USED)) emit("name.exemption-stale", "exemption:" e, q(LEGACY[e]) " no longer produces a finding: delete the row (§10.3)")
  if (mode == "registry")
    for (e in FIXLEG) { s = LEGACY[e]; if (s !~ /:/ && s !~ /^\*\*\// && !(s in EXISTS) && !(s ~ /\/$/ && (substr(s, 1, length(s) - 1) in EXISTS)))
      emit("name.exemption-stale", "exemption:" e, q(s) " is not in the tree: delete the row (§10.3)") }
  print "S", NSUP + 0, NLEG + 0
}
'

judge() { # <mode> <records> [nolegacy] -> the judge's output
  _ex="$EXBASE"
  if [ -z "$_ex" ] && [ "$ACTION" != check ] && git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    _mb="$(git -C "$ROOT" merge-base HEAD "$BASE" 2>/dev/null)" || _mb=""
    if [ -n "$_mb" ] && git -C "$ROOT" cat-file -e "$_mb:$REL_REG/exemptions.tsv" 2>/dev/null; then
      git -C "$ROOT" show "$_mb:$REL_REG/exemptions.tsv" > "$T/exbase.tsv" 2>/dev/null && _ex="$T/exbase.tsv"
    fi
  fi
  # shellcheck disable=SC2046 # one path per registry file, none with spaces by construction
  awk -v mode="$1" -v surface="$SURFACE" -v today="$TODAY" -v exbase="$_ex" -v recfile="$2" -v root="$ROOT" \
      -v nolegacy="${3:-0}" -v files="$FILES" -v unlisted="$UNLISTED" -v priv="$PRIV" -v packt="$PACKT" \
      -v ovw="$OVW" -v ovf="$OVF" -v ovhome="$OVHOME" -v foreign="$FOREIGN" \
      "$JUDGE" $(for _f in $FILES; do reg_file "$_f"; printf ' '; done) ${PRIV:+"$PRIV"} $PACKT ${OVW:+"$OVW"} ${OVF:+"$OVF"} "$2"
}

# ── what a whole-tree or --changed run judges ────────────────────────────────
list_tree() { # -> "<mode> TAB <path>" for every tracked file
  if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$ROOT" ls-files -s | awk -F'\t' '{ split($1, m, " "); print m[1] "\t" $2 }'
  else
    ( cd "$ROOT" && find . -type f ! -path './.git/*' ! -path './build/*' -perm -u+x | sed 's#^\./#100755	#'
      cd "$ROOT" && find . -type f ! -path './.git/*' ! -path './build/*' ! -perm -u+x | sed 's#^\./#100644	#' )
  fi
}
list_changed() { list_since ACMR 1; }
# --added: the paths the branch brings in. --no-renames, so a rename is an add of its new path (git diff would
# otherwise call it R and a good name renamed to a bad one would pass); untracked files are not the branch's yet.
list_added() { list_since A 0; }
list_since() { # <diff-filter letters> <1 to add the untracked files>
  git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1 || return 1
  _mb="$(git -C "$ROOT" merge-base HEAD "$BASE" 2>/dev/null)" || return 1
  { git -C "$ROOT" diff --name-only --no-renames --diff-filter="$1" "$_mb" --; [ "$2" != 1 ] || git -C "$ROOT" ls-files --others --exclude-standard; } | sort -u > "$T/changed"
  # nothing changed is an empty list: the join below reads "the first file" as whichever input has lines, so given an
  # empty one it would take the whole tree for the changed set and judge the mode column as paths
  [ -s "$T/changed" ] || return 0
  list_tree | awk -F'\t' 'NR == FNR { c[$0] = 1; next } ($2 in c) { print; delete c[$2] } END { for (p in c) print "100644\t" p }' "$T/changed" -
}
# Env tokens: words in tracked code that start with a system, a codename or an alias, read with awk
# rather than grep (this box's grep is ugrep, which skips files by its own rules).
env_tokens() { # <file of "<mode> TAB <path>"> -> one name per line
  _pre="$(awk -F'\t' '/^#/ || !H[FILENAME]++ { next }
    FILENAME ~ /terms\.tsv$/ && $2 ~ /(^|,)system(,|$)/ { print $1 }
    FILENAME ~ /aliases\.tsv$/ && $3 != "abbreviation" && $3 != "spelling" && $1 !~ /-/ { print $1 }' "$(reg_file terms.tsv)" "$(reg_file aliases.tsv)" ${PRIV:+"$PRIV"} $PACKT \
    | tr 'a-z-' 'A-Z_' | sort -u | tr '\n' '|' | sed 's/|$//')"
  [ -n "$_pre" ] || return 0
  awk -F'\t' '{ p = $2 }
    p ~ /^(standards|fixtures)\/naming\// { next }
    p ~ /\.(sh|py|ts|tsx|mjs|cjs|js|awk|jq|yml|yaml|plist|service|timer)$/ || p ~ /^(bin|hooks|worklog)\/[^.\/]+$/ || p == "justfile" { print p }' "$1" > "$T/code"
  awk -v root="$ROOT" -v pre="$_pre" 'BEGIN { re = "[^A-Za-z0-9_](" pre ")_[A-Z0-9_]*[A-Z0-9]" }
    { f = root "/" $0
      while ((getline line < f) > 0) {
        line = " " line
        while (match(line, re)) { t = substr(line, RSTART + 1, RLENGTH - 1); seen[t] = 1; line = substr(line, RSTART + RLENGTH) }
      }
      close(f) }
    END { for (t in seen) print t }' "$T/code" | sort
}

unchecked() { log_fail "the judge (awk) failed, so nothing was judged"; log_nofix "bin/lint-name.sh"; log_end || :; exit "$UNCHECKED"; }

# ── reporting ────────────────────────────────────────────────────────────────
report() { # <judge output> <what was judged>
  _n=0; _blk=0; _shd=0
  awk -F'\t' '$1 == "F"' "$1" | sort -t '	' -k2,2 -k4,4 > "$T/findings"
  # Every finding is red under its own rule id, a shadow one too: shadow is how a rule earns warn (§10.3).
  [ -z "${LINT_REDLOG:-}" ] || cut -f2 "$T/findings" >> "$LINT_REDLOG" 2>/dev/null || :
  if [ "$FORMAT" = findings ]; then
    awk -F'\t' '$3 != "shadow" { printf "%s\t%s\t%s\n", ($3 == "block" ? "V" : "A"), $2, $4 }' "$T/findings"
  fi
  while IFS='	' read -r _k _r _l _s _m; do
    case "$_l" in
      shadow) _shd=$((_shd + 1)); continue ;;
      block)  _blk=$((_blk + 1)); log_fail "[$_r] $_s — $_m"; log_nofix "standards/naming.md" ;;
      *)      log_warn "[$_r] $_s — $_m" ;;
    esac
    _n=$((_n + 1))
  done < "$T/findings"
  _sup="$(awk -F'\t' '$1 == "S" { print $2 }' "$1")"; _leg="$(awk -F'\t' '$1 == "S" { print $3 }' "$1")"
  if [ "$_n" -eq 0 ]; then log_ok "$2: no finding"; fi
  [ "$_shd" -eq 0 ] || log_note "$_shd shadow finding(s) recorded, not shown: their rules are in shadow mode (D-29)"
  [ "${_sup:-0}" -eq 0 ] || log_note "$_sup finding(s) not shown: exempted by standards/naming/exemptions.tsv ($_leg legacy row(s), which only shrink)"
  [ "$_blk" -eq 0 ]
}

# lc_rules → `id TAB title` per bin/lint-conventions.sh rule, read from its source and never run: a
# judged tree's script is data here. The id is the first word of its `rule "…"` line (harbor#957).
lc_rules() {
  [ -f "$ROOT/bin/lint-conventions.sh" ] || return 0
  sed -n 's/^rule "\([^ "]*\) *\(.*\)"$/\1	\2/p' "$ROOT/bin/lint-conventions.sh"
}


# rk_facts → `D TAB <dir> TAB <kind> TAB <entry> TAB <state>`: what a directory holds against its repo-kind (--check repo-kind).
# The kind is the `repo-kind:` line of <dir>/.agents/config.yaml; the entries are the ones repo-kinds.tsv requires of that
# kind, each read as present, absent or the wrong sort. A directory is read, never a tree scan: harbor's own layout moves in
# harbor#956, and a scan would report the migration plan as findings. An entry the kind does not list is not a finding.
rk_facts() { # <dir, absolute or relative to --root>
  case "$1" in /*) _rd="$1" ;; *) _rd="$ROOT/$1" ;; esac
  [ -d "$_rd" ] || { log_fail "no such directory: $1"; log_fix "sh $SELF --check repo-kind <dir>, a directory on disk (a relative one is read from --root)"; log_end || :; exit 2; }
  _rk="$(sed -n 's/^repo-kind:[[:space:]]*\(.*\)$/\1/p' "$_rd/.agents/config.yaml" 2>/dev/null | head -n 1)"
  _rk="${_rk%%#*}"; _rk="$(printf '%s' "$_rk" | tr -d "\"' \t\r")"
  printf 'D\t%s\t%s\t-\t-\n' "$1" "$_rk"
  [ -n "$_rk" ] || return 0
  awk -F'\t' -v k="$_rk" '/^#/ || !H[FILENAME]++ { next } $1 == k { print $2 }' "$(reg_file repo-kinds.tsv)" | tr ' ' '\n' | while IFS= read -r _t; do
    [ -n "$_t" ] || continue
    case "$_t" in
      */) if [ -d "$_rd/$_t" ]; then _s=ok; elif [ -e "$_rd/${_t%/}" ]; then _s=file; else _s=absent; fi ;;
      *)  if [ -f "$_rd/$_t" ]; then _s=ok; elif [ -d "$_rd/$_t" ]; then _s=dir; else _s=absent; fi ;;
    esac
    printf 'D\t%s\t%s\t%s\t%s\n' "$1" "$_rk" "$_t" "$_s"
  done
  # The verbs verbs.tsv requires of the kind, each read off the justfile's recipe names (harbor#1428). No justfile is
  # name.repo-kind's finding already, so it is not repeated once per verb.
  [ -f "$_rd/justfile" ] || return 0
  awk -F'\t' -v k="$_rk" -v d="$1" 'FILENAME == ARGV[1] { if ($0 ~ /^[a-z][a-z0-9_-]*( [^:=]*)?:/ && index($0, ":=") == 0) { n = $0; sub(/[: ].*/, "", n); have[n] = 1 } next }
    /^#/ || !H[FILENAME]++ { next }
    $4 != "-" && index(" " $4 " ", " " k " ") { printf "D\t%s\t%s\tjust:%s\t%s\n", d, k, $1, ($1 in have) ? "ok" : "absent" }' "$_rd/justfile" "$(reg_file verbs.tsv)"
}

# census → `K TAB <register> TAB <id>` for every control id the tree carries, found by the extractor each
# registers.tsv row names in its `census` cell (<extractor>:<path>[=<arg>]). The judge holds each against
# controls.tsv, so a guard, check or pack added without a row is red (name.control-unregistered, D-62).
# Read with awk and sed rather than grep, which is ugrep on this box and skips files by its own rules.
census() {
  awk -F'\t' '/^#/ || !H[FILENAME]++ { next } $4 != "-" { t = $3; if (t ~ /^type=/) sub(/^type=/, "", t); else t = $1; print t "\t" $4 }' "$(reg_file registers.tsv)" | while IFS='	' read -r _rg _cs; do
    _x="${_cs%%:*}"; _a="${_cs#*:}"; _arg=""
    case "$_a" in *=*) _arg="${_a#*=}"; _a="${_a%%=*}" ;; esac
    : > "$T/cx"
    { case "$_x" in
      lines)       [ -f "$ROOT/$_a" ] && awk '/^#/ || /^[ \t]*$/ { next } { print $1 }' "$ROOT/$_a" ;;
      lint-rules)  [ -f "$ROOT/$_a" ] && sed -n 's/^rule "\([^ "]*\) *\(.*\)"$/\1/p' "$ROOT/$_a" ;;
      checks-table) [ -f "$ROOT/$_a" ] && awk -F'|' '/^## Register/ { on = 1; next } on && /^## / { exit }
                      on && /^\| `/ { x = $2; gsub(/[ `]/, "", x); print x }' "$ROOT/$_a" ;;
      required-checks) [ -f "$ROOT/$_a" ] && awk -F'\t' -v r="$_arg" '/^#/ || NF < 2 { next } $1 == r { print $2 }' "$ROOT/$_a" ;;
      inject-ids)  for _d in $(printf '%s' "$_a" | tr ',' ' '); do
                     [ -d "$ROOT/$_d" ] && find "$ROOT/$_d" -type f -name '*.md' | sort | while IFS= read -r _f; do
                       # the id: key, else the file name (D-43, harbor#1010: the id is derived)
                       awk -v n="${_f##*/}" 'BEGIN { sub(/\.md$/, "", n) } NR == 1 { if ($0 != "---") exit; f = 1 } /^---$/ && NR > 1 { exit }
                         /^id:/ { sub(/^id:[ \t]*/, ""); if ($0 != "") { print; d = 1; exit } } END { if (f && !d) print n }' "$_f"
                     done
                   done ;;
      hook-rules)  for _f in $(printf '%s' "$_a" | tr ',' ' '); do
                     [ -f "$ROOT/$_f" ] && sed -n 's/^[[:space:]]*# control: \([^ ]*\).*$/\1/p' "$ROOT/$_f"
                   done ;;
      guard-rules) for _f in "$ROOT"/$_a; do
                     [ -f "$_f" ] && awk '/^[ \t]*#/ { next } { l = $0; while (l ~ /\\$/ && (getline nx) > 0) { sub(/\\$/, "", l); l = l " " nx }
                       if (l ~ /floor_block[ \t]+[a-z]+[ \t]/ && l !~ /floor_block\(\)/) {
                         n = split(l, w, /[ \t]+/); t = w[n]; sub(/[;&|)]+$/, "", t); sub(/\.[a-z]+$/, "", t)
                         for (i = 1; i < n; i++) if (w[i] == "floor_block") { print w[i + 1] ":" t; break } } }' "$_f"
                   done ;;
    esac; } > "$T/cx" 2>/dev/null
    # a source that is missing or yields no id is a census that saw nothing, never a clean one
    if [ -s "$T/cx" ]; then awk -v r="$_rg" 'NF { print "K\t" r "\t" $0 }' "$T/cx"
    else printf 'M\t%s\t%s\n' "$_rg" "$_cs"; fi
  done
}

# register_facts <file of "<mode> TAB <path>"> → `G TAB <rule> TAB <subject> TAB <message>`: every tracked .tsv against its row in
# schemas.tsv, and every reference column against its target (standard.register-declared, standard.reference-resolves; harbor#1424).
# bin/standard-register.sh does the reading, so the lint and every engine meet a register through one reader. A fixture's own
# registers are the .tsv files under its overlay directory (below the top, which overlays the registry itself).
register_facts() {
  _sch="$(reg_file schemas.tsv)"
  [ -r "$_sch" ] || { log_note "no schemas.tsv in the registry: registers were not checked against a schema (harbor#1424)"; return 0; }
  # the vendored package always ships the reader (standard-vendor.sh --check says when it is gone); a bare copy of the lint judges names only
  [ -f "$HERE/standard-register.sh" ] || { log_note "no standard-register.sh beside the lint: registers were not checked against schemas.tsv (harbor#1424)"; return 0; }
  awk -F'\t' -v r="$ROOT" '{ print $2 "\t" r "/" $2 }' "$1" > "$T/regmap"
  # the registry's own files, as the run reads them (a fixture overlay applied); a registry outside the judged root is not that tree's
  case "$REG" in "$ROOT"/*)
    for _rf in "$REG"/*.tsv; do [ -f "$_rf" ] || continue; printf '%s/%s\t%s\n' "$REL_REG" "${_rf##*/}" "$(reg_file "${_rf##*/}")"; done >> "$T/regmap"
    [ -z "$_od" ] || find "$_od" -maxdepth 1 -type f -name '*.tsv' | while IFS= read -r _xf; do printf '%s/%s\t%s\n' "$REL_REG" "${_xf##*/}" "$(reg_file "${_xf##*/}")"; done >> "$T/regmap" ;;
  esac
  [ -z "$_od" ] || find "$_od" -mindepth 2 -type f -name '*.tsv' | while IFS= read -r _xf; do printf '%s\t%s\n' "${_xf#"$_od"/}" "$_xf"; done >> "$T/regmap"
  _ex=""; [ "$OVHOME" = 1 ] || [ ! -f "$OVDIR/naming-schemas.tsv" ] || _ex="$OVDIR/naming-schemas.tsv"
  sh "$HERE/standard-register.sh" --root "$ROOT" --schemas "$_sch" ${_ex:+--extra "$_ex"} --map "$T/regmap" facts || {
    log_fail "the register reader failed, so the registers were not judged"; log_fix "sh $HERE/standard-register.sh check"; log_end || :; exit "$UNCHECKED"; }
}

# seam_facts → `G TAB standard.seam-registered TAB <subject> TAB <message>`: contracts/seams.tsv against the tree, both ways (harbor#1425).
# A copy is a tracked MANIFEST.tsv or a top-level vendor/<name>/; it needs a row naming it as the copy_path of a row whose consumer is
# this tree (harbor). A row whose copy is gone, or whose schema this tree owns and does not hold, is red too. Reads $T/regmap, so a
# fixture overlay applies; silent when the tree has no register (a workload carries none).
seam_facts() {
  [ -s "$T/regmap" ] || return 0
  _sm="$(awk -F'\t' '$1 == "contracts/seams.tsv" { f = $2 } END { print f }' "$T/regmap")"
  [ -n "$_sm" ] && [ -r "$_sm" ] || return 0
  awk -F'\t' '
    FNR == NR { if ($0 ~ /^#/ || $1 == "id" || NF < 11) next
      if ($4 == "harbor" && $5 != "-") { nc++; C[nc] = $5; CID[nc] = $1; OPT[nc] = 0
        if (C[nc] ~ /\?$/) { sub(/\?$/, "", C[nc]); OPT[nc] = 1 } }   # a trailing ? is a dev-layer copy the core export does not ship (D-85)
      if ($3 == "harbor" && $7 != "-") { ns++; S[ns] = $7; SID[ns] = $1 }
      next }
    { P[$1] = 1 }
    END {
      for (p in P) {
        if (p ~ /(^|\/)(fixtures|tests?)\// || p ~ /^build\//) continue
        u = ""
        if (p ~ /(^|\/)MANIFEST\.tsv$/) { u = p; sub(/MANIFEST\.tsv$/, "", u) }
        else if (p ~ /^vendor\/[^\/]+\//) { split(p, a, "/"); u = "vendor/" a[2] "/" }
        if (u == "" || (u in SEEN)) continue
        SEEN[u] = 1; ok = 0
        for (i = 1; i <= nc; i++) if (C[i] == u || (C[i] ~ /\/$/ && index(u, C[i]) == 1)) ok = 1
        if (!ok) printf "G\tstandard.seam-registered\tcopy:%s\t%s is a vendored copy with no row in contracts/seams.tsv: add a row naming it as copy_path (harbor#1425)\n", u, u
      }
      for (i = 1; i <= nc; i++) {
        found = 0
        for (p in P) if (p == C[i] || (C[i] ~ /\/$/ && index(p, C[i]) == 1)) { found = 1; break }
        if (!found && !OPT[i]) printf "G\tstandard.seam-registered\trow:%s\tcontracts/seams.tsv row %s names the copy %s, which is not in the tree: delete the row or restore the copy\n", CID[i], CID[i], C[i]
      }
      for (i = 1; i <= ns; i++) if (!(S[i] in P)) printf "G\tstandard.seam-registered\tschema:%s\tcontracts/seams.tsv row %s names the schema %s, which this tree owns and does not hold: the producer ships its schema\n", SID[i], SID[i], S[i]
    }' "$_sm" "$T/regmap"
}

# version_facts → `G TAB standard.version-registered TAB <subject> TAB <message>`: every action a workflow of the judged tree uses
# must be a row of the versions register (axis action, name owner/repo[/path]); a pin the register does not name is a pin held in
# one place only (harbor#1426). Reads the workflows through $T/regmap, so a fixture applies; silent where the registry carries no
# versions.tsv, and for a local or docker ref, which is not a pin.
version_facts() {
  [ -s "$T/regmap" ] || return 0
  _vf="$(reg_file versions.tsv)"; [ -r "$_vf" ] || return 0
  awk -F'\t' '/^#/ || $1 == "id" { next } $2 == "action" { print $3 }' "$_vf" | sort -u > "$T/vnames"
  awk -F'\t' '$1 ~ /^\.github\/workflows\/[^\/]+\.ya?ml$/ { print $1 "\t" $2 }' "$T/regmap" | sort -u > "$T/vwf"
  # a fixture overlay carries its files as workflows/*.yml, so no fixture sits in a real workflow folder (the overlay maps only .tsv files)
  [ -z "${_od:-}" ] || find "$_od/workflows" -maxdepth 1 -type f -name '*.yml' 2>/dev/null | while IFS= read -r _wf; do printf '.github/workflows/%s\t%s\n' "${_wf##*/}" "$_wf"; done >> "$T/vwf"
  [ -s "$T/vwf" ] || return 0
  # a composite action this tree holds (.github/actions/<name>/action.yml) is the repo's own, not a third-party pin (harbor#967)
  awk -F'\t' '$1 ~ /^\.github\/actions\/[^\/]+\/action\.ya?ml$/ { n = $1; sub(/^\.github\/actions\//, "", n); sub(/\/.*$/, "", n); print n }' "$T/regmap" | sort -u > "$T/vown"
  while IFS='	' read -r _vp _vr; do
    awk -v f="$_vp" -v own="$T/vown" 'BEGIN { while ((getline x < own) > 0) O[x] = 1 }
      /^[[:space:]]*(-[[:space:]]+)?uses:[[:space:]]/ {
        l = $0; sub(/^[[:space:]]*(-[[:space:]]+)?uses:[[:space:]]*/, "", l); sub(/[[:space:]]+#.*$/, "", l); gsub(/["\047]/, "", l)
        if (l ~ /^\.\// || l ~ /^docker:\/\//) next
        if (l ~ /^[^\/@]+\/[^\/@]+\/\.github\/actions\/[^\/@]+(@|$)/) { nm = l; sub(/^[^\/@]+\/[^\/@]+\/\.github\/actions\//, "", nm); sub(/@.*$/, "", nm); if (nm in O) next }
        n = index(l, "@"); if (n > 0) l = substr(l, 1, n - 1)
        print l "\t" f }' "$_vr"
  done < "$T/vwf" | sort -u | awk -F'\t' 'NR == FNR { k[$1] = 1; next } !($1 in k) { seen[$1] = (($1 in seen) ? seen[$1] ", " : "") $2 }
      END { for (a in seen) printf "G\tstandard.version-registered\taction:%s\t%s is pinned in %s and has no row in standards/naming/versions.tsv: register the action once, for every repo (harbor#1426)\n", a, a, seen[a] }' "$T/vnames" -
}

# inventory_facts → `G TAB <rule> TAB <subject> TAB <message>`: contracts/env.tsv and contracts/bin.tsv against the tree, both ways
# (standard.env-registered, standard.script-registered; harbor#1431, #1432). Reads $T/regmap, so a fixture overlay applies; silent
# where the tree carries neither register or the vendored copy has no standard-inventory.sh beside it.
inventory_facts() {
  [ -f "$HERE/standard-inventory.sh" ] && [ -s "$T/regmap" ] || return 0
  # a fixture overlay that carries neither register leaves the tree's verdict as the plain run gives it: it is not judged again
  [ -z "${_od:-}" ] || [ -f "$_od/contracts/env.tsv" ] || [ -f "$_od/contracts/bin.tsv" ] || [ -f "$_od/contracts/roots.tsv" ] || return 0
  sh "$HERE/standard-inventory.sh" --root "$ROOT" --map "$T/regmap" facts || {
    log_fail "the inventory reader failed, so the env and bin registers were not judged"; log_fix "sh $HERE/standard-inventory.sh facts"; log_end || :; exit "$UNCHECKED"; }
}

case "$ACTION" in
  list)
    awk -F'\t' '/^#/ || !H[FILENAME]++ || NF < 2 { next } $3 == "lint" && $4 == "bin/lint-name.sh" { printf "  %-26s %-9s %s\n", $1, $2, $NF }' "$(reg_file controls.tsv)"
    # The other lint's ids share the namespace (D-10), so they are listed here too; lint
    # lint.id-one-meaning fails a tree where one id is in both. Its mode is in its title.
    lc_rules | awk -F'\t' '{ printf "  %-26s %-9s %s\n", $1, (index($2, "(advisory)") || index($2, "(warn)") ? "warn" : "block"), $2 }'
    exit 0 ;;

  check)
    case "$SURFACE" in registry) ;; *)
      awk -F'\t' -v s="$SURFACE" '/^#/ || !H[FILENAME]++ { next } $1 == s && ($4 == "tree" || $4 == "check") { f = 1 } END { exit !f }' "$(reg_file surfaces.tsv)" \
        || { log_fail "surface $SURFACE is not checkable (standards/naming/surfaces.tsv, checked = tree or check)"; log_fix "sh $SELF --list"; log_end || :; exit 2; } ;;
    esac
    printf 'C\t%s\t%s\n' "$SURFACE" "$NAME" > "$T/rec"
    [ "$SURFACE" != repo-kind ] || rk_facts "$NAME" > "$T/rec"
    if [ "$SURFACE" = registry ]; then
      list_tree | awk -F'\t' '{ print "X\t" $2 }' > "$T/rec"
      census >> "$T/rec"
      list_tree > "$T/ltree"; register_facts "$T/ltree" >> "$T/rec"; seam_facts >> "$T/rec"; version_facts >> "$T/rec"; inventory_facts >> "$T/rec"
      judge registry "$T/rec" > "$T/out" || unchecked
    else
      judge check "$T/rec" > "$T/out" || unchecked
    fi
    log_head "check $SURFACE $NAME"
    _rc=0; report "$T/out" "$SURFACE $NAME" || _rc=1
    log_end || :; exit "$_rc" ;;

  parse)
    printf 'P\t%s\t%s\n' "$SURFACE" "$NAME" > "$T/rec"
    judge grammar "$T/rec" > "$T/out" || unchecked
    _p="$(awk -F'\t' '$1 == "P"' "$T/out")"
    case "$_p" in
      P"	"ok"	"*) printf '%s\n' "$_p" | cut -f3- | tr ' ' '\n'; exit 0 ;;
      *) log_head "parse $SURFACE $NAME"; log_fail "${_p#P	err	}"; log_fix "sh $SELF --render $SURFACE <slot>=<value>..."; log_end || :; exit 1 ;;
    esac ;;

  render)
    printf 'R\t%s' "$SURFACE" > "$T/rec"
    for _kv in $SLOTS; do printf '\t%s' "$_kv" >> "$T/rec"; done
    printf '\n' >> "$T/rec"
    judge grammar "$T/rec" > "$T/out" || unchecked
    _r="$(awk -F'\t' '$1 == "R"' "$T/out")"
    case "$_r" in
      R"	"ok"	"*) printf '%s\n' "$_r" | cut -f3-; exit 0 ;;
      *) log_head "render $SURFACE"; log_fail "${_r#R	err	}"; log_fix "sh $SELF --render $SURFACE family=<family> object=<object>"; log_end || :; exit 1 ;;
    esac ;;

  tree|changed|added)
    if [ "$ACTION" = changed ]; then
      list_changed > "$T/files" || { log_fail "no base to compare against ($BASE): --changed judged NOTHING"; log_fix "sh $SELF --base <ref> --changed"; log_end || :; exit "$UNCHECKED"; }
      _what="the names changed since $BASE"
    elif [ "$ACTION" = added ]; then
      # bin/lint-conventions.sh (standard.names-added) tells this FAIL from the other exit-3 causes by its wording.
      list_added > "$T/files" || { log_fail "no base to compare against ($BASE): --added judged NOTHING"; log_fix "sh $SELF --base <ref> --added"; log_end || :; exit "$UNCHECKED"; }
      _what="the names added since $BASE"
    else
      list_tree > "$T/files"; _what="every tracked name"
    fi
    awk -F'\t' '{ print "F\t" $2 "\t" $1 }' "$T/files" > "$T/rec"
    # A token read out of an added file is not a name the branch added: the file may only use one that was there
    # already, so --added leaves the env tokens to the tree run, and judges the paths.
    [ "$ACTION" = added ] || env_tokens "$T/files" | awk '{ print "E\t" $0 }' >> "$T/rec"
    # bin/lint-conventions.sh's rule ids are names on the rule-id surface too (harbor#957).
    [ "$ACTION" != tree ] || [ "$FOREIGN" = 1 ] || lc_rules | awk -F'\t' '{ print "C\trule-id\t" $1 }' >> "$T/rec"
    [ "$ACTION" != tree ] || [ "$FOREIGN" = 1 ] || census >> "$T/rec"
    # A workload's own registers are judged too: its undeclared .tsv is red there as here (harbor#1424 item 7).
    [ "$ACTION" != tree ] || register_facts "$T/files" >> "$T/rec"
    [ "$ACTION" != tree ] || seam_facts >> "$T/rec"
    [ "$ACTION" != tree ] || version_facts >> "$T/rec"
    [ "$ACTION" != tree ] || inventory_facts >> "$T/rec"
    log_head "$_what in ${ROOT##*/}"
    judge "$ACTION" "$T/rec" > "$T/out" || unchecked
    _rc=0; report "$T/out" "$_what" || _rc=1
    log_end || :; exit "$_rc" ;;
esac

# ── --gen: views generated from the registry, never committed (D-40) ────────
gen_header() { printf '%s\n' "<!-- generated by bin/lint-name.sh --gen $1 from $REL_REG/ — do not edit -->"; }
gen() { # <view> -> writes under build/, prints the path
  _out="$ROOT/build/naming"; mkdir -p "$_out"
  case "$1" in
    glossary)
      { gen_header glossary
        printf '%s\n\n' '# Glossary'
        printf '%s\n' '| Term | Class | Means | Broader | VSM | Also seen as |' '|---|---|---|---|---|---|'
        awk -F'\t' '
          FILENAME ~ /aliases\.tsv$/ { if ($0 ~ /^#/ || !H[FILENAME]++) next
            k = ($3 == "codename") ? "codename" : ($3 == "legacy" ? "legacy, retires " $5 : $3)
            AL[$2] = AL[$2] (AL[$2] == "" ? "" : " · ") $1 " *(" k ")*"; next }
          /^#/ || !H[FILENAME]++ { next }
          { id[++n] = $1; cls[$1] = $2; pref[$1] = $3; br[$1] = $4; vsm[$1] = $5; alt[$1] = $9; def[$1] = $11 }
          END {
            for (i = 1; i <= n; i++) {
              t = id[i]; path = ""; b = br[t]
              # joined, never cut: substr() counts bytes in one awk and characters in another (harbor#950)
              for (d = 0; b != "-" && b != "" && d < 8; d++) { path = (path == "" ? b : path " → " b); b = br[b] }
              also = AL[t]; if (alt[t] != "-" && alt[t] != "") also = also (also == "" ? "" : " · ") alt[t]
              gsub(/\|/, "/", also); x = def[t]; gsub(/\|/, "/", x)
              printf "| **%s** (%s) | %s | %s | %s | %s | %s |\n", t, pref[t], cls[t], x, (path == "" ? "—" : path), vsm[t], (also == "" ? "—" : also)
            }
          }' "$(reg_file aliases.tsv)" "$(reg_file terms.tsv)"
      } > "$_out/glossary.md"; printf '%s\n' "$_out/glossary.md" ;;
    naming-guide)
      { gen_header naming-guide
        printf '%s\n\n' '# Naming guide'
        printf '%s\n' '| Surface | Renderer | Family first | Checked | Example | Rule | View | Note |' '|---|---|---|---|---|---|---|---|'
        awk -F'\t' '/^#/ || !H[FILENAME]++ { next } { printf "| `%s` | %s | %s | %s | `%s` | %s | %s | %s |\n", $1, $2, $3, $4, $5, $6, $7, $8 }' "$(reg_file surfaces.tsv)"
        printf '\n%s\n\n' '## Rules'
        printf '%s\n' '| Rule | Level | Surfaces | Title |' '|---|---|---|---|'
        awk -F'\t' '/^#/ || !H[FILENAME]++ { next } $3 == "lint" && $4 == "bin/lint-name.sh" { printf "| `%s` | %s | %s | %s |\n", $1, $2, $5, $NF }' "$(reg_file controls.tsv)"
      } > "$_out/naming-guide.md"; printf '%s\n' "$_out/naming-guide.md" ;;
    cli-tree)
      # D-13: <system> <family> [<object>] <verb>, parsed back from bin/ by the registry. A script
      # whose first word is not a family is not in the tree yet; it is a name.family finding.
      list_tree | awk -F'\t' '$2 ~ /^bin\/[^\/]+\.[a-z]+$/ { print $2 }' > "$T/bin"
      awk -F'\t' -v sys="harbor" '
        FILENAME ~ /terms\.tsv$/ { if ($0 ~ /^#/ || !H[FILENAME]++) next
          n = split($2, c, ","); for (i = 1; i <= n; i++) { if (c[i] == "family") { F[$1] = 1; fam[++nf] = $1 } if (c[i] == "verb") V[$1] = 1 }; next }
        { p = $0; sub(/^bin\//, "", p); sub(/\.[^.]*$/, "", p); k = split(p, w, "-")
          if (!(w[1] in F)) next
          verb = (k > 1 && (w[k] in V)) ? w[k] : ""; obj = ""
          for (i = 2; i <= k - (verb != ""); i++) obj = obj (obj == "" ? "" : "-") w[i]
          line[w[1]] = line[w[1]] sprintf("    %s %s%s%s    <- %s\n", sys, w[1], (obj == "" ? "" : " " obj), (verb == "" ? "" : " " verb), $0) }
        END { print "# generated by bin/lint-name.sh --gen cli-tree from standards/naming/terms.tsv and bin/ — do not edit"
              for (i = 1; i <= nf; i++) { printf "%s %s\n", sys, fam[i]; printf "%s", (fam[i] in line) ? line[fam[i]] : "    (no command yet)\n" } }
      ' "$(reg_file terms.tsv)" "$T/bin" > "$_out/cli-tree.txt"
      # harbor#1436: the registered systems are the dispatch table, so the tree lists what `harbor <system> <verb>` runs
      if [ -f "$ROOT/contracts/systems.tsv" ]; then
        { printf '# the registered systems, dispatched by contracts/systems.tsv through bin/scope-system.sh\n'
          awk -F'\t' '/^#/ || !H++ { next } NF >= 5 { printf "harbor %s %s    <- %s\n", $1, ($2 == "*" ? "<verb>" : $2), $3 }' "$ROOT/contracts/systems.tsv"
        } >> "$_out/cli-tree.txt"
      fi
      printf '%s\n' "$_out/cli-tree.txt" ;;
    labels)
      # D-30, harbor#1427: every label is <axis>:<value>, and this is THE list a sync or a scaffolder creates from: the
      # family labels terms.tsv derives, then every other label row of label-axes.tsv (the explicit meaning wins nothing
      # over a family's; a name is listed once). The axis rows are not labels. Descriptions are cut to GitHub's 100.
      awk -F'\t' 'BEGIN { print "# generated by bin/lint-name.sh --gen labels from standards/naming/terms.tsv and label-axes.tsv — do not edit"; print "name\tdescription" }
        FILENAME == ARGV[1] { if ($0 ~ /^#/ || !H[FILENAME]++) next
          if ($2 ~ /(^|,)family(,|$)/) { d = $11; sub(/^Family: /, "", d); seen["family:" $1] = 1; print "family:" $1 "\t" substr(d, 1, 100) }
          next }
        /^#/ || !G[FILENAME]++ { next } $3 == "-" || ($1 in seen) { next }
        { print $1 "\t" substr($9, 1, 100) }' \
        "$(reg_file terms.tsv)" "$(reg_file label-axes.tsv)" > "$_out/labels.tsv"; printf '%s\n' "$_out/labels.tsv" ;;
    vale)
      _v="$ROOT/build/vale"; mkdir -p "$_v/styles/Harbor"
      printf '%s\n' '# generated by bin/lint-name.sh --gen vale from standards/naming/aliases.tsv — do not edit' \
        'StylesPath = styles' 'MinAlertLevel = suggestion' '' '[*.md]' 'BasedOnStyles = Harbor' > "$_v/.vale.ini"
      awk -F'\t' -v k=legacy -v ext=substitution -v msg="Use %s instead of %s (standards/naming/aliases.tsv, legacy)" '
        FILENAME ~ /terms\.tsv$/ { if ($0 !~ /^#/ && H[FILENAME]++) P[$1] = $3; next }
        BEGIN { print "# generated by bin/lint-name.sh --gen vale — do not edit"; print "extends: " ext; print "message: \"" msg "\""; print "level: warning"; print "ignorecase: true"; print "swap:" }
        /^#/ || !H[FILENAME]++ { next } $3 == k { gsub(/-/, "[- ]", $1); printf "  \047%s\047: %s\n", $1, (($2 in P) ? P[$2] : $2) }' \
        "$(reg_file terms.tsv)" "$(reg_file aliases.tsv)" > "$_v/styles/Harbor/Legacy.yml"
      awk -F'\t' -v ext=substitution -v msg="Spell out %s: %s is not a registered abbreviation (D-18)" '
        BEGIN { print "# generated by bin/lint-name.sh --gen vale — do not edit"; print "extends: " ext; print "message: \"" msg "\""; print "level: suggestion"; print "ignorecase: false"; print "swap:" }
        /^#/ || !H[FILENAME]++ { next } $3 == "abbreviation" { printf "  \047\\b%s\\b\047: %s\n", $1, $2 }' "$(reg_file aliases.tsv)" > "$_v/styles/Harbor/Abbreviations.yml"
      awk -F'\t' '
        BEGIN { print "# generated by bin/lint-name.sh --gen vale — do not edit"; print "extends: existence"; print "message: \"%s is banned in the standard: see standards/naming/aliases.tsv\""; print "level: error"; print "ignorecase: true"; print "tokens:" }
        /^#/ || !H[FILENAME]++ { next } $3 == "banned" { gsub(/-/, "[- ]", $1); printf "  - \047\\b%s\\b\047\n", $1 }' "$(reg_file aliases.tsv)" > "$_v/styles/Harbor/Banned.yml"
      awk -F'\t' -v ext=substitution -v msg="Use %s instead of %s: the standard is US English (D-16, standards/naming/aliases.tsv)" '
        BEGIN { print "# generated by bin/lint-name.sh --gen vale — do not edit"; print "extends: " ext; print "message: \"" msg "\""; print "level: warning"; print "ignorecase: true"; print "swap:" }
        /^#/ || !H[FILENAME]++ { next } $3 == "spelling" { printf "  \047%s\047: %s\n", $1, $2 }' "$(reg_file aliases.tsv)" > "$_v/styles/Harbor/Spelling.yml"
      printf '%s\n' "$_v/.vale.ini" ;;
    issue-taxonomy)
      # harbor#978: the connector's taxonomy format from label-axes.tsv, so the label list has one source. The
      # legacy-to-new map goes beside it. Neither is committed and nothing on GitHub is renamed by either.
      awk -F'\t' 'BEGIN { print "# generated by bin/lint-name.sh --gen issue-taxonomy from standards/naming/label-axes.tsv — do not edit"
                          print "# FORMAT: kind <TAB> label <TAB> glob|- <TAB> meaning; #AXIS <TAB> axis <TAB> min <TAB> max <TAB> who sets it" }
        /^#/ || !H[FILENAME]++ { next }
        $3 == "-" { printf "#AXIS\t%s\t%s\t%s\t%s\n", $2, $6, $7, $9; next }
        { printf "%s\t%s\t%s\t%s\n", $2, $1, $5, $9 }' "$(reg_file label-axes.tsv)" > "$_out/issue-taxonomy.tsv"
      awk -F'\t' 'BEGIN { print "# generated by bin/lint-name.sh --gen issue-taxonomy from standards/naming/label-axes.tsv — do not edit"
                          print "legacy\tlabel\tstatus" }
        /^#/ || !H[FILENAME]++ { next } $3 != "-" { printf "%s\t%s\t%s\n", $4, $1, $8 }' "$(reg_file label-axes.tsv)" > "$_out/label-map.tsv"
      printf '%s\n' "$_out/issue-taxonomy.tsv" ;;
    vendors)
      # harbor#985: the vendor-token map a dialect checker reads (azimuth's check-naming), so the vendor list has one source.
      # `use` is the current term; a token row is a legacy alias valid inside a dialect until touched (D-54).
      awk -F'\t' 'BEGIN { print "# generated by bin/lint-name.sh --gen vendors from standards/naming/aliases.tsv and terms.tsv — do not edit"
                          print "token\tuse\tstatus\tremove_by" }
        FILENAME == ARGV[1] { if ($0 !~ /^#/ && H1++ && $2 != "-" ) { n = split($4, S, ","); for (i = 1; i <= n; i++) if (S[i] == "dialect") { printf "%s\t%s\tlegacy\t%s\n", $1, $2, $5; U[$2] = 1 } } next }
        $0 !~ /^#/ && H2++ && ($1 in U) { printf "%s\t%s\tcurrent\t-\n", $1, $1 }' "$(reg_file aliases.tsv)" "$(reg_file terms.tsv)" > "$_out/vendors.tsv"
      printf '%s\n' "$_out/vendors.tsv" ;;
    exemptions)
      # The first scan (harbor#950): one legacy row per finding the rules make on this tree today,
      # judged with no legacy row applied. A person reviews and commits the rows; after that the
      # list only shrinks (name.exemption-grow, name.exemption-stale).
      # remove_by is the rename wave that retires the row (harbor#961: W1 docs, W2 tests, W3 bin, W4
      # PATH, W5 state, env and launchd, W6 admission; skills follow D-35). One date for every row
      # would expire the whole list on one day. Eval data renames one experiment at a time (D-56
      # evals/<family>/<experiment>/), so each experiment is its own batch. Batches take the dates
      # below in order, a week apart; the reason names the batch.
      list_tree > "$T/files"
      awk -F'\t' '{ print "F\t" $2 "\t" $1 }' "$T/files" > "$T/rec"
      env_tokens "$T/files" | awk '{ print "E\t" $0 }' >> "$T/rec"
      judge tree "$T/rec" 1 | awk -F'\t' '$1 == "F" && $2 !~ /^name\.(registry-|exemption-|alias-expired)/ { print $2 "\t" $4 }' | sort -u \
        | awk -F'\t' '
          function batch(s,   a, n) {
            if (s ~ /^env:/) return "5 W5 state, env and launchd"
            n = split(s, a, "/")
            if (a[1] == "evals") return "1 W1 eval data " (n > 2 ? a[2] : "(root)")
            if (a[1] == "tests" || a[1] == "fixtures") return "2 W2 tests"
            if (a[1] == "bin" && n == 2 && a[2] ~ /^(guard-|floor-hook|guard-integrity)/) return "6 W6 admission"
            if (a[1] == "bin" && n == 2 && a[2] !~ /\./) return "4 W4 PATH"
            if (a[1] == "bin") return "3 W3 bin"
            if (a[1] == "launchd" || a[1] == "systemd") return "5 W5 state, env and launchd"
            if (a[1] == "skills" || a[1] == "vendor" || a[1] == "commands") return "7 skills (D-35)"
            return "0 W1 docs"
          }
          BEGIN { split("2026-11-16 2026-11-23 2026-11-30 2026-12-07 2026-12-14 2026-12-21 2026-12-28 2027-01-04 " \
                        "2027-01-11 2027-01-18 2027-01-25 2027-02-01 2027-02-08 2027-02-15 2027-02-22 2027-03-01", DAY, " ") }
          { R[NR] = $0; B[NR] = batch($2); if (!(B[NR] in SEEN)) { SEEN[B[NR]] = 1; K[++nk] = B[NR] } }
          END {
            for (i = 2; i <= nk; i++) for (j = i; j > 1 && K[j - 1] > K[j]; j--) { t = K[j]; K[j] = K[j - 1]; K[j - 1] = t }
            for (i = 1; i <= nk; i++) { D[K[i]] = DAY[i]; L = K[i]; sub(/^[0-9] /, "", L); LAB[K[i]] = L }
            if (nk > 16) { print "lint-name: more rename batches than dates" > "/dev/stderr"; exit 1 }
            for (i = 1; i <= NR; i++) printf "legacy-%04d\t%s\tlegacy\t%s\tfirst scan, harbor#950; %s\n", i, R[i], D[B[i]], LAB[B[i]]
          }' > "$_out/exemptions.legacy.tsv"
      printf '%s\n' "$_out/exemptions.legacy.tsv" ;;
    validators)
      # harbor#1259: build/validators.txt, one `<surface> TAB <ERE>` per grammar-backed surface, derived here and never by hand.
      validators_to "$ROOT/build/validators.txt" || return 1
      printf '%s\n' "$ROOT/build/validators.txt" ;;
    *) return 2 ;;
  esac
}
# The derivation (harbor#1259): the judge renders each grammar, so a grammar cell that cannot be compiled is refused here.
validators_to() { # <file> -> writes the validators; nonzero when a grammar cannot be rendered
  awk -F'\t' '/^#/ || !H[FILENAME]++ { next } $6 != "-" && $6 != "" { print "V\t" $1 }' "$(reg_file surfaces.tsv)" > "$T/vrec"
  # Core registers only: the private, pack and repo overlays would make build/validators.txt differ between boxes and
  # repos, and a validator is the shape the public standard gives. A private system prefix is then a vocabulary question
  # for the lint, and a repo family never reaches a view (harbor#967).
  _vp="$PRIV"; _vk2="$PACKT"; _vow="$OVW"; _vof="$OVF"; PRIV=""; PACKT=""; OVW=""; OVF=""
  judge validators "$T/vrec" > "$T/vout" || unchecked
  PRIV="$_vp"; PACKT="$_vk2"; OVW="$_vow"; OVF="$_vof"
  _vbad=0
  while IFS='	' read -r _vk _vs _vst _vwhy; do
    [ "$_vk" = V ] || continue
    [ "$_vst" = ok ] || { log_fail "--gen validators: $_vs: $_vwhy"; log_fix "fix the grammar cell of $_vs in $REL_REG/surfaces.tsv"; _vbad=1; }
  done < "$T/vout"
  [ "$_vbad" -eq 0 ] || return 1
  mkdir -p "$(dirname "$1")"
  { printf '%s\n' "# generated by bin/lint-name.sh --gen validators from $REL_REG/surfaces.tsv and terms.tsv — do not edit"
    awk -F'\t' '$1 == "V" && $3 == "ok" { print $2 "\t" $4 }' "$T/vout"
  } > "$1.new" && mv "$1.new" "$1"
}

if [ "$ACTION" = gen ]; then
  _rc=0; gen "$VIEW" || _rc=$?
  if [ "$_rc" -eq 2 ]; then log_fail "unknown view: $VIEW"; log_fix "sh $SELF --gen glossary|naming-guide|cli-tree|labels|vale|exemptions|issue-taxonomy|vendors|validators"; log_end || :; exit 2; fi
  if [ "$_rc" -ne 0 ]; then log_end || :; exit 1; fi
  log_ok "wrote --gen $VIEW under ${ROOT##*/}/build/"; log_end || :; exit 0
fi

# ── --self-test: every rule goes red on a fail fixture, read from LINT_REDLOG ─
# Whether a rule went red is read from the red log each --check writes, never from the fixture
# text: the expect.tsv names what MUST go red, and only the run says whether it did.
FX="$ROOT/fixtures/naming"
FIXTURE_DAY=2026-10-04   # every fixture is judged on one fixed day, so an alias date cannot redden a pass row
[ -d "$FX" ] || { log_fail "no fixtures at fixtures/naming — the self-test judged NOTHING"; log_fix "ls $ROOT/fixtures"; log_end || :; exit "$UNCHECKED"; }
: > "$T/allred"; _i=0
# A repo-kind fixture is a manifest, one entry per line: `README.md`, `docs/` for a directory, and `<file> TAB <text>`
# for a file with content. It is built into a real directory before --check reads it, so the check meets the same
# thing a person gives it, and the fixtures never plant a nested repo marker (.agents/config.yaml) in this tree.
rk_build() { # <manifest> <dir>
  mkdir -p "$2"
  while IFS='	' read -r _e _c; do
    case "$_e" in ''|'#'*) continue ;; */) mkdir -p "$2/$_e" ;; *) mkdir -p "$2/$(dirname "$_e")"; printf '%b\n' "$_c" > "$2/$_e" ;; esac
  done < "$1"
}
one() { # <surface> <name> -> the red log of one --check, in $T/red
  _i=$((_i + 1)); : > "$T/red"
  _tn="$2"
  if [ "$1" = repo-kind ] && [ -f "$ROOT/$2" ]; then _tn="$T/rk$_i"; rk_build "$ROOT/$2" "$_tn"; fi
  _crc=0; LINT_REDLOG="$T/red" sh "$SELF" --check "$1" "$_tn" --root "$ROOT" --registry "$REG" --today "$FIXTURE_DAY" < /dev/null > /dev/null 2>&1 || _crc=$?
  sort -u "$T/red" -o "$T/red"; awk -v s="$1" '{ print s "\t" $0 }' "$T/red" >> "$T/allred"
  [ -z "${LINT_REDLOG:-}" ] || cat "$T/red" >> "$LINT_REDLOG" 2>/dev/null || :
  [ "$_crc" -le 1 ] || { log_fail "$1 $2: --check exited $_crc, so it judged nothing"; log_fix "sh $SELF --check $1 '$2'"; return 1; }
  return 0
}
log_head "self-test: fixtures/naming"
# --rule: the rules the run is limited to, and the surfaces they declare (their pass fixtures are judged for them alone).
if [ -n "$RULES" ]; then
  awk -F'\t' '/^#/ || !H[FILENAME]++ { next } $3 == "lint" && $4 == "bin/lint-name.sh" { print $1 }' "$(reg_file controls.tsv)" > "$T/rules0"
  for _r in $RULES; do
    grep -qx -- "$_r" "$T/rules0" || { log_fail "--rule $_r is not a lint-name rule in controls.tsv"; log_fix "sh $SELF --list"; log_end || :; exit 2; }
  done
  awk -F'\t' -v sel=" $RULES" '/^#/ || !H[FILENAME]++ { next } index(sel, " " $1 " ") && $5 != "-" { n = split($5, S, ","); for (i = 1; i <= n; i++) print S[i] }' \
    "$(reg_file controls.tsv)" | sort -u > "$T/selsurf"
  log_note "limited to --rule ${RULES% }: the other rules, the examples and the pass fixtures of other surfaces are not judged"
fi
reds() { if [ -n "$RULES" ]; then awk -v r=" $RULES" 'index(r, " " $0 " ")' "$T/red"; else cat "$T/red"; fi; }
find "$FX" -mindepth 3 -maxdepth 3 -name expect.tsv | sort > "$T/expects"
[ -s "$T/expects" ] || { log_fail "no fixtures/naming/<surface>/{pass,fail}/expect.tsv — nothing to judge"; log_fix "ls $FX"; log_end || :; exit "$UNCHECKED"; }
while IFS= read -r _e; do
  _side="$(basename "$(dirname "$_e")")"; _sf="$(basename "$(dirname "$(dirname "$_e")")")"
  _rows=0
  while IFS='	' read -r _nm _want _rest; do
    case "$_nm" in ''|'#'*|name) continue ;; esac
    _rows=$((_rows + 1))
    if [ -n "$RULES" ]; then
      if [ "$_side" = fail ]; then
        _want="$(printf '%s' "$_want" | tr ',' '\n' | awk -v r=" $RULES" 'index(r, " " $0 " ")' | tr '\n' ',')"; _want="${_want%,}"
        [ -n "$_want" ] || continue
      else grep -qx -- "$_sf" "$T/selsurf" || continue; fi
    fi
    one "$_sf" "$_nm" || continue
    if [ "$_side" = fail ]; then
      for _w in $(printf '%s' "$_want" | tr ',' ' '); do
        if grep -qx -- "$_w" "$T/red"; then log_ok "$_sf/fail: $_nm went red on $_w"
        else log_fail "$_sf/fail: $_nm did not go red on $_w (red: $(tr '\n' ' ' < "$T/red"))"; log_fix "sh $SELF --check $_sf '$_nm'"; fi
      done
    elif [ -n "$(reds)" ]; then log_fail "$_sf/pass: $_nm went red on $(tr '\n' ' ' < "$T/red")"; log_fix "sh $SELF --check $_sf '$_nm'"
    else log_ok "$_sf/pass: $_nm stays green"; fi
  done < "$_e"
  [ "$_rows" -gt 0 ] || { log_fail "${_e#"$ROOT"/} holds no row"; log_fix "add a row: <name> TAB <rule>"; }
done < "$T/expects"

# The doctest: every example the registry and the standard give must pass (surfaces.tsv §6.1, and
# each table row of standards/naming.md whose first two cells are a checkable surface and a name in backticks).
awk -F'\t' '/^#/ || !H[FILENAME]++ { next } ($4 == "tree" || $4 == "check") && $5 != "-" { print $1 "\t" $5 }' "$(reg_file surfaces.tsv)" > "$T/doc"
[ ! -f "$ROOT/standards/naming.md" ] || awk -F'\t' 'NR == FNR { if ($0 !~ /^#/ && H[FILENAME]++ && ($4 == "tree" || $4 == "check")) s[$1] = 1; next }
  /^ *\| `[a-z-]+` \| `[^`]+`/ { n = split($0, c, "|"); a = c[2]; gsub(/[ `]/, "", a); b = c[3]; sub(/^[^`]*`/, "", b); sub(/`.*$/, "", b); if (a in s) print a "\t" b }' \
  "$(reg_file surfaces.tsv)" "$ROOT/standards/naming.md" >> "$T/doc"
[ -z "$RULES" ] || : > "$T/doc"   # the examples are the whole tree's, not a rule's
while IFS='	' read -r _sf _nm; do
  one "$_sf" "$_nm" || continue
  if [ -s "$T/red" ]; then log_fail "example $_sf $_nm went red on $(tr '\n' ' ' < "$T/red")"; log_fix "fix the example or the rule: sh $SELF --check $_sf '$_nm'"
  else log_ok "example $_sf $_nm stays green"; fi
done < "$T/doc"

# The grammar round-trips: each surface whose renderer is a grammar parses its example, renders the slots
# back to the same name, and parses that again to the same slots. A renderer that cannot reproduce its own
# example is a grammar the lint and a writer would read two ways (09a S6).
awk -F'\t' '/^#/ || !H[FILENAME]++ { next } $6 != "-" && $6 != "" { print "T\t" $1 }' "$(reg_file surfaces.tsv)" > "$T/trec"
if [ -s "$T/trec" ]; then
  judge grammar "$T/trec" > "$T/tout" || unchecked
  _gn=0
  while IFS='	' read -r _k _sid _st _why; do
    [ "$_k" = T ] || continue
    _gn=$((_gn + 1))
    if [ "$_st" = ok ]; then log_ok "grammar $_sid: parse then render gives the example back"
    else log_fail "grammar $_sid: $_why"; log_fix "sh $SELF --parse $_sid '<example>'"; fi
  done < "$T/tout"
  [ "$_gn" -gt 0 ] || { log_fail "the grammar round-trip judged no surface"; log_fix "sh $SELF --parse bin admission-bash.sh"; }
fi

# The file list is registers.tsv's (harbor#950), so a standards/naming/*.tsv it does not name is a register the
# lint never read. This holds in the self-test itself, whatever the fixtures cover: plant one, red; remove it, green.
if [ -n "$UNLISTED" ]; then
  log_fail "standards/naming holds a file registers.tsv has no row for: ${UNLISTED% }"
  log_fix "add a row per file to $REL_REG/registers.tsv: id, file, select, census, note"
else
  log_ok "every $REL_REG/*.tsv has a registers.tsv row ($(printf '%s\n' $FILES | grep -c .) files)"
fi

# Coverage, per rule AND per surface: every surface a controls.tsv row declares went red on that rule in
# this run. Per rule only, a rule declared on five surfaces passed on one fixture (21 of 57 pairs never
# went red, harbor#950), so a renderer could stop judging four surfaces and nothing noticed.
LC_ALL=C sort -u "$T/allred" -o "$T/allred"
# The rules are the controls this lint evaluates: type lint, engine bin/lint-name.sh. Planned ones (engine -) wait for their evaluator.
awk -F'\t' -v sel=" $RULES" '/^#/ || !H[FILENAME]++ { next } $3 == "lint" && $4 == "bin/lint-name.sh" && (sel == " " || index(sel, " " $1 " ")) { print $1 }' "$(reg_file controls.tsv)" | LC_ALL=C sort -u > "$T/rules"
awk -F'\t' -v sel=" $RULES" '/^#/ || !H[FILENAME]++ { next } $3 == "lint" && $4 == "bin/lint-name.sh" && $5 != "-" && (sel == " " || index(sel, " " $1 " ")) { n = split($5, S, ","); for (i = 1; i <= n; i++) print S[i] "\t" $1 }' "$(reg_file controls.tsv)" \
  | LC_ALL=C sort -u > "$T/pairs"
cut -f2 "$T/allred" | LC_ALL=C sort -u | comm -23 "$T/rules" - > "$T/never"
LC_ALL=C comm -23 "$T/pairs" "$T/allred" | awk -F'\t' '{ printf "%s on %s\n", $2, $1 }' > "$T/nopair"
if [ -s "$T/never" ]; then
  log_fail "never went red in this self-test: $(tr '\n' ' ' < "$T/never")"; log_fix "add a fail fixture under fixtures/naming/<surface>/fail/ for each"
else
  if [ -n "$RULES" ]; then log_ok "every selected rule went red at least once ($(grep -c . "$T/rules" | tr -d ' ') rules; limited to --rule)"
  else log_ok "every rule in controls.tsv went red at least once ($(grep -c . "$T/rules" | tr -d ' ') rules)"; fi
fi
if [ -s "$T/nopair" ]; then
  log_fail "a declared surface never went red: $(awk '{ printf "%s%s", (NR > 1 ? ", " : ""), $0 }' "$T/nopair")"
  log_fix "add a fail fixture under fixtures/naming/<surface>/fail/, or drop the surface from the rule's controls.tsv row"
else
  if [ -n "$RULES" ]; then log_ok "every selected rule went red on every surface it declares ($(grep -c . "$T/pairs" | tr -d ' ') rule×surface pairs; limited to --rule)"
  else log_ok "every rule went red on every surface it declares ($(grep -c . "$T/pairs" | tr -d ' ') rule×surface pairs)"; fi
fi

# The validators (harbor#1259). The derivation runs into $T, so this never reads the box; the file a consumer reads is
# build/validators.txt, and when it exists it must be what the derivation gives and must pass the same checks.
vcheck() { # <validators file> <label> -> every grammar surface has one, its example matches it, and it refuses "" and "a b"
  _vres="$(awk -F'\t' -v label="$2" '
    FILENAME == ARGV[1] { if ($0 !~ /^#/ && NF == 2) { RE[$1] = $2 }; next }
    /^#/ || !H[FILENAME]++ { next }
    $6 != "-" && $6 != "" { G[$1] = 1; n++
      if (!($1 in RE)) { print "fail\tno validator for " $1; next }
      if (!($5 ~ RE[$1])) print "fail\t" $1 " its example " $5 " does not match its validator"
    }
    END {
      for (s in RE) {
        if (!(s in G)) print "fail\ta validator for " s ", which has no grammar cell (prose renders no validator)"
        if (("" ~ RE[s]) || ("a b" ~ RE[s])) print "fail\t" s " accepts a string that is empty or has a space"
      }
      print "count\t" n + 0
    }' "$1" "$(reg_file surfaces.tsv)")"
  _vfail="$(printf '%s\n' "$_vres" | awk -F'\t' '$1 == "fail" { printf "%s; ", $2 }')"
  _vn="$(printf '%s\n' "$_vres" | awk -F'\t' '$1 == "count" { print $2 }')"
  if [ -n "$_vfail" ]; then log_fail "$2: ${_vfail%; }"; log_fix "sh $SELF --gen validators"
  else log_ok "$2: $_vn grammar surfaces, each example matches its validator, none accepts \"\" or \"a b\""; fi
}
if validators_to "$T/validators.txt"; then
  vcheck "$T/validators.txt" "validators derived in this run"
  if [ -f "$ROOT/build/validators.txt" ]; then
    if cmp -s "$T/validators.txt" "$ROOT/build/validators.txt"; then log_ok "build/validators.txt is what --gen validators derives"
    else log_fail "build/validators.txt is not what --gen validators derives (edited by hand, or stale)"; log_fix "sh $SELF --gen validators"; fi
    vcheck "$ROOT/build/validators.txt" "build/validators.txt"
  fi
else log_fail "the validators could not be derived from the grammar cells"; log_fix "sh $SELF --gen validators"; fi
log_end
