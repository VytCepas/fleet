#!/usr/bin/env sh
# library-log.sh (harbor#771) — the one log format (contracts/logging.md), sourced by every tool that logs.
# Never run: it sets no shell option (set -u in a sourced file would leak into every caller), and
# every function is safe under the caller's set -eu.
#
#   . "$HERE/library-log.sh"
#   log_init <tool>               name the run and zero the counters (default: the script's name)
#   log_file <target>             opt in: the same lines, timestamped, in <target>/backups/<tool>/<TS>/log
#   log_head <step>               ── <tool> · <step>
#   log_ok|log_fail|log_warn|log_note|log_skip <message>      "  <TAG>: <message>"
#   log_fix <command>             "  fix: <command>" — the command that resolves the FAIL above
#   log_nofix <issue-or-doc>      "  fix: none automatic — see <issue-or-doc>"
#   pkg_hint <tool>               the command that installs <tool> on THIS machine (brew on macOS; apt-get, dnf, pacman, apk or
#                                 zypper on Linux/WSL2), for `log_fix "$(pkg_hint jq)"`. Prints one line, exits 0, never installs
#   log_end                       "<tool>: N ok, M failed, K warnings", then "log: <path>"; returns 0 iff M is 0
#   log_receipt <dir> <id> <status> [subject...]   a check's receipt for the finding ledger (harbor#896)
#   receipt_dir [config-dir]      the one receipt dir a writer passes to log_receipt and the reconcile reads
#   slug_of_url <url>             owner/name of a github.com remote (https, git@ or ssh spelling), nothing for any other URL
#   slug_valid <owner/name>       0 when it is exactly two github-shaped segments
#   repo_slug [dir]               owner/name of the repository THIS harbor is, for the script in <dir> (default: the caller's own
#                                 dir); nothing when none is derivable. Never fails the caller (harbor#1149)
#   status_init <id> [config-dir] begin a `<check> --status` report (contracts/reconcile.md, harbor#1031)
#   status_ok | status_drift <key> <desired> <observed> [delta] | status_unjudged <reason> | status_end
#                                 one item at its desired state / not at it / the run could not judge / close the report
#
# LOG_FD=2 sends the log to stderr, for a tool whose stdout is data. LOG_TS names the run dir when the
# caller already chose one. After log_file, LOG_DIR is the run dir (backups go beside the log) and
# LOG_PATH the file, empty when there is none.

# Sourced twice (a tool and a library it uses), the second source keeps the run's counts.
if [ -z "${_LOG_LIB:-}" ]; then
  _LOG_LIB=1
  _log_b="${0##*/}"; LOG_TOOL="${_log_b%.sh}"
  LOG_DIR=""; LOG_PATH=""; _LOG_WHY=""
  _LOG_OK=0; _LOG_FAILED=0; _LOG_WARNED=0; _LOG_NEEDFIX=0
fi

log_init() { # <tool>
  LOG_TOOL="${1:-$LOG_TOOL}"
  _LOG_OK=0; _LOG_FAILED=0; _LOG_WARNED=0; _LOG_NEEDFIX=0
}

# One line to the screen and, when there is one, to the file behind an ISO-8601 UTC time. A file
# that stops taking writes is said once on stderr and dropped; the screen never is.
_log_emit() { # <line>
  if [ "${LOG_FD:-1}" = 2 ]; then printf '%s\n' "$1" >&2 || :; else printf '%s\n' "$1" || :; fi
  [ -n "$LOG_PATH" ] || return 0
  if ! { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG_PATH"; } 2>/dev/null; then
    printf 'log-lib: %s stopped accepting writes — logging to the screen only\n' "$LOG_PATH" >&2
    _LOG_WHY="$LOG_PATH (incomplete — a write failed)"; LOG_PATH=""
  fi
  return 0
}

# A FAIL left without its fix is said, and counted, before the next line or the summary.
_log_needfix() {
  [ "$_LOG_NEEDFIX" = 1 ] || return 0
  _LOG_NEEDFIX=0; _LOG_WARNED=$((_LOG_WARNED + 1))
  _log_emit "  WARN: no fix line followed the FAIL above — $LOG_TOOL breaks contracts/logging.md"
  _log_emit "  fix: none automatic — see contracts/logging.md"
}

_log_line() { # <TAG> <message>
  _log_needfix
  _log_m="${2-}"
  case "$_log_m" in *'
'*) _log_m="$(printf '%s' "$_log_m" | tr '\n' ' ')" ;; esac
  if [ "$1" = '──' ]; then _log_emit "── $LOG_TOOL · $_log_m"; else _log_emit "  $1: $_log_m"; fi
}

log_head() { _log_line '──' "${1-}"; }
log_ok()   { _LOG_OK=$((_LOG_OK + 1)); _log_line ok "${1-}"; }
log_fail() { _log_line FAIL "${1-}"; _LOG_FAILED=$((_LOG_FAILED + 1)); _LOG_NEEDFIX=1; }
log_warn() { _LOG_WARNED=$((_LOG_WARNED + 1)); _log_line WARN "${1-}"; }
log_note() { _log_line note "${1-}"; }
log_skip() { _log_line skip "${1-}"; }
log_fix()  { _LOG_NEEDFIX=0; _log_emit "  fix: ${1-}"; }
log_nofix() { log_fix "none automatic — see ${1-}"; }

# pkg_hint (harbor#618): the install command a "tool is missing" FAIL should name on this OS. A tool's package
# name differs per manager (python3 is `python` on pacman, ShellCheck on dnf); one with no distro package
# (bun, gitleaks on apt/dnf) names its install page. With no known manager it still says what to install.
pkg_hint() { # <tool>
  _ph_t="${1-}"
  # A stripped PATH (a test, a hook) can lack uname; macOS and Linux both keep it in /usr/bin.
  _ph_os="$(uname -s 2>/dev/null || /usr/bin/uname -s 2>/dev/null || :)"
  case "$_ph_os" in
    Darwin)
      case "$_ph_t" in
        bun) printf 'brew install oven-sh/bun/bun\n' ;; python3) printf 'brew install python\n' ;;
        awk) printf 'brew install gawk\n' ;; sed) printf 'brew install gnu-sed\n' ;;
        mkfifo|sha256) printf 'brew install coreutils\n' ;; find) printf 'brew install findutils\n' ;;
        diff) printf 'brew install diffutils\n' ;; *) printf 'brew install %s\n' "$_ph_t" ;;
      esac; return 0 ;;
  esac
  case "$_ph_t" in bun) printf 'see https://bun.sh/docs/installation (bun has no distro package)\n'; return 0 ;; esac
  for _ph_m in apt-get dnf pacman apk zypper; do command -v "$_ph_m" >/dev/null 2>&1 && break; _ph_m=""; done
  _ph_p="$_ph_t"
  case "$_ph_t:$_ph_m" in
    awk:*) _ph_p=gawk ;; mkfifo:*|sha256:*) _ph_p=coreutils ;; find:*) _ph_p=findutils ;; diff:*) _ph_p=diffutils ;;
    python3:pacman) _ph_p=python ;; python3:apk) _ph_p=python3 ;; shellcheck:dnf) _ph_p=ShellCheck ;;
    gh:pacman|gh:apk) _ph_p=github-cli ;;
    gitleaks:apt-get|gitleaks:dnf|gitleaks:zypper|gitleaks:)
      printf 'see https://github.com/gitleaks/gitleaks/releases (no %s package)\n' "${_ph_m:-distro}"; return 0 ;;
  esac
  case "$_ph_m" in
    apt-get) printf 'sudo apt-get install -y %s\n' "$_ph_p" ;; dnf) printf 'sudo dnf install -y %s\n' "$_ph_p" ;;
    pacman) printf 'sudo pacman -S %s\n' "$_ph_p" ;; apk) printf 'sudo apk add %s\n' "$_ph_p" ;;
    zypper) printf 'sudo zypper install %s\n' "$_ph_p" ;;
    *) printf 'install %s with your distro package manager\n' "$_ph_p" ;;
  esac
}

log_file() { # <target>
  _log_ts="${LOG_TS:-$(date +%Y%m%d-%H%M%S)}"
  _log_d="${1%/}/backups/$LOG_TOOL/$_log_ts"
  # A run in the same second as another gets its own dir rather than appending to that log.
  [ ! -e "$_log_d/log" ] || _log_d="$_log_d-$$"
  if mkdir -p "$_log_d" 2>/dev/null && : >> "$_log_d/log" 2>/dev/null; then
    LOG_DIR="$_log_d"; LOG_PATH="$_log_d/log"; _LOG_WHY=""
  else
    printf 'log-lib: could not create %s — logging to the screen only\n' "$_log_d" >&2
    # LOG_DIR is read by the caller, for its backups.
    # shellcheck disable=SC2034
    LOG_DIR=""; LOG_PATH=""; _LOG_WHY="none — could not create $_log_d"
  fi
  return 0
}

log_end() {
  _log_needfix
  _log_emit "$LOG_TOOL: $_LOG_OK ok, $_LOG_FAILED failed, $_LOG_WARNED warnings"
  if [ -n "$LOG_PATH" ]; then _log_emit "log: $LOG_PATH"
  elif [ -n "$_LOG_WHY" ]; then _log_emit "log: $_LOG_WHY"; fi
  [ "$_LOG_FAILED" -eq 0 ]
}

# ── the check tier's receipt (harbor#896, contracts/finding.md) ──────────────
#   log_receipt <dir> <id> <green|red|stood-down> [subject...]
# writes <dir>/<id>.json = {"id", "ran_at", "status", "subjects": [...]} atomically, and returns 1 when
# it could not, so the caller says so in its own log. `lint-findings.sh reconcile` reads it and never re-runs
# the check. Subjects are stable ids (a repo name, a path, a short sha), never a count or a date, which
# would move the digest on their own. No jq: a check that adopts it gains no dependency.
_receipt_json() { # <string> → a JSON string literal; control characters dropped
  printf '"%s"' "$(printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')"
}
# The one receipt dir, for every writer and the reconcile (harbor#904): HARBOR_CHECK_RECEIPTS, else
# <config dir>/cache/checks. A writer's own cache dir (HARBOR_SCOPE_CACHE_DIR) never moves it.
receipt_dir() { printf '%s' "${HARBOR_CHECK_RECEIPTS:-${1:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}}/cache/checks}"; }
log_receipt() {
  [ "$#" -ge 3 ] || return 1
  _rd="$1"; _ri="$2"; _rs="$3"; shift 3
  case "$_rs" in green|red|stood-down) ;; *) return 1 ;; esac
  # Dots are in an id since harbor#1050 (`status.obligations`, as `event.retention` is) but never leading or doubled: the id
  # is a file name, and `.x` or `a..b` is not a name a reader's glob or a path check should be asked to trust.
  case "$_ri" in ''|.*|*..*|*.|*[!abcdefghijklmnopqrstuvwxyz0-9.-]*) return 1 ;; esac
  mkdir -p "$_rd" 2>/dev/null || return 1
  _rl=""
  for _rj in "$@"; do
    [ -n "$_rj" ] || continue
    _rl="${_rl:+$_rl,}$(_receipt_json "$_rj")"
  done
  # mktemp, never a predictable name: the cache is session-writable, and a planted symlink would
  # aim this write at whatever it points to (review #895).
  _rt="$(mktemp "$_rd/.$_ri.json.XXXXXX" 2>/dev/null)" || return 1
  # LOG_RECEIPT_FRESH_DAYS: how long the receipt counts as a run, for a check that runs less than
  # daily (harbor#577). Absent, the reconcile's default window applies.
  _rf=""; case "${LOG_RECEIPT_FRESH_DAYS:-}" in ''|*[!0-9]*) ;; *) _rf=",\"fresh_days\":$LOG_RECEIPT_FRESH_DAYS" ;; esac
  printf '{"id":%s,"ran_at":"%s","status":"%s","subjects":[%s]%s}\n' \
    "$(_receipt_json "$_ri")" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$_rs" "$_rl" "$_rf" > "$_rt" 2>/dev/null \
    && mv -f "$_rt" "$_rd/$_ri.json" 2>/dev/null \
    || { rm -f "$_rt" 2>/dev/null; return 1; }
}

# ── this repository's own name (harbor#1149) ────────────────────────────────
# A tool that names harbor's own repository (the CI reads, the gate's fetch) asks here, so no script spells it and a clone
# under another owner targets itself. THE ORDER IS THE POINT:
#   1. <dir> sits in a harbor checkout (<dir>/../contracts/repo-slug.txt exists): that checkout's origin, read raw from its own
#      config (an insteadOf rewrite is not applied). A checkout with no github.com origin takes the pin of step 2.
#   2. <dir> is an install: <dir>/repo-slug.txt, which runtime-install ships from contracts/repo-slug.txt.
# NOTHING ELSE IS READ: not the environment, not the registered ~/port checkout, not the private layer. A session can write
# all three, and lint-findings-run.sh fetches and runs what this names with the operator's gh login (review #895): a name a
# session could redirect would be a fetch it could redirect. A tool's own override variable (HARBOR_CI_PLAN_REPO, ...) is its
# caller's decision, taken before it asks here.
slug_of_url() { # <url> — what the bootstrap installer and repo_slug share: the three spellings a clone is given, else nothing
  case "${1-}" in
    https://github.com/*)     _sg="${1#https://github.com/}" ;;
    git@github.com:*)         _sg="${1#git@github.com:}" ;;
    ssh://git@github.com/*)   _sg="${1#ssh://git@github.com/}" ;;
    *)                        return 0 ;;
  esac
  _sg="${_sg%/}"; printf '%s' "${_sg%.git}"
}
slug_valid() { # <owner/name> — exactly two segments, github's own alphabets, no leading dash or dot
  case "${1-}" in ''|*/*/*|/*|*/|-*|*/-*|*/.*|*[!A-Za-z0-9/._-]*) return 1 ;; */*) ;; *) return 1 ;; esac
  case "${1%%/*}" in *[!A-Za-z0-9-]*) return 1 ;; esac
}
_rs_pin() { # <file> — its first line that is not blank or a # comment, when that line is a slug
  [ -f "$1" ] || return 0
  while IFS= read -r _rp_l || [ -n "$_rp_l" ]; do
    _rp_l="${_rp_l%"${_rp_l##*[![:space:]]}"}"
    case "$_rp_l" in ''|'#'*) continue ;; esac
    if slug_valid "$_rp_l"; then printf '%s\n' "$_rp_l"; fi
    return 0
  done < "$1"
  return 0
}
repo_slug() { # [dir]
  _rs_d="${1:-}"
  if [ -z "$_rs_d" ]; then case "$0" in */*) _rs_d="${0%/*}" ;; *) _rs_d=. ;; esac; fi
  _rs_d="$(CDPATH='' cd -- "$_rs_d" 2>/dev/null && pwd)" || return 0
  if [ -f "$_rs_d/../contracts/repo-slug.txt" ]; then
    # An empty environment but PATH, and only the checkout's own config: a hook's GIT_DIR would answer for the repository that is
    # committing, a GIT_TRACE would write a file, and a global config is not the checkout's origin.
    _rs_u="$(env -i "PATH=$PATH" git -C "$_rs_d/.." config --local --get remote.origin.url 2>/dev/null)" || _rs_u=""
    _rs_s="$(slug_of_url "$_rs_u")"
    if slug_valid "$_rs_s"; then printf '%s\n' "$_rs_s"; return 0; fi
    _rs_pin "$_rs_d/../contracts/repo-slug.txt"
  else
    _rs_pin "$_rs_d/repo-slug.txt"
  fi
  return 0
}

# ── the reconcile interface (harbor#1031, contracts/reconcile.md) ────────────
# A drift check's `--status` prints ONE shape on stdout, whoever wrote the check: a header, one record per item
# that is NOT at its desired state (desired, observed, delta, and how long it has been), the items that
# converged since the last run, and a summary. The delta and the ages, never the census.
#   status_init <id> [config-dir]  the header. The id is the check's receipt id; returns 1 when it is not a file name
#   status_ok [count]              items judged and at their desired state: counted, never printed
#   status_drift <key> <desired> <observed> [delta]   an item that is not: `new` the first run it is seen, `standing`
#                                  (with its age in seconds) every run after, until it converges. A key given twice in one
#                                  run is one row (the first), counted once
#   status_unjudged <reason>       the run could not judge: prints `unchecked`, leaves the first-seen state alone, returns 3
#   status_end                     `cleared` for every item gone since the last run, the summary, the state saved;
#                                  returns 0 when nothing drifted and 1 when something did (the caller exits with it)
# The first-seen state is <config dir>/cache/reconcile/<id>.tsv (`key<TAB>epoch`), written only by a run that judged.
# A tool that sources this and sets LOG_FD=2 keeps stdout as data, which a --status caller must.
reconcile_dir() { printf '%s' "${1:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}}/cache/reconcile"; }
_st_f() { printf '%s' "${1-}" | tr '\t\n\r' '   '; }   # one field: a tab or a line break would move every column after it
status_init() {
  _ST_ID="${1:-}"; _ST_CFG="${2:-}"; _ST_OPEN=0; _ST_PERSIST=0; _ST_EX=0; _ST_DR=0
  _ST_PREV=""; _ST_NEW=""; _ST_NOW=0
  _ST_SEEN='
'
  case "$_ST_ID" in ''|.*|*..*|*.|*[!abcdefghijklmnopqrstuvwxyz0-9.-]*) _ST_ID=""; return 1 ;; esac
  printf 'reconcile\t1\t%s\n' "$_ST_ID"
}
# Opened by the first item, not by status_init, so a run that cannot judge needs nothing but printf.
_st_open() {
  [ "$_ST_OPEN" = 0 ] || return 0
  _ST_OPEN=1
  _ST_NOW="$(date +%s 2>/dev/null)"; case "$_ST_NOW" in ''|*[!0-9]*) _ST_NOW=0 ;; esac
  _ST_DIR="$(reconcile_dir "$_ST_CFG")"; _ST_PREV="$_ST_DIR/$_ST_ID.tsv"
  # A config dir that is not there is not made for this: nothing is installed in it, so there is nothing to remember.
  [ -d "${_ST_CFG:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}}" ] || { _ST_NEW=""; return 0; }
  if [ "$_ST_NOW" -gt 0 ] && [ -n "$_ST_ID" ] && mkdir -p "$_ST_DIR" 2>/dev/null \
     && _ST_NEW="$(mktemp "$_ST_DIR/.$_ST_ID.XXXXXX" 2>/dev/null)" && [ -n "$_ST_NEW" ]; then
    _ST_PERSIST=1
  else
    _ST_NEW=""; log_warn "reconcile: first-seen state cannot be kept in $_ST_DIR, so ages are unknown this run"
  fi
}
status_ok() { _ST_EX=$((_ST_EX + ${1:-1})); }   # [count]
status_drift() { # <key> <desired> <observed> [delta]
  _sk="$(_st_f "${1-}")"; [ -n "$_sk" ] || _sk='-'
  # A key is unique within a report, and one item can be found by more than one wiring (a script wired under two matchers):
  # the first sighting is the row, and a repeat is neither listed, counted nor remembered a second time.
  case "$_ST_SEEN" in *"
$_sk
"*) return 0 ;; esac
  _ST_SEEN="$_ST_SEEN$_sk
"
  _st_open
  _ST_EX=$((_ST_EX + 1)); _ST_DR=$((_ST_DR + 1))
  _sa='-'; _sy=standing
  if [ "$_ST_PERSIST" = 1 ]; then
    # ENVIRON, not -v: awk reads a backslash in a -v value as an escape, and a key may hold one
    _sf=""; [ ! -f "$_ST_PREV" ] || _sf="$(RECONCILE_K="$_sk" awk -F'\t' '$1 == ENVIRON["RECONCILE_K"] { print $2; exit }' "$_ST_PREV" 2>/dev/null)"
    case "$_sf" in ''|*[!0-9]*) _sf="$_ST_NOW"; _sy=new ;; esac
    _sa=$((_ST_NOW - _sf)); [ "$_sa" -ge 0 ] || _sa=0
    printf '%s\t%s\n' "$_sk" "$_sf" >> "$_ST_NEW"
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$_sy" "$_sk" "$(_st_f "${2-}")" "$(_st_f "${3-}")" "$(_st_f "${4:--}")" "$_sa"
}
status_unjudged() { # <reason>
  [ -z "${_ST_NEW:-}" ] || rm -f "$_ST_NEW" 2>/dev/null
  printf 'unchecked\t%s\n' "$(_st_f "${1-}")"
  return 3
}
status_end() {
  _st_open
  _sc=0
  if [ "$_ST_PERSIST" = 1 ] && [ -f "$_ST_PREV" ]; then
    # keys the last run held and this one does not: converged (or no longer examined)
    # the new file is read in BEGIN: with NR == FNR an empty new file would leave the old one looking like the first
    RECONCILE_NEW="$_ST_NEW" awk -F'\t' 'BEGIN { while ((getline l < ENVIRON["RECONCILE_NEW"]) > 0) { split(l, a, "\t"); seen[a[1]] = 1 } }
      !($1 in seen) { print $1 }' "$_ST_PREV" > "$_ST_NEW.cl" 2>/dev/null || : > "$_ST_NEW.cl"
    while IFS= read -r _sk; do
      [ -n "$_sk" ] || continue
      printf 'cleared\t%s\n' "$_sk"; _sc=$((_sc + 1))
    done < "$_ST_NEW.cl"
    rm -f "$_ST_NEW.cl" 2>/dev/null
  fi
  printf 'summary\t%s\t%s\t%s\n' "$_ST_EX" "$_ST_DR" "$_sc"
  if [ "$_ST_PERSIST" = 1 ]; then
    mv -f "$_ST_NEW" "$_ST_PREV" 2>/dev/null || { rm -f "$_ST_NEW" 2>/dev/null; log_warn "reconcile: first-seen state was not saved to $_ST_PREV"; }
  fi
  [ "$_ST_DR" -eq 0 ]
}

# ── the auto-remediation allowlist (harbor#1031 item 4) ──────────────────────
# One list says what may repair without a person: the HEAL rows of contracts/finding.md (the lanes block), added by a
# person's PR. A reconciler (a tool that fixes what a drift check found) asks before it repairs, and a repair the rows
# do not name is refused, never run. `--status` itself repairs nothing; this is for the tool that does.
#   heal_rows [contract]                        `lane TAB key TAB subject TAB repair`, one per row of the lanes block
#   repair_allowed <key> <subject> <repair> [contract]   0 when a HEAL row of <key> matches <subject> and <repair> is its repair
#                                               (`<subject>` read as "$1", the way the runner passes it); else 2, a FAIL and a fix
#                                               on the log. <subject> is `-` for a repair that takes none
heal_rows() { # [contract]
  awk '/^<!-- lanes:begin -->$/ { t = 1; next } /^<!-- lanes:end -->$/ { t = 0 } t && /^\|/' "${1:-${ROOT:-.}/contracts/finding.md}" 2>/dev/null \
    | awk -F'|' '{ for (i = 2; i < NF; i++) { f = $i; gsub(/`/, "", f); gsub(/^[[:space:]]+|[[:space:]]+$/, "", f); $i = f }
                   if ($2 == "lane" || $2 ~ /^-+$/) next
                   printf "%s\t%s\t%s\t%s\n", $2, $3, $4, $5 }'
}
repair_allowed() { # <key> <subject> <repair> [contract]
  _ra_k="${1-}"; _ra_s="${2-}"; _ra_c="${3-}"; _ra_f="${4:-${ROOT:-.}/contracts/finding.md}"
  [ -f "$_ra_f" ] || { log_fail "repair refused: no allowlist at $_ra_f, so nothing says what may repair"; log_fix "git checkout origin/main -- contracts/finding.md"; return 2; }
  [ "$_ra_s" != stood-down ] || { log_fail "repair refused: $_ra_k is stood down, so a person reads it"; log_nofix "contracts/finding.md, the HEAL rows"; return 2; }
  _ra_rows="$(heal_rows "$_ra_f")"
  while IFS='	' read -r _rl _rk _rs _rr; do
    [ "$_rl" = HEAL ] && [ "$_rk" = "$_ra_k" ] || continue
    [ "$(printf '%s' "$_rr" | sed 's/<subject>/"$1"/g')" = "$_ra_c" ] || continue
    # shellcheck disable=SC2254
    case "$_ra_s" in -) return 0 ;; $_rs) return 0 ;; esac
  done <<RA_ROWS
$_ra_rows
RA_ROWS
  log_fail "repair refused: $_ra_c on ${_ra_s} is not a HEAL row of $_ra_k in contracts/finding.md"
  log_fix "a person adds the row to the lanes block of contracts/finding.md in a PR; nothing else may repair on its own"
  return 2
}
