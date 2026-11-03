#!/usr/bin/env sh
# doc-shape.sh (harbor#1434, #1435) — every doc carries one front matter shape, every generated file one header.
#
#   sh bin/doc-shape.sh                 judge both halves
#   sh bin/doc-shape.sh --docs          only docs/**/*.md front matter (doc.kind-folder, doc.frontmatter-fields)
#   sh bin/doc-shape.sh --generated     only the generated-by header <-> contracts/fresh-checks.tsv row equality (doc.fresh-registered)
#   sh bin/doc-shape.sh --write         write the missing front matter into docs/**/*.md; idempotent; prints the files whose
#                                       owner fell back to the CODEOWNERS default as `default-owner TAB path`
#   sh bin/doc-shape.sh --list-generated   every file carrying the header, with its command
#
#   --root <dir>        the tree to judge (default: the repo this script sits in, or holds vendor/naming/)
#   --registry <dir>    kinds.tsv and controls.tsv (default <root>/standards/naming, else <root>/vendor/naming/registry)
#   --repo <name>       the repo name a bare ticket number is prefixed with (default: the origin remote's name)
#   --strict            exit 1 on any finding, whatever controls.tsv says (what a fixture proves red with)
#   --format findings   also print one `A|V TAB <id> TAB <path>` record per finding on stdout
#
# THE SHAPE. A doc's front matter carries kind (the kinds.tsv row whose folder holds the file, or a row of
# contracts/doc-folders.tsv), owner (`repo#n`, `KEY-n` or `@login`), status, and source where the kind's required_fields name
# it. A generated file carries `generated-by: <command>` in a comment of its own syntax; the command is the `fix` column of its
# contracts/fresh-checks.tsv row, and the two are held equal both ways. OWNER IS NEVER INVENTED: --write takes it from
# the doc's own owner_issue, a ticket in its title or source, the git history's top human author, then the CODEOWNERS default.
#   exit 0 clean, or only warn findings · 1 a block finding, or --strict · 2 usage · 3 unchecked
set -eu
LC_ALL=C; export LC_ALL
case "$0" in */*) SRC="${0%/*}" ;; *) SRC=. ;; esac
SRC="$(CDPATH='' cd -- "$SRC" && pwd -P)"
SELF="$SRC/$(basename -- "$0")"
# shellcheck disable=SC2034 # LOG_FD: read by library-log.sh
LOG_FD=2
. "$SRC/library-log.sh"
log_init doc-shape

usage() { awk 'NR < 2 { next } /^# THE SHAPE/ || !/^#/ { exit } { sub(/^# ?/, ""); print }' "$SELF"; }
usage_err() { log_fail "$1"; log_fix "sh $SELF --help"; log_end || :; exit 2; }
unchecked() { log_fail "$1"; log_fix "$2"; log_end || :; exit 3; }

ACTION=judge; HALF=all; ROOT=""; REG=""; REPO=""; STRICT=0; FORMAT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --docs) HALF=docs ;;
    --generated) HALF=generated ;;
    --write) ACTION=write ;;
    --list-generated) ACTION=list ;;
    --strict) STRICT=1 ;;
    --root) [ $# -ge 2 ] || usage_err "--root needs a directory"; ROOT="$2"; shift ;;
    --registry) [ $# -ge 2 ] || usage_err "--registry needs a directory"; REG="$2"; shift ;;
    --repo) [ $# -ge 2 ] || usage_err "--repo needs a name"; REPO="$2"; shift ;;
    --format) [ $# -ge 2 ] || usage_err "--format needs a value"; FORMAT="$2"; shift ;;
    -*) usage_err "unknown option $1" ;;
    *) usage_err "unexpected argument $1" ;;
  esac
  shift
done
case "$FORMAT" in ''|findings) ;; *) usage_err "--format is findings" ;; esac
if [ -z "$ROOT" ]; then
  # sitting in vendor/naming/, the repo is two levels up
  ROOT="$SRC/.."; case "$SRC" in */vendor/naming) ROOT="$SRC/../.." ;; esac
fi
ROOT="$(CDPATH='' cd -- "$ROOT" 2>/dev/null && pwd -P)" || usage_err "--root is not a directory"
if [ -z "$REG" ]; then
  if [ -f "$ROOT/standards/naming/kinds.tsv" ]; then REG="$ROOT/standards/naming"; else REG="$ROOT/vendor/naming/registry"; fi
fi
KINDS="$REG/kinds.tsv"; CONTROLS="$REG/controls.tsv"
OVR="$ROOT/contracts/doc-folders.tsv"; FRESH="$ROOT/contracts/fresh-checks.tsv"
T="$(mktemp -d "${TMPDIR:-/tmp}/doc-shape.XXXXXX")"
trap 'rm -rf "$T"' EXIT INT TERM HUP
TAB="$(printf '\t')"
# the header, in each comment syntax: `# generated-by: cmd`, `// generated-by: cmd`, `<!-- generated-by: cmd -->`
HDR_RE='^(#|//|;|--|<!--) ?generated-by: ?[^ ]'

# mode_of <id> → shadow | warn | block (warn when the registry has no row: a vendored copy may predate the control)
mode_of() {
  [ -f "$CONTROLS" ] || { echo warn; return 0; }
  _m="$(awk -F'\t' -v id="$1" '/^#/ { next } $1 == id { m = $2; if (m == "advisory" || m == "inform") m = "warn"; print m; exit }' "$CONTROLS")"
  echo "${_m:-warn}"
}

# ── the docs ─────────────────────────────────────────────────────────────────
# scan_docs → `path TAB expected-kind TAB fm TAB kind TAB owner TAB status TAB source TAB gen TAB owner_issue` per doc.
scan_docs() {
  [ -d "$ROOT/docs" ] || return 0
  (cd "$ROOT" && find docs -type f -name '*.md' | LC_ALL=C sort) > "$T/doclist"
  awk -F'\t' -v ROOT="$ROOT" -v KINDS="$KINDS" -v OVR="$OVR" -v LIST="$T/doclist" -v HDR="$HDR_RE" '
  function trim(s) { sub(/^[ \t\r]+/, "", s); sub(/[ \t\r]+$/, "", s); return s }
  function nz(s) { return (s == "" ? "-" : s) }
  function unq(s) { s = trim(s); if (s ~ /^".*"$/ || s ~ /^\047.*\047$/) s = substr(s, 2, length(s) - 2); return s }
  function expected(p,   i, best, bl, f, n, seg) {
    best = ""; bl = 0
    for (i = 1; i <= nf; i++) {
      f = FOLDER[i]
      if (substr(f, length(f)) == "/") { if (substr(p, 1, length(f)) == f && length(f) > bl) { best = FKIND[i]; bl = length(f) } }
      else if (p == f) { best = FKIND[i]; bl = 9999 }
    }
    if (best == "") { n = split(p, seg, "/"); if (n == 2 && (seg[2] == "README.md" || seg[2] == "index.md")) best = "reference" }
    return best
  }
  BEGIN {
    while ((getline l < KINDS) > 0) {
      if (l ~ /^#/ || l ~ /^[ \t]*$/) continue
      n = split(l, c, "\t")
      if (c[1] == "id" || n < 5) continue
      if (c[2] ~ /^docs\//) { nf++; FOLDER[nf] = c[2]; FKIND[nf] = c[1] }
    }
    close(KINDS)
    while ((getline l < OVR) > 0) {
      if (l ~ /^#/ || l ~ /^[ \t]*$/) continue
      n = split(l, c, "\t")
      if (c[1] == "folder" || n < 2) continue
      nf++; FOLDER[nf] = c[1]; FKIND[nf] = c[2]
    }
    close(OVR)
    while ((getline p < LIST) > 0) {
      fm = 0; kind = owner = status = source = oi = ""; gen = 0; ln = 0; inf = 0; done = 0
      path = ROOT "/" p
      while ((getline line < path) > 0) {
        ln++
        sub(/\r$/, "", line)
        if (line ~ HDR) gen = 1
        if (ln == 1) { if (line == "---") { inf = 1; fm = 1 } ; if (inf) continue }
        if (inf && !done) {
          if (line == "---") { inf = 0; done = 1; continue }
          if (line ~ /^[A-Za-z_][A-Za-z0-9_-]*:/) {
            k = substr(line, 1, index(line, ":") - 1); v = unq(substr(line, index(line, ":") + 1))
            if (k == "kind") kind = v; else if (k == "owner") owner = v; else if (k == "status") status = v
            else if (k == "source") source = v; else if (k == "owner_issue") oi = v
          }
        }
      }
      close(path)
      # an empty field is "-": tab is IFS whitespace for `read`, which would fold two empty fields into none
      printf "%s\t%s\t%d\t%s\t%s\t%s\t%s\t%d\t%s\n", p, nz(expected(p)), fm, nz(kind), nz(owner), nz(status), nz(source), gen, nz(oi)
    }
  }' /dev/null
}

# judge_docs <docs.tsv> → `id TAB path TAB message`
judge_docs() {
  awk -F'\t' -v KINDS="$KINDS" '
  BEGIN {
    while ((getline l < KINDS) > 0) {
      if (l ~ /^#/ || l ~ /^[ \t]*$/) continue
      n = split(l, c, "\t"); if (c[1] == "id" || n < 5) continue
      REQ[c[1]] = " " c[5] " "
    }
    close(KINDS)
    nstat = split("proposed accepted rejected superseded current draft unreviewed archived generated", S, " ")
    for (i = 1; i <= nstat; i++) OK[S[i]] = 1
  }
  {
    for (i = 2; i <= 9; i++) if ($i == "-") $i = ""
    p = $1; ex = $2; fm = $3; kind = $4; owner = $5; status = $6; source = $7; gen = $8
    if (ex == "") { print "doc.kind-folder\t" p "\tno kind is declared for this folder: add a row to contracts/doc-folders.tsv"; next }
    if (!fm) { print "doc.frontmatter-fields\t" p "\tno front matter: needs kind " ex ", owner and status (sh bin/doc-shape.sh --write)"; next }
    if (kind == "") print "doc.frontmatter-fields\t" p "\tfront matter has no kind (this folder holds " ex ")"
    else if (kind != ex) print "doc.kind-folder\t" p "\tkind is " kind " but this folder holds " ex
    if (owner == "") print "doc.frontmatter-fields\t" p "\tfront matter has no owner (a ticket repo#n or KEY-n, or a person @login)"
    else if (owner !~ /^@[A-Za-z0-9][A-Za-z0-9-]*$/ && owner !~ /^[A-Za-z0-9][A-Za-z0-9._-]*#[0-9]+$/ && owner !~ /^[A-Z][A-Z0-9]*-[0-9]+$/)
      print "doc.frontmatter-fields\t" p "\towner " owner " is neither a ticket (repo#n, KEY-n) nor a person (@login)"
    if (status == "") print "doc.frontmatter-fields\t" p "\tfront matter has no status"
    else if (!(status in OK)) print "doc.frontmatter-fields\t" p "\tstatus " status " is not one of: proposed accepted rejected superseded current draft unreviewed archived generated"
    if (index(REQ[ex], " source ") && source == "") print "doc.frontmatter-fields\t" p "\t" ex " needs a source (the command or file it derives from)"
    if (index(REQ[ex], " generated-by ") && !gen) print "doc.frontmatter-fields\t" p "\t" ex " needs a generated-by header (standards/naming.md, document header)"
  }' "$1"
}

# ── the generated header ↔ fresh-checks.tsv ─────────────────────────────────
# scan_headers → `path TAB command` per file carrying the header (first header of a file wins)
scan_headers() {
  : > "$T/hits"
  if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 && [ "$(git -C "$ROOT" rev-parse --show-toplevel 2>/dev/null)" = "$ROOT" ]; then
    git -C "$ROOT" grep -I -n -E "$HDR_RE" -- . ':!fixtures/' ':!tests/' ':!vendor/skills/' ':!vendor/skills-archive/' ':!vendor/archify/' ':!evals/' > "$T/hits" 2>/dev/null || :
  else
    ( cd "$ROOT" && grep -r -I -n -E "$HDR_RE" . --exclude-dir=.git --exclude-dir=fixtures --exclude-dir=tests --exclude-dir=node_modules --exclude-dir=.worktrees 2>/dev/null ) | sed 's|^\./||' > "$T/hits" || :
  fi
  awk -F':' '
    { p = $1; ln = $2; rest = $0; sub(/^[^:]*:[^:]*:/, "", rest)
      if (ln + 0 > 20 || p in seen) next   # a header sits in the first 20 lines: a generator that prints one holds the literal further down
      seen[p] = 1
      c = rest; sub(/^(#|\/\/|;|--|<!--) ?generated-by: ?/, "", c); sub(/[ \t]*-->[ \t]*$/, "", c); sub(/[ \t\r]+$/, "", c)
      printf "%s\t%s\n", p, c }' "$T/hits" | LC_ALL=C sort
}

# judge_generated <headers.tsv> → `id TAB path TAB message`
judge_generated() {
  : > "$T/rows"
  [ ! -f "$FRESH" ] || awk -F'\t' '/^#/ || $1 == "" || $1 == "-" { next } { print $1 "\t" $3 "\t" (($4 == "") ? $1 : $4) }' "$FRESH" > "$T/rows"
  awk -F'\t' -v ROOT="$ROOT" '
  function present(p,   cmd, rc) { cmd = "test -e \"" ROOT "/" p "\""; rc = system(cmd); return rc == 0 }
  FILENAME == HDRS { H[$1] = $2; next }
  { R[$1] = $2; RN[$1] = $3 }
  END {
    for (p in H) {
      if (!(p in R)) print "doc.fresh-registered\t" p "\tcarries generated-by: " H[p] " but contracts/fresh-checks.tsv has no row for it"
      else if (R[p] != H[p]) print "doc.fresh-registered\t" p "\tgenerated-by says [" H[p] "] but its fresh-checks.tsv fix is [" R[p] "]"
    }
    for (p in R) {
      if (p in H) continue
      if (!present(p) && !present(RN[p])) continue
      if (present(p)) print "doc.fresh-registered\t" p "\thas a contracts/fresh-checks.tsv row but no generated-by header"
    }
  }' HDRS="$1" "$1" "$T/rows"
}

# report <findings.tsv> → logs each, records them, returns 1 when the run must fail
report() {
  _bad=0
  LC_ALL=C sort -u "$1" > "$T/f.sorted"
  while IFS="$TAB" read -r _id _p _msg; do
    [ -n "$_id" ] || continue
    _m="$(mode_of "$_id")"
    [ -z "${LINT_REDLOG:-}" ] || printf '%s\n' "$_id" >> "$LINT_REDLOG" 2>/dev/null || :
    [ "$_m" != shadow ] || continue
    if [ "$FORMAT" = findings ]; then printf '%s\t%s\t%s\n' "$( [ "$_m" = block ] && echo V || echo A)" "$_id" "$_p"; fi
    if [ "$_m" = block ] || [ "$STRICT" = 1 ]; then log_fail "[$_id] $_p — $_msg"; log_nofix "contracts/doc-folders.tsv, or sh $SELF --write"; _bad=1
    else log_warn "[$_id] $_p — $_msg"; fi
  done < "$T/f.sorted"
  [ "$_bad" -eq 0 ]
}

if [ "$ACTION" = list ]; then
  scan_headers | awk -F'\t' '{ printf "%s\t%s\n", $1, $2 }'
  exit 0
fi

# ── apply ────────────────────────────────────────────────────────────────────
# default_owner → the CODEOWNERS default: the `*` rule's first owner, else the owner named most often
default_owner() {
  for _c in "$ROOT/.github/CODEOWNERS" "$ROOT/CODEOWNERS" "$ROOT/docs/CODEOWNERS"; do
    [ -f "$_c" ] || continue
    awk '/^#/ || NF < 2 { next }
      $1 == "*" { for (i = 2; i <= NF; i++) if ($i ~ /^@/) { print $i; found = 1; exit } }
      { for (i = 2; i <= NF; i++) if ($i ~ /^@/) n[$i]++ }
      END { if (found) exit; for (o in n) if (n[o] > best || (n[o] == best && o < top)) { best = n[o]; top = o } if (top != "") print top }' "$_c" && return 0
  done
  return 1
}

# the repo name a bare #n belongs to
repo_name() {
  [ -z "$REPO" ] || { printf '%s' "$REPO"; return 0; }
  _u="$(git -C "$ROOT" remote get-url origin 2>/dev/null || true)"
  _u="${_u##*/}"; _u="${_u%.git}"
  printf '%s' "${_u:-$(basename "$ROOT")}"
}

# author_logins → key TAB login, the key a commit author's name or address: a login can be read only off a noreply address,
# so the same person's other addresses are matched through the name they committed under
author_logins() {
  git -C "$ROOT" log --format='%an%x09%ae' 2>/dev/null | LC_ALL=C sort -u | awk -F'\t' '
    $1 ~ /\[bot\]$/ { next }
    { N[NR] = $1; E[NR] = $2; n = NR
      if ($2 ~ /^[0-9]+\+[A-Za-z0-9-]+@users\.noreply\.github\.com$/) { l = $2; sub(/^[0-9]+\+/, "", l); sub(/@.*/, "", l); L[$1] = l } }
    END { for (i = 1; i <= n; i++) if (N[i] in L) { print N[i] "\t" L[N[i]]; print E[i] "\t" L[N[i]] } }' | LC_ALL=C sort -u
}

# git_owner <path> → @login of the commit author with most commits to the path (bots excluded), or nothing
git_owner() {
  git -C "$ROOT" log --follow --format='%an%x09%ae' -- "$1" 2>/dev/null | awk -F'\t' -v MAP="$T/logins" '
    BEGIN { while ((getline l < MAP) > 0) { split(l, c, "\t"); M[c[1]] = c[2] } }
    $1 ~ /\[bot\]$/ || $2 ~ /noreply@anthropic\.com$/ { next }
    { lg = ""
      if ($2 ~ /^[0-9]+\+[A-Za-z0-9-]+@users\.noreply\.github\.com$/) { lg = $2; sub(/^[0-9]+\+/, "", lg); sub(/@.*/, "", lg) }
      else if ($2 in M) lg = M[$2]
      else if ($1 in M) lg = M[$1]
      if (lg != "") n[lg]++ }
    END { for (o in n) if (n[o] > best || (n[o] == best && o < top)) { best = n[o]; top = o } if (top != "") print "@" top }'
}

# ticket_owner <path> <owner_issue> <source> → repo#n or nothing: the doc'"'"'s own words, never a guess
ticket_owner() {
  _oi="$2"; _src="$3"
  case "$_oi" in
    ''|*[!0-9]*) ;;
    *) printf '%s#%s' "$RNAME" "$_oi"; return 0 ;;
  esac
  case "$_oi" in *#[0-9]*) printf '%s' "$_oi"; return 0 ;; esac
  case "$_src" in [A-Za-z]*#[0-9]*) case "$_src" in *[!A-Za-z0-9._#-]*) ;; *) printf '%s' "$_src"; return 0 ;; esac ;; esac
  _title="$(awk 'NR == 1 && $0 == "---" { fm = 1; next } fm && $0 == "---" { fm = 0; next } fm { next } /^# / { print; exit }' "$ROOT/$1")"
  _t="$(printf '%s\n' "$_title" | grep -oE '[A-Za-z0-9._-]+#[0-9]+|\(#[0-9]+\)' | head -n 1 || true)"
  case "$_t" in
    '') ;;
    '(#'*) _t="${_t#(#}"; printf '%s#%s' "$RNAME" "${_t%)}"; return 0 ;;
    *) printf '%s' "$_t"; return 0 ;;
  esac
  return 1
}

# fm_block <kind> <owner> <status> <source> → the lines to add
apply_one() { # <path> <expected> <fm> <kind> <owner> <status> <source> <gen> <oi>
  _p="$1"; _ex="$2"; _fm="$3"; _kd="$4"; _ow="$5"; _st="$6"; _sr="$7"; _oi="$9"
  [ -n "$_ex" ] || { printf 'unmapped\t%s\n' "$_p"; return 0; }
  : > "$T/add"
  [ -n "$_kd" ] || printf 'kind: %s\n' "$_ex" >> "$T/add"
  if [ -z "$_ow" ]; then
    _o=""
    _o="$(ticket_owner "$_p" "$_oi" "$_sr" || true)"
    [ -n "$_o" ] || _o="$(git_owner "$_p" || true)"
    if [ -z "$_o" ]; then _o="$DEFAULT_OWNER"; printf 'default-owner\t%s\n' "$_p"; fi
    printf 'owner: %s\n' "$_o" >> "$T/add"
  fi
  if [ -z "$_st" ]; then
    _s=unreviewed
    case "$_ex" in generated-doc|context-map) _s=generated ;; esac
    case "$_ex:$_p" in decision-record:*/README.md|decision-record:*/index.md) ;; decision-record:*)   # an ADR states its own status in its body
      _b="$(sed -n '1,15p' "$ROOT/$_p" | sed -n 's/^[-*_ ]*[Ss]tatus[*_]*:[*_ ]*\([A-Za-z]*\).*/\1/p' | head -n 1 | tr 'A-Z' 'a-z')"
      case "$_b" in proposed|accepted|rejected|superseded) _s="$_b" ;; esac ;;
    esac
    printf 'status: %s\n' "$_s" >> "$T/add"
  fi
  if [ -z "$_sr" ] && awk -F'\t' -v k="$_ex" '$1 == k && (" " $5 " ") ~ / source / { f = 1 } END { exit !f }' "$KINDS" 2>/dev/null; then
    _f="$(awk -F'\t' -v p="$_p" '$1 == p { print $2; exit }' "$T/rows" 2>/dev/null || true)"
    [ -z "$_f" ] || printf 'source: %s\n' "$_f" >> "$T/add"
  fi
  [ -s "$T/add" ] || return 0
  if [ "$_fm" = 1 ]; then
    awk -v ADD="$T/add" 'NR == 1 { print; next } !done && $0 == "---" { while ((getline l < ADD) > 0) print l; done = 1 } { print }' "$ROOT/$_p" > "$T/new"
  else
    { printf -- '---\n'; cat "$T/add"; printf -- '---\n\n'; cat "$ROOT/$_p"; } > "$T/new"
  fi
  cat "$T/new" > "$ROOT/$_p"
  printf 'wrote\t%s\n' "$_p"
}

if [ "$ACTION" = write ]; then
  [ -f "$KINDS" ] || unchecked "no kinds.tsv at $KINDS, so no folder has a kind" "ls $REG"
  RNAME="$(repo_name)"
  DEFAULT_OWNER="$(default_owner || true)"
  [ -n "$DEFAULT_OWNER" ] || unchecked "no CODEOWNERS file names an owner, so a doc with no derivable owner would get none" "add .github/CODEOWNERS"
  author_logins > "$T/logins"
  scan_docs > "$T/docs.tsv"
  : > "$T/rows"; [ ! -f "$FRESH" ] || awk -F'\t' '/^#/ || $1 == "" || $1 == "-" { next } { print $1 "\t" $3 }' "$FRESH" > "$T/rows"
  while IFS="$TAB" read -r p ex fm kd ow st sr gen oi; do
    [ "$ex" != - ] || ex=""; [ "$kd" != - ] || kd=""; [ "$ow" != - ] || ow=""; [ "$st" != - ] || st=""; [ "$sr" != - ] || sr=""; [ "$oi" != - ] || oi=""
    apply_one "$p" "$ex" "$fm" "$kd" "$ow" "$st" "$sr" "$gen" "$oi"
  done < "$T/docs.tsv" > "$T/applied"
  _w="$(grep -c '^wrote' "$T/applied" || true)"
  grep -E '^(default-owner|unmapped)' "$T/applied" || :
  log_ok "wrote front matter into $_w doc(s); $(grep -c '^default-owner' "$T/applied" || true) took the default owner $DEFAULT_OWNER"
  log_end || :
  exit 0
fi

# ── judge ────────────────────────────────────────────────────────────────────
: > "$T/findings"
if [ "$HALF" = all ] || [ "$HALF" = docs ]; then
  [ -f "$KINDS" ] || unchecked "no kinds.tsv at $KINDS, so no folder has a kind" "ls $REG"
  scan_docs > "$T/docs.tsv"
  judge_docs "$T/docs.tsv" >> "$T/findings"
  _nd="$(grep -c . "$T/docs.tsv" || true)"
fi
if [ "$HALF" = all ] || [ "$HALF" = generated ]; then
  scan_headers > "$T/headers.tsv"
  judge_generated "$T/headers.tsv" >> "$T/findings"
  _ng="$(grep -c . "$T/headers.tsv" || true)"
fi
_rc=0
report "$T/findings" || _rc=1
if [ ! -s "$T/findings" ]; then
  [ -z "${_nd:-}" ] || log_ok "$_nd doc(s) carry the front matter shape"
  [ -z "${_ng:-}" ] || log_ok "${_ng} generated file(s) carry the header, each with its fresh-checks.tsv row"
fi
log_end || :
exit "$_rc"
