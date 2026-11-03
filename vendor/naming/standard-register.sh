#!/usr/bin/env sh
# standard-register.sh (harbor#1424) — the one reader of the .tsv registers, and the check that every one is declared.
#
#   sh bin/standard-register.sh get [--where <col>=<value>]... [--header] <file> <col>...
#                                         the named columns of every row of <file>, tab-separated, found by NAME through
#                                         the file's row in standards/naming/schemas.tsv. A header that is not the
#                                         declared columns, a row with a wrong column count or a repeated key is exit 1
#                                         and no row, never a silent shift of $4 into $5.
#                                         --as <schema path> reads <file> by the row of that path (a test's copy, an override)
#   sh bin/standard-register.sh cols <file>       the declared columns, space-separated (an optional one ends in ?)
#   sh bin/standard-register.sh schema <file>     the file's schema row as <field>=<value> lines
#   sh bin/standard-register.sh check             every register against its schema: the findings, human-readable
#   sh bin/standard-register.sh facts             the same findings as `G TAB <rule> TAB <subject> TAB <message>` records,
#                                                 the form bin/lint-name.sh judges (standard.register-declared and
#                                                 standard.reference-resolves, born warn there)
#   sh bin/standard-register.sh index             the register index, markdown, from the schema table
#   sh bin/standard-register.sh --help
#
#   --root <dir>        the tree whose registers are read (default: this checkout, or the repo a vendored copy sits in)
#   --schemas <file>    the schema table (default: standards/naming/schemas.tsv, or vendor/naming/registry/schemas.tsv)
#   --extra <file>      a repo's own schema rows, same shape (naming-schemas.tsv at its root): a vendored copy cannot
#                       describe a repo's own registers, so the repo declares them beside the vendored table
#   --map <file>        `<path> TAB <file to read>` for every tracked file, so a caller can overlay a fixture; default is
#                       git ls-files (else find) under --root
#
#   exit 0 read or clean · 1 a finding or a mismatch · 2 usage · 3 unchecked (no schema row for the file asked, or no table)
#
# A REGISTER IS EVERY TRACKED *.tsv outside a fixtures/ directory and outside a directory or file named test*. The schema table's
# `id` is a path or a one-star glob; a `columns` cell of =<path> says the file is a copy of that one (a vendored register).
# The rows-with-no-file direction holds in a tree that carries the table itself; elsewhere it holds for --extra rows only.
#
# DELIBERATELY ONE FILE: POSIX sh around one awk program, so the naming package vendors it beside lint-name.sh and a
# repo with no toolchain reads registers by header the way harbor does.
set -eu

case "$0" in */*) HERE="${0%/*}" ;; *) HERE=. ;; esac
HERE="$(CDPATH='' cd -- "$HERE" && pwd)"
SELF="$HERE/${0##*/}"
# shellcheck disable=SC2034 # LOG_FD: read by library-log.sh
LOG_FD=2   # stdout carries the rows, the records and the index; the log goes to stderr
. "$HERE/library-log.sh"
log_init standard-register

usage() { awk 'NR < 2 { next } /^# DELIBERATELY/ || !/^#/ { exit } { sub(/^# ?/, ""); print }' "$SELF"; }
die2() { log_fail "$1"; log_fix "sh $SELF --help"; log_end || :; exit 2; }
die3() { log_fail "$1"; log_fix "$2"; log_end || :; exit 3; }

ROOT=""; SCHEMAS=""; EXTRA=""; MAP=""; CMD=""; HEADER=0; WHERE=""; ASREL=""
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --root) [ $# -ge 2 ] || die2 "--root needs a directory"; ROOT="$2"; shift ;;
    --schemas) [ $# -ge 2 ] || die2 "--schemas needs a file"; SCHEMAS="$2"; shift ;;
    --extra) [ $# -ge 2 ] || die2 "--extra needs a file"; EXTRA="$2"; shift ;;
    --map) [ $# -ge 2 ] || die2 "--map needs a file"; MAP="$2"; shift ;;
    --where) [ $# -ge 2 ] || die2 "--where needs <col>=<value>"; case "$2" in *=*) ;; *) die2 "--where needs <col>=<value>" ;; esac
             WHERE="$WHERE$2
"; shift ;;
    --header) HEADER=1 ;;
    get|cols|schema|check|facts|index) CMD="$1"; shift; break ;;
    -*) die2 "unknown option: $1" ;;
    *) die2 "unknown command: $1" ;;
  esac
  shift
done
[ -n "$CMD" ] || die2 "no command"

# A vendored copy sits in vendor/naming/ beside registry/; harbor's own is in bin/.
if [ -z "$ROOT" ]; then
  if [ -f "$HERE/registry/schemas.tsv" ]; then ROOT="$HERE/../.."; else ROOT="$HERE/.."; fi
fi
ROOT="$(CDPATH='' cd -- "$ROOT" 2>/dev/null && pwd -P)" || die2 "--root is not a directory"
if [ -z "$SCHEMAS" ]; then
  if [ -f "$HERE/registry/schemas.tsv" ]; then SCHEMAS="$HERE/registry/schemas.tsv"; else SCHEMAS="$ROOT/standards/naming/schemas.tsv"; fi
fi
[ -r "$SCHEMAS" ] || die3 "no schema table at $SCHEMAS, so no register can be read by name" "sh $SELF --schemas <standards/naming/schemas.tsv>"
[ -z "$EXTRA" ] || [ -r "$EXTRA" ] || die3 "the repo's schema rows $EXTRA are not readable" "ls -l $EXTRA"

T="$(mktemp -d "${TMPDIR:-/tmp}/standard-register.XXXXXX")"
trap 'rm -rf "$T"' EXIT INT TERM HUP

ARG1=""; ARGS=""
if [ "$CMD" = get ]; then   # --where and --header may follow the verb
  while [ $# -gt 0 ]; do
    case "$1" in
      --where) [ $# -ge 2 ] || die2 "--where needs <col>=<value>"; case "$2" in *=*) ;; *) die2 "--where needs <col>=<value>" ;; esac
               WHERE="$WHERE$2
"; shift ;;
      --header) HEADER=1 ;;
      --as) [ $# -ge 2 ] || die2 "--as needs a schema path"; ASREL="$2"; shift ;;
      *) break ;;
    esac
    shift
  done
fi
case "$CMD" in
  get) [ $# -ge 2 ] || die2 "get needs <file> <col>..."; ARG1="$1"; shift; ARGS="$*" ;;
  cols|schema) [ $# -eq 1 ] || die2 "$CMD needs one <file>"; ARG1="$1" ;;
  check|facts|index) [ $# -eq 0 ] || die2 "$CMD takes no argument" ;;
esac
# A file is named from the root: an absolute path under it is made relative.
[ -n "$ASREL" ] || case "$ARG1" in "$ROOT"/*) ARG1="${ARG1#"$ROOT"/}" ;; esac

if [ -z "$MAP" ] && [ "$CMD" != index ]; then
  MAP="$T/map"
  if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then git -C "$ROOT" ls-files
  else (cd "$ROOT" && find . -type f ! -path './.git/*' ! -path './build/*' | sed 's#^\./##'); fi \
    | awk -v r="$ROOT" '{ print $0 "\t" r "/" $0 }' > "$MAP"
fi
[ "$CMD" != index ] || { MAP="$T/map"; : > "$MAP"; }

PROG='
function fnd(rule, subj, msg) {
  if (mode == "facts") { print "G\t" rule "\t" subj "\t" msg; NF_++ }
  else { print "  " msg > "/dev/stderr"; BAD = 1 }
}
function excluded(rel,   n, a, k) {
  n = split(rel, a, "/")
  for (k = 1; k <= n; k++) if (a[k] == "fixtures" || a[k] ~ /^test/) return 1
  return 0
}
function glob2re(g,   i, c, o) {
  o = "^"
  for (i = 1; i <= length(g); i++) { c = substr(g, i, 1)
    if (c == "*") o = o "[^/]*"; else if (index(".+?^$|(){}[]\\", c) > 0) o = o "\\" c; else o = o c }
  return o "$"
}
function addschema(path, src,   line, n, f, first, id) {
  first = 1
  while ((getline line < path) > 0) {
    sub(/\r$/, "", line)
    if (line ~ /^#/ || line ~ /^[ \t]*$/) continue
    if (first) { first = 0; if (line != SHDR) { fnd("standard.register-declared", "register:" path, path ": the header row is not " SHDR); } continue }
    n = split(line, f, "\t")
    if (n != 8) { fnd("standard.register-declared", "register:" path, path ": a row has " n " columns, the header has 8: " f[1]); continue }
    id = f[1]; opt = 0
    if (id ~ /\?$/) { sub(/\?$/, "", id); opt = 1 }
    if (id in SEXACT || id in SGLOB) { fnd("standard.register-declared", "register:" id, path ": " id " is declared twice"); continue }
    NS++; SID[NS] = id; SOPT[NS] = opt; SHD[NS] = f[2]; SCL[NS] = f[3]; SKY[NS] = f[4]; SRF[NS] = f[5]; SOW[NS] = f[6]; SRD[NS] = f[7]; SNT[NS] = f[8]; SSRC[NS] = src
    if (index(id, "*") > 0) { SGLOB[id] = NS; SRE[NS] = glob2re(id); NG++; GL[NG] = NS } else SEXACT[id] = NS
  }
  close(path)
}
# the schema row of a path: an exact id wins, else the first glob; AMB counts the globs that matched
function findschema(rel,   g, k, c) {
  AMB = 0
  if (rel in SEXACT) return SEXACT[rel]
  c = 0
  for (k = 1; k <= NG; k++) if (rel ~ SRE[GL[k]]) { c++; if (c == 1) g = GL[k] }
  AMB = c
  return c ? g : 0
}
# the columns, header kind and key a path is read with: a =<path> row borrows them from that path'"'"'s own row
function effective(i, rel,   tmpl, p, ip, ipre, isuf, seg, tgt, j) {
  EH = SHD[i]; EC = SCL[i]; EK = SKY[i]; EERR = ""
  if (substr(SCL[i], 1, 1) != "=") return
  tmpl = substr(SCL[i], 2); tgt = tmpl
  p = index(tmpl, "*"); ip = index(SID[i], "*")
  if (p > 0 && ip > 0) {
    ipre = substr(SID[i], 1, ip - 1); isuf = substr(SID[i], ip + 1)
    seg = substr(rel, length(ipre) + 1, length(rel) - length(ipre) - length(isuf))
    tgt = substr(tmpl, 1, p - 1) seg substr(tmpl, p + 1)
  }
  if (!(tgt in SEXACT)) { EERR = SID[i] " is a copy of " tgt ", which has no schema row"; return }
  j = SEXACT[tgt]
  if (substr(SCL[j], 1, 1) == "=") { EERR = tgt " is itself a copy"; return }
  EH = SHD[j]; EC = SCL[j]; EK = SKY[j]
}
function loadfile(rel, i,   abs, line, nf, f, first, ln, r, c, k, n, a, nm, ragged, key, kk, kn, kcol, miss) {
  if (rel in LOADED) return
  LOADED[rel] = 1; abs = FILES[rel]; effective(i, rel)
  if (EERR != "") { fnd("standard.register-declared", "register:" rel, rel ": " EERR); LOADERR[rel] = 1; return }
  n = split(EC, a, " "); FNC[rel] = n; FNREQ[rel] = n
  for (k = 1; k <= n; k++) { nm = a[k]
    if (nm ~ /\?$/) { sub(/\?$/, "", nm); if (FNREQ[rel] == n) FNREQ[rel] = k - 1 }
    FCN[rel, k] = nm; FCI[rel, nm] = k }
  first = 1; ln = 0; r = 0; ragged = 0
  while ((getline line < abs) > 0) {
    ln++; sub(/\r$/, "", line)
    if (line ~ /^#/ || line ~ /^[ \t]*$/) continue
    nf = split(line, f, "\t")
    if (first) { first = 0
      if (EH == "row") {
        miss = (nf < FNREQ[rel] || nf > n)
        if (!miss) for (k = 1; k <= nf; k++) if (f[k] != FCN[rel, k]) miss = 1
        if (miss) { fnd("standard.register-declared", "register:" rel, rel " line " ln ": the header row is `" line "` and the schema declares `" EC "`"); LOADERR[rel] = 1 }
        continue
      }
    }
    r++; RLN[rel, r] = ln; RNF[rel, r] = nf
    if (nf < FNREQ[rel] || nf > n) {
      ragged++
      if (ragged <= 3) fnd("standard.register-declared", "register:" rel ":line" ln, rel " line " ln " has " nf " columns and the schema declares " FNREQ[rel] (FNREQ[rel] == n ? "" : ".." n))
      LOADERR[rel] = 1
    }
    for (c = 1; c <= nf; c++) RV[rel, r, c] = f[c]
  }
  close(abs)
  if (ragged > 3) fnd("standard.register-declared", "register:" rel ":ragged", rel ": " (ragged - 3) " more rows with the wrong column count")
  FNR_[rel] = r
  if (EK != "-" && EK != "=" && EK != "") {
    kn = split(EK, a, "+"); miss = 0
    for (k = 1; k <= kn; k++) { if (!((rel, a[k]) in FCI)) { fnd("standard.register-declared", "register:" rel, rel ": the key names " a[k] ", which is not a column"); miss = 1 } else kcol[k] = FCI[rel, a[k]] }
    if (!miss) for (r = 1; r <= FNR_[rel]; r++) {
      key = ""; for (k = 1; k <= kn; k++) key = key (k > 1 ? "\034" : "") RV[rel, r, kcol[k]]
      if ((rel, key) in KSEEN) fnd("standard.register-declared", "register:" rel ":key:" key, rel " line " RLN[rel, r] " repeats the key " EK "=" key " of line " KSEEN[rel, key])
      else KSEEN[rel, key] = RLN[rel, r]
    }
  }
}
function schemaof(rel,   i) { i = findschema(rel); if (i) loadfile(rel, i); return i }
function targetset(tf, tc,   tk, i, r, c) {
  tk = tf SUBSEP tc
  if (tk in TOK) return TOK[tk]
  TOK[tk] = 0
  if (!(tf in FILES)) { fnd("standard.reference-resolves", "target:" tf, "the target register " tf " is not in the tree"); return 0 }
  i = schemaof(tf)
  if (!i) { fnd("standard.reference-resolves", "target:" tf, "the target register " tf " has no schema row"); return 0 }
  if (tf in LOADERR) return 0
  if (!((tf, tc) in FCI)) { fnd("standard.reference-resolves", "target:" tf ":" tc, "the target register " tf " has no column " tc); return 0 }
  c = FCI[tf, tc]
  for (r = 1; r <= FNR_[tf]; r++) TV[tf, tc, RV[tf, r, c]] = 1
  TOK[tk] = 1
  return 1
}
function checkrefs(rel, i,   n, k, spec, p, col, tgt, nl, parts, main, lits, sep, q, tf, tc, c, r, v, ni, items, m, item, ok, pathmode, pre, pp) {
  if (SRF[i] == "-" || SRF[i] == "" || (rel in LOADERR)) return
  n = split(SRF[i], spec, " ")
  for (k = 1; k <= n; k++) {
    p = index(spec[k], ">")
    if (p == 0) { fnd("standard.register-declared", "register:" SID[i], SID[i] ": the reference " spec[k] " is not <column>>target"); continue }
    col = substr(spec[k], 1, p - 1); tgt = substr(spec[k], p + 1)
    if (!((rel, col) in FCI)) { fnd("standard.register-declared", "register:" SID[i] ":ref:" col, SID[i] ": the reference names " col ", which is not a column of " rel); continue }
    nl = split(tgt, parts, "|"); main = parts[1]; delete lits
    for (q = 2; q <= nl; q++) lits[parts[q]] = 1
    sep = ""; q = index(main, "@"); if (q > 0) { sep = substr(main, q + 1); main = substr(main, 1, q - 1); if (sep == "space") sep = " " }
    pathmode = (substr(main, 1, 5) == "path:")
    if (pathmode) pre = substr(main, 6)
    else { q = index(main, ":"); tf = substr(main, 1, q - 1); tc = substr(main, q + 1); if (!targetset(tf, tc)) continue }
    c = FCI[rel, col]
    for (r = 1; r <= FNR_[rel]; r++) {
      v = RV[rel, r, c]
      if (v == "" || v == "-" || (v in lits)) continue
      if (sep != "") ni = split(v, items, sep); else { ni = 1; items[1] = v }
      for (m = 1; m <= ni; m++) { item = items[m]
        if (item == "" || item == "-" || (item in lits)) continue
        if (pathmode) { pp = pre item; sub(/\/$/, "", pp); ok = ((pp in FILES) || (pp in DIRS)) }
        else ok = ((tf, tc, item) in TV)
        if (!ok) fnd("standard.reference-resolves", "ref:" rel ":" RLN[rel, r] ":" col, rel " line " RLN[rel, r] ": " col " is " item ", which is " (pathmode ? "not a path in the tree" : "not in " tf " column " tc))
      }
    }
  }
}
function esc(s) { gsub(/\|/, "\\|", s); return s }
BEGIN {
  FS = "\t"
  SHDR = "id\theader\tcolumns\tkey\trefs\towner\tread_by\tnote"
  while ((getline line < map) > 0) { split(line, f, "\t"); if (!(f[1] in FILES)) { NFL++; FL[NFL] = f[1] }; FILES[f[1]] = f[2]
    n = split(f[1], a, "/"); d = ""; for (k = 1; k < n; k++) { d = d (k > 1 ? "/" : "") a[k]; DIRS[d] = 1 } }
  close(map)
  addschema(schemas, 0)
  if (extra != "") addschema(extra, 1)
  if (mode == "index") {
    print "| Register | Owner | Key | Columns | References | Read by | What it holds |"
    print "|---|---|---|---|---|---|---|"
    for (i = 1; i <= NS; i++) {
      cl = SCL[i]; ky = SKY[i]; rf = SRF[i]
      if (substr(cl, 1, 1) == "=") { cl = "as `" substr(cl, 2) "`"; ky = "as the original"; rf = "-" } else cl = "`" cl "`"
      printf "| `%s` | %s | %s | %s | %s | %s | %s |\n", SID[i], SOW[i], esc(ky), esc(cl), esc(rf == "" ? "-" : rf), (SRD[i] == "-" ? "-" : "`" SRD[i] "`"), esc(SNT[i])
    }
    exit 0
  }
  if (mode == "cols" || mode == "schema" || mode == "get") {
    rel = arg1
    if (asrel != "") { rel = asrel; FILES[rel] = arg1 }
    i = findschema(rel)
    if (!i) { print "no schema row declares " rel > "/dev/stderr"; exit 3 }
    if (!(rel in FILES)) { print rel " is not in the tree" > "/dev/stderr"; exit 3 }
    effective(i, rel)
    if (EERR != "") { print EERR > "/dev/stderr"; exit 1 }
    if (mode == "cols") { print EC; exit 0 }
    if (mode == "schema") {
      print "id=" SID[i]; print "header=" EH; print "columns=" EC; print "key=" EK; print "refs=" SRF[i]
      print "owner=" SOW[i]; print "read_by=" SRD[i]; print "note=" SNT[i]; exit 0
    }
    loadfile(rel, i)
    if (BAD) exit 1
    nc = split(args, want, " ")
    for (k = 1; k <= nc; k++) { if (!((rel, want[k]) in FCI)) { print rel " has no column " want[k] " (declared: " EC ")" > "/dev/stderr"; exit 2 }; wc[k] = FCI[rel, want[k]] }
    nw = split(ENVIRON["SR_WHERE"], wl, "\n"); nwc = 0
    for (k = 1; k <= nw; k++) { if (wl[k] == "") continue; p = index(wl[k], "=")
      cn = substr(wl[k], 1, p - 1)
      if (!((rel, cn) in FCI)) { print rel " has no column " cn " (declared: " EC ")" > "/dev/stderr"; exit 2 }
      nwc++; wcol[nwc] = FCI[rel, cn]; wval[nwc] = substr(wl[k], p + 1) }
    if (header) { o = ""; for (k = 1; k <= nc; k++) o = o (k > 1 ? "\t" : "") want[k]; print o }
    for (r = 1; r <= FNR_[rel]; r++) {
      ok = 1; for (k = 1; k <= nwc; k++) if (RV[rel, r, wcol[k]] != wval[k]) { ok = 0; break }
      if (!ok) continue
      o = ""; for (k = 1; k <= nc; k++) o = o (k > 1 ? "\t" : "") RV[rel, r, wc[k]]
      print o
    }
    exit 0
  }
  # facts and check: every tracked .tsv has a row, every row has a file, every file is what its row says, every reference resolves
  home = (HOMEREL in FILES)
  for (n = 1; n <= NFL; n++) { rel = FL[n]
    if (rel !~ /\.tsv$/ || excluded(rel)) continue
    NTSV++
    i = findschema(rel)
    if (!i) { fnd("standard.register-declared", "register:" rel, rel " is a .tsv with no row in schemas.tsv: declare its columns, key and references there (harbor#1424)"); continue }
    if (AMB > 1) fnd("standard.register-declared", "register:" rel ":ambiguous", rel " is matched by " AMB " globs of schemas.tsv: one row per register")
    FSCH[rel] = i; USED[i]++
  }
  for (i = 1; i <= NS; i++) if (!(i in USED) && !SOPT[i] && (SSRC[i] == 1 || home))
    fnd("standard.register-declared", "register:" SID[i], SID[i] " has a row in " (SSRC[i] ? "the repo schema rows" : "schemas.tsv") " and no file in the tree: delete the row or restore the file")
  for (n = 1; n <= NFL; n++) { rel = FL[n]; if (rel in FSCH) loadfile(rel, FSCH[rel]) }
  for (n = 1; n <= NFL; n++) { rel = FL[n]; if (rel in FSCH) checkrefs(rel, FSCH[rel]) }
  if (mode == "check") exit (BAD ? 1 : 0)
  exit 0
}
'

HOMEREL="standards/naming/schemas.tsv"
_run() { # <mode> -> awk over the map
  SR_WHERE="$WHERE" awk -v mode="$1" -v map="$MAP" -v schemas="$SCHEMAS" -v extra="$EXTRA" -v arg1="$ARG1" -v args="$ARGS" -v asrel="$ASREL" \
      -v header="$HEADER" -v HOMEREL="$HOMEREL" "$PROG" < /dev/null
}
case "$CMD" in
  get|cols|schema)
    _rc=0; _run "$CMD" > "$T/out" 2> "$T/err" || _rc=$?
    case "$_rc" in
      0) cat "$T/out"; exit 0 ;;
      3) log_fail "$(head -n 1 "$T/err")"; log_fix "declare it: a row in standards/naming/schemas.tsv (or naming-schemas.tsv in a repo)"; log_end || :; exit 3 ;;
      2) log_fail "$(head -n 1 "$T/err")"; log_fix "sh $SELF cols $ARG1"; log_end || :; exit 2 ;;
      *) log_fail "$ARG1 is not the register its schema row declares, so no row was read"; cat "$T/err" >&2
         log_fix "bring the file and its row in schemas.tsv back into agreement"; log_end || :; exit 1 ;;
    esac ;;
  index) _run index; exit $? ;;
  facts) _rc=0; _run facts || _rc=$?; exit "$_rc" ;;
  check)
    _rc=0; _run facts > "$T/facts" || _rc=$?
    [ "$_rc" -eq 0 ] || { log_fail "the reader failed, so nothing was judged"; log_fix "sh $SELF facts"; log_end || :; exit 3; }
    _n=0
    while IFS='	' read -r _g _rule _subj _msg; do
      [ "$_g" = G ] || continue
      _n=$((_n + 1)); log_fail "[$_rule] $_msg"; log_nofix "standards/naming/schemas.tsv"
    done < "$T/facts"
    if [ "$_n" -eq 0 ]; then log_ok "every register is declared and every reference resolves"; log_end || :; exit 0; fi
    log_end || :; exit 1 ;;
esac
