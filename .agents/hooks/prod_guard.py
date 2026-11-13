"""Prod-safety guard (PI-168, ADR-012): destructive commands, and secret reads.

PreToolUse hook on Bash. Deterministic deny-table — no LLM, no network.

TWO CLASSES, one hook. Destruction is the original one. Reading a secret file
is the second (PI-893): it destroys nothing, but the contents land in the model
transcript, where they are re-sent on every subsequent turn and outlive the
session. It lives here rather than in a sibling hook because it needs the
identical machinery — the config walk with its symlink refusal, ``safety.allow``,
the ask/deny-by-mode posture, fail-open — and a second copy of security-critical
code is the drift this repo keeps finding. Extending this hook also reaches the
non-Claude surfaces through ``admission_agent_adapter.py`` for free.
Destructive operations that bypass the git/CI boundary (cloud deletes,
DROP DATABASE, terraform destroy, …) get:

- ``ask``   in interactive sessions — a human confirms or rejects;
- ``block`` in fully autonomous sessions (``bypassPermissions``) — there is
  no human to ask, so the command is blocked outright.

Escape hatch: ``safety.allow`` in ``.agents/config.yaml`` holds a JSON list
of regex patterns; a command matching any of them is never flagged. Use it
for known-safe contexts (e.g. a dev-cluster kubectl context).

This is a guardrail, not the security boundary (ADR-007/ADR-012): a
sufficiently creative command can evade a deny-list. The guarantee comes
from credential separation — agent sessions must never hold production
credentials (see .agents/docs/guides/secrets.md).

Fail-open by design: any internal error lets the command proceed.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import NamedTuple

# (pattern, label) — matched against the full command string. ``_SEG``
# tolerates global flags between the CLI name and the destructive verb
# (e.g. `kubectl --context prod delete …`) while stopping at pipeline and
# command separators; the cost is rare false positives on odd resource
# names, which the ask/allowlist paths absorb cheaply.
_SEG = r"[^|;&]*?"
# `--replace` / `--replace=true` but never `--replace=false` (PI-906): the
# false spelling is the default written out, and flagging it would nag on a
# command that is explicitly asking NOT to overwrite.
_REPLACE = r"--replace(?:=true)?(?![\w=-])"
DENY_RULES: list[tuple[re.Pattern[str], str]] = [
    # OpenTofu (`tofu`) is a CLI-identical Terraform fork — guard both the same
    # way (PI-488). `(?:-\S+\s+)*` tolerates global options before the verb
    # (e.g. `tofu -chdir=infra destroy`) like _SEG does for the other rules, but
    # stays flag-specific (only skips leading `-tokens`) so a read-only
    # `plan -destroy` is NOT flagged. Routine `apply -auto-approve` is
    # intentionally not flagged; only destroy / apply-with-destroy is.
    (
        re.compile(r"\b(?:terraform|tofu)\s+(?:-\S+\s+)*(destroy|apply\s+.*-destroy)\b"),
        "terraform/tofu destroy/apply -destroy",
    ),
    (re.compile(rf"\bkubectl\b{_SEG}\bdelete\b"), "kubectl delete"),
    (re.compile(rf"\bhelm\b{_SEG}\b(uninstall|delete)\b"), "helm uninstall"),
    (re.compile(rf"\baws\b{_SEG}\b(delete|terminate|remove)\S*\b"), "aws delete/terminate"),
    (
        re.compile(rf"\baws\b{_SEG}\bs3\s+(rb\b|rm\b{_SEG}--recursive)"),
        "aws s3 bucket/recursive removal",
    ),
    (re.compile(rf"\bgcloud\b{_SEG}\bdelete\b"), "gcloud delete"),
    # GCS recursive removal, both spellings (PI-906). The `gcloud delete` rule
    # above LOOKS like it covers this and does not: it keys on the token
    # `delete`, which neither `gsutil rm -r` nor the modern `gcloud storage rm
    # -r` carries — so the highest-blast-radius GCS operation walked past a
    # gcloud rule. (`gcloud storage buckets delete` does carry the token and is
    # already caught above.) Shaped like the `aws s3 rm --recursive` rule: a
    # single-object `rm` is not flagged, only the recursive form, which is what
    # empties a bucket. `-\w*[rR]\w*` covers gsutil's `-r` and `-R`; the flag may
    # sit before or after the URL, hence _SEG on both sides.
    (
        re.compile(rf"\bgsutil\b{_SEG}\brm\b{_SEG}\s(?:-\w*[rR]\w*|--recursive)\b"),
        "gsutil recursive bucket removal",
    ),
    (
        re.compile(rf"\bgcloud\b{_SEG}\bstorage\s+rm\b{_SEG}\s(?:-\w*[rR]\w*|--recursive)\b"),
        "gcloud storage recursive bucket removal",
    ),
    # BigQuery dataset/table destruction (PI-906). Flag-specific rather than
    # _SEG for `plan -destroy`'s reason: `bq query` takes SQL as an argument, so
    # an _SEG reaching into the statement would fire on any query whose text
    # happens to contain `rm`. Only global flags (`--project_id=…`,
    # `--location=…`) may sit between `bq` and the verb. `--help`/`-h` is a
    # read-only lookup of the very command being guarded — never flag it.
    (
        re.compile(r"\bbq\s+(?:-\S+\s+)*rm\b(?!\s+(?:--help|-h)\b)"),
        "bq rm (BigQuery dataset/table removal)",
    ),
    # `bq truncate` is a bq SUBCOMMAND, not SQL, so the `truncate table` rule
    # below never sees it — there is no `table` token in `bq truncate ds.t`
    # (PI-906). Same shape and same help exemption as `bq rm`.
    (
        re.compile(r"\bbq\s+(?:-\S+\s+)*truncate\b(?!\s+(?:--help|-h)\b)"),
        "bq truncate (BigQuery table truncation)",
    ),
    # `--replace` overwrites the destination: the prior contents are gone as
    # surely as after a DROP, while the verb reads like an ordinary write
    # (PI-906). `--replace=false` is the DEFAULT spelled out and must not be
    # flagged, which a bare `--replace\b` would do — `\b` matches before the
    # `=`. Hence `_REPLACE`: the bare flag or `=true`, never `=false`.
    (
        re.compile(rf"\bbq\s+(?:-\S+\s+)*load\b{_SEG}\s{_REPLACE}"),
        "bq load --replace (destination overwrite)",
    ),
    # A destination_table WITHOUT --replace appends, which is not destruction;
    # both must be present, and the flags appear in either order.
    (
        re.compile(
            rf"\bbq\s+(?:-\S+\s+)*query\b"
            rf"(?={_SEG}\s--destination_table[=\s])"
            rf"(?={_SEG}\s{_REPLACE})"
        ),
        "bq query --replace (destination table overwrite)",
    ),
    (re.compile(rf"\baz\b{_SEG}\bdelete\b"), "az delete"),
    # dbt `--full-refresh` drops and rebuilds incremental models: data
    # destruction wearing a build verb (PI-906). Two lookaheads because the
    # flags appear in either order, the same idiom the `rm -r -f` rule below
    # uses. DELIBERATE LIMIT: a production TARGET must be named explicitly, so
    # routine `dbt run --full-refresh --target dev` stays unflagged — a guard
    # that nags on ordinary dev work gets switched off. The cost is that a
    # bare `--full-refresh` against a profile whose DEFAULT target is prod is
    # not reached; naming the target is the supported way to be protected.
    #
    # THE TARGET VALUE IS QUOTED AS OFTEN AS NOT, and the first cut required
    # `prod` immediately after whitespace — so `--target "prod"` and
    # `--target="prod"` ran the destructive refresh straight through a rule
    # written to stop it (PR #915 review, P1). The shell strips the quotes
    # before dbt sees them; this guard reads the RAW command string, so it has
    # to tolerate what the shell would remove.
    #
    # AND THE VALUE MUST END. `\s*prod` also matched `--target prod-dev`,
    # flagging a dev target because its name starts with the same four letters
    # (PR #915 review). `(?![\w-])` is the boundary — a plain `\b` does NOT
    # help here, because `-` is a non-word character and `prod\b` matches
    # happily inside `prod-dev`.
    (
        re.compile(
            r"\bdbt\b"
            rf"(?={_SEG}\s--full-refresh\b)"
            rf"(?={_SEG}\s(?:--target[=\s]|-t\s)\s*[\"']?(?:prod|production)(?![\w-]))"
        ),
        "dbt --full-refresh against a production target",
    ),
    # A dbt run/build/seed/snapshot against a PRODUCTION target rewrites prod
    # relations whether or not `--full-refresh` is passed: a `table`
    # materialisation is a drop-and-recreate every time, and `run-operation`
    # executes an arbitrary macro (PI-906). The `--full-refresh` rule above
    # stays first so its more specific label keeps firing for that case.
    #
    # JUDGMENT CALL, stated so it can be overruled: this flags the ordinary
    # production deploy command, `dbt build --target prod`. It is `ask`, not
    # deny, and the posture is that an agent shell reaching prod should confirm
    # once — a deploy pipeline does not run through this hook, and a human who
    # deploys by hand all day has `safety.allow`. Read-only verbs (test,
    # compile, parse, docs, ls, debug, deps, show, source) are deliberately
    # absent from the verb list.
    (
        re.compile(
            r"\bdbt\b"
            rf"(?={_SEG}\s(?:build|run|run-operation|seed|snapshot)\b)"
            rf"(?={_SEG}\s(?:--target[=\s]|-t\s)\s*[\"']?(?:prod|production)(?![\w-]))"
        ),
        "dbt write against a production target",
    ),
    # IAM mutation on shared identities (PI-906). Granting access is not
    # obviously "destructive" and so was never modelled, but where an identity
    # is shared it changes other people's reach without their knowledge, and it
    # is the least reversible thing in this table. Grants are narrowed to the
    # roles that hand over the estate (owner/editor/any *Admin) so that routine
    # `roles/bigquery.dataViewer` grants stay unflagged; removals are flagged
    # unconditionally, because taking access away is destruction by another
    # name. `get-iam-policy` is read-only and matches neither.
    #
    # Same quoting problem as the dbt rule above, and the same severity (PR #915
    # review, P1): `--role="roles/owner"` passes gcloud exactly the argument
    # `roles/owner`, but the raw command carries a quote between `--role=` and
    # `roles/`, so an unquoted-only pattern let the estate handover through in
    # autonomous mode — where the verdict is a hard deny, not a prompt.
    (
        re.compile(
            rf"\bgcloud\b{_SEG}\badd-iam-policy-binding\b"
            rf"{_SEG}--role[=\s]\s*[\"']?roles/(?:owner|editor|\S*[Aa]dmin)"
        ),
        "gcloud IAM grant of owner/editor/admin",
    ),
    (
        re.compile(rf"\bgcloud\b{_SEG}\bremove-iam-policy-binding\b"),
        "gcloud IAM binding removal",
    ),
    # `set-iam-policy` REPLACES the whole policy from a file, so it revokes
    # every binding the file omits — the widest access change in the set, and
    # the one that looks least like one (PI-906). Flagged unconditionally: the
    # roles are in the file, which this guard does not read.
    (
        re.compile(rf"\bgcloud\b{_SEG}\bset-iam-policy\b"),
        "gcloud IAM policy replacement",
    ),
    # A service-account key is a long-lived credential that outlives the
    # session, cannot be rotated by revoking a login, and is exactly what the
    # credential-separation boundary (ADR-012) exists to keep out of an agent
    # shell. `keys list` / `keys describe` are reads and match neither.
    (
        re.compile(rf"\bgcloud\b{_SEG}\biam\s+service-accounts\s+keys\s+create\b"),
        "gcloud service-account key creation",
    ),
    # Bucket-level access. `iam ch` edits bindings, `iam set` replaces the
    # policy wholesale; `iam get` is a read and is not flagged.
    (
        re.compile(rf"\bgsutil\b{_SEG}\biam\s+(?:ch|set)\b"),
        "gsutil bucket IAM mutation",
    ),
    # The same three verbs exist on `bq` for datasets and tables, and none of
    # the gcloud rules above reach them — different CLI, identical effect.
    (
        re.compile(
            r"\bbq\s+(?:-\S+\s+)*"
            r"(?:set-iam-policy|add-iam-policy-binding|remove-iam-policy-binding)\b"
        ),
        "bq IAM policy mutation",
    ),
    # `bq update --source <file>` replaces a dataset's ACL (or a table's
    # schema) from a file — the pre-IAM spelling of set-iam-policy, still
    # supported and still a wholesale replacement.
    (
        re.compile(rf"\bbq\s+(?:-\S+\s+)*update\b{_SEG}\s--source[=\s]"),
        "bq update --source (dataset ACL/schema replacement)",
    ),
    (re.compile(r"\bdrop\s+(table|database|schema)\b", re.IGNORECASE), "SQL DROP"),
    (re.compile(r"\btruncate\s+table\b", re.IGNORECASE), "SQL TRUNCATE"),
    # A full-table DELETE empties it as surely as a TRUNCATE, and neither rule
    # above reaches it (PI-906). `\s+` after the verb keeps identifiers that
    # merely start with the word — `deleted_at`, `delete_log` — out, and the
    # `\b` before it keeps `is_deleted FROM …` out: the word must stand alone
    # and be immediately followed by FROM, which a SELECT never does.
    #
    # A TABLE MUST FOLLOW, AND THE CLAUSE MUST END. "delete from" is ordinary
    # English, unlike "drop table" and "truncate table", so the bare form this
    # rule first shipped with flagged six perfectly normal commands — measured
    # against the live deny table, not supposed:
    #     git commit -m "chore: delete from the stale cache"
    #     git log --grep "delete from"
    #     grep -rn "DELETE FROM" src/
    #     echo 'how to delete from a list in python'
    #     # TODO: delete from the queue once drained
    #     echo "we should delete from that table eventually"
    # Note the second-order problem: writing a commit message ABOUT this rule
    # tripped it.
    #
    # Requiring an identifier and then a WHERE, a statement terminator or a
    # closing quote separates the statement from the sentence — prose continues
    # with more words, SQL does not. Measured after: 0 false positives on those
    # six, 0 missed true positives on the destructive corpus.
    (
        re.compile(
            r"\bdelete\s+from\s+[A-Za-z_`\"\[][\w.`\"\[\]$-]*\s*(?:\bwhere\b|;|\"|'|$)",
            re.IGNORECASE,
        ),
        "SQL DELETE FROM",
    ),
    # MERGE rewrites and can DELETE rows in the target; only DROP/TRUNCATE and
    # DELETE FROM were modelled (PI-906). "merge" alone is hopeless as a
    # signal — `git merge`, `gh pr merge` and every commit message about a
    # merge would match — so the rule keys on the SHAPE of the statement:
    # target identifier, USING, ON, then WHEN [NOT] MATCHED.
    #
    # USING ... ON alone was NOT enough, measured rather than supposed: the
    # sentence `echo "we should merge into that table using the new source on
    # monday"` matched it. Every clause in that shape is ordinary English. The
    # WHEN [NOT] MATCHED clause is not, and MERGE is invalid without at least
    # one of them, so requiring it costs no true positive.
    (
        re.compile(
            r"\bmerge\s+(?:into\s+)?[A-Za-z_`\"\[][\w.`\"\[\]$-]*"
            r"\s+(?:(?:as\s+)?[A-Za-z_]\w*\s+)?using\b"
            rf"{_SEG}\bon\b{_SEG}\bwhen\s+(?:not\s+)?matched\b",
            re.IGNORECASE,
        ),
        "SQL MERGE",
    ),
    (
        # Recursive + force can be bundled (-rf/-fr) OR split across separate
        # args in any order (rm -r -f /, rm --force --recursive /) — the old
        # single-token pattern missed the split forms (2026-07 review). Two
        # lookaheads assert both a recursive and a force flag appear somewhere in
        # the option run before a dangerous target (an absolute path other than
        # /tmp, or ~).
        re.compile(
            r"\brm\b"
            r"(?=(?:\s+-{1,2}[\w-]+)*\s+(?:-\w*r\w*|--recursive)\b)"
            r"(?=(?:\s+-{1,2}[\w-]+)*\s+(?:-\w*f\w*|--force)\b)"
            r"(?:\s+-{1,2}[\w-]+)+\s+(/(?!tmp\b)|~)"
        ),
        "recursive force-remove outside the project",
    ),
    # Publishing a GTM container version pushes tags to every live page the
    # container is on — instant, global, and not a git-mediated change
    # (PI-906). THE HONEST LIMIT: this matcher sees a `curl` in a Bash tool
    # call. The same publish through a non-Bash tool, an MCP server or the GTM
    # UI bypasses it entirely. The durable control is a GTM-side permission;
    # this only stops the spelling an agent shell reaches for.
    (
        re.compile(r"tagmanager\.googleapis\.com[^\s'\"]*:publish"),
        "GTM container version publish",
    ),
    (re.compile(r"\bgh\s+repo\s+delete\b"), "gh repo delete"),
    (re.compile(r"\bdocker\s+(volume\s+prune|system\s+prune)\b"), "docker prune"),
]

# ── Prose is not execution (#965) ───────────────────────────────────────────
# The deny table above regex-searches the RAW command string, so WRITING ABOUT a
# destructive verb was indistinguishable from RUNNING it. Measured before this:
# 5 of 5 pure documentation commands returned `ask`, including a commit message
# that says never to run the verb it names.
#
#     git commit -m "docs: never run terraform destroy on prod"
#     grep -rn 'terraform destroy' docs/
#     echo 'the dangerous verb is DROP DATABASE'
#     cat runbook.md | grep -c 'kubectl delete namespace'
#
# WHY THE `delete from` FIX DOES NOT GENERALISE. That rule solved its own
# version of this by narrowing the RULE — requiring an identifier and then a
# terminator — because "delete from" is ordinary English and real SQL is not.
# That lever does not exist here: the text inside the commit message is
# BYTE-IDENTICAL to the real command. `terraform destroy` is `terraform
# destroy`. Only the CONTEXT it sits in separates the two, so context is what
# this reads.
#
# FAIL-CLOSED BY CONSTRUCTION, and this is the whole safety argument: an
# ALLOW-LIST of heads whose quoted arguments are inert, never a deny-list of
# heads that execute. A deny-list has to enumerate `sh -c`, `bash -c`, `eval`,
# `ssh`, `xargs`, `find -exec`, `su -c`, `env`, `timeout`, `watch`, `python -c`,
# `perl -e`… and every one it misses is a fail-open. An allow-list that misses
# something merely keeps today's prompt. `sh -c "terraform destroy"` is not
# exempt because `sh` is not on the list.
#
# The exemption is also SUBTRACTIVE, never a short-circuit: a rule that still
# matches once the prose is blanked out still fires, so `git commit -m "x" &&
# terraform destroy` is unaffected. And blanking happens IN PLACE in the raw
# string, preserving every other byte, because some rules match on quote
# characters themselves (the `delete from` terminator class).
_PROSE_HEADS = frozenset({"echo", "printf"})

#: Searchers whose pattern argument is inert. DELIBERATELY NOT `_PATTERN_FIRST_ARG`,
#: which exists for a different question (which arg is not a path) and includes
#: `sed`/`awk`/`gawk`/`nawk`. Both of those EXECUTE: `awk 'BEGIN{system("…")}'`
#: and GNU `sed 's/x/y/e'` run their argument, so exempting them would be a
#: fail-open. Measured — both leaked through a draft of this that reused the
#: other set.
_PROSE_PATTERN_TOOLS = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack"})

#: A substitution inside a quoted string is code, whatever encloses it:
#: `echo "$(terraform destroy)"` prints the OUTPUT of a real destroy. Blanking
#: such a span would hide the verb from the deny table — the third fail-open a
#: draft of this shipped.
_HAS_SUBSTITUTION = re.compile(r"\$\(|`|\$\{")

# ── PI-996: read the shell's grammar, not a regex that guesses at it ─────────
# Five review rounds (#942, #952, #953, #974, #979) patched this exemption one
# reported shape at a time, and #971 merged with three more already reported.
# Every one was the same mistake — a regex deciding where a quote, a statement
# or a redirection ends — and each of these RAN its verb with no verdict:
#
#     echo 'safe\'; terraform destroy; echo 'x'    `\'` is no escape inside '…'
#     echo > >(sh) "terraform destroy"            the redirection came first
#     echo > x.sh "terraform destroy"             the same, staging a script
#     echo "terraform destroy" 2>/dev/null|sh     `2>\S+` swallowed the pipe
#
# So the command is now LEXED by POSIX quoting rules, and a quoted region is
# exempt only when everything about the simple command holding it is modelled.
# Every doubt resolves the way the allow-list above does: a construct this does
# not model costs the WHOLE command its exemption, which keeps today's prompt
# and never loses a verdict. Each refusal below was run in bash and zsh with a
# harmless payload before it was written down.

#: Names that end the analysis for the whole command. Compound-command words
#: put a prose-headed statement inside a body whose output is piped at the
#: closing word: `for x in 1; do echo "…"; done | sh` RAN, and so did the same
#: body in `{ …; }`. The rest rebind a name or a descriptor for everything after
#: them: `hash -p /bin/sh grep` (bash) and `hash grep=/bin/sh` (zsh) made
#: `grep -c "…"` run its pattern, `alias echo='sh -c'` did the same to `echo` on
#: the next line in zsh, and `exec >run.sh` writes every later statement to a
#: file. `eval` and `source` run text this lexer never sees as statements.
_UNMODELLED_NAMES = frozenset(
    {
        "case",
        "coproc",
        "do",
        "done",
        "elif",
        "else",
        "esac",
        "fi",
        "for",
        "foreach",
        "function",
        "if",
        "repeat",
        "select",
        "then",
        "until",
        "while",
        "alias",
        "eval",
        "exec",
        "hash",
        "source",
        ".",
        # #1035: `enable -n echo` (bash), `disable echo` and `autoload echo`
        # (zsh) hand `echo` to a PATH binary or a function; a DEBUG `trap`
        # runs code before every later command. Each RAN a payload.
        "enable",
        "disable",
        "autoload",
        "trap",
    }
)

#: Words that run the NEXT word as the command, so the name check looks past them.
_COMMAND_PREFIXES = frozenset({"builtin", "command", "noglob", "nocorrect"})

#: Where an unquoted word ends. Parens are here so no word swallows one: every
#: unquoted `(` or `)` refuses the command — subshells, process substitution,
#: function definitions, arithmetic, and zsh glob qualifiers that run code.
_WORD_END = frozenset(" \t\n;&|<>()")

#: Redirection operators whose effect on a descriptor is modelled, longest first
#: so `>>|` is never read as `>>`. A heredoc is deliberately absent: its body is
#: not shell text, and a quote inside it desynchronises every later line. After
#: `cat <<'EOF'` with a body line `echo "`, the statement between `EOF` and a
#: second `echo "` RAN in bash and zsh while a scan saw one quoted string.
_REDIRECT_OPS = sorted(
    {"<<<", "<>", "<&", "<", ">>|", ">>!", ">>", ">&", ">|", ">!", ">", "&>>", "&>|", "&>!", "&>"},
    key=len,
    reverse=True,
)

#: A glob in a command name: `*`, `?`, or a bracket expression — but not the
#: `[` and `[[` test commands themselves.
_GLOB_NAME = re.compile(r"[*?]|\[(?!\[?$)")

#: Directories an absolute prose head may be spelled from. A relative path is
#: whatever file sits there: `./echo "…"` runs a local script named echo.
_SYSTEM_BIN_DIRS = frozenset({"/bin", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"})


class _Word(NamedTuple):
    text: str
    #: Offsets of each quoted region in the whole command, quotes included.
    quoted: tuple[tuple[int, int], ...]
    #: No quote, backslash or `$` anywhere in the word. Glob, brace and tilde
    #: characters do NOT clear it. That is safe only because every use compares
    #: a plain word with a fixed name — a pattern cannot equal `echo` — and
    #: `_ends_analysis` refuses a command name that could glob into one.
    plain: bool


class _Simple(NamedTuple):
    words: list[_Word]
    #: (IO number or "", operator, target word), in the order written.
    redirects: list[tuple[str, str, _Word]]
    #: The operator that ended this command — `|`, `&&`, `;`, a newline — or "".
    then: str


def _read_word(command: str, i: int) -> tuple[_Word, int] | None:
    """The shell word starting at *i*, or None when its quoting is not resolvable."""
    start, n = i, len(command)
    quoted: list[tuple[int, int]] = []
    plain = True
    while i < n and command[i] not in _WORD_END:
        ch = command[i]
        if ch == "\\":
            if i + 1 >= n:
                return None
            plain = False
            i += 2
        elif ch == "'":
            # Nothing is special inside single quotes, a backslash included.
            close = command.find("'", i + 1)
            if close < 0:
                return None
            quoted.append((i, close + 1))
            plain = False
            i = close + 1
        elif ch == '"':
            j = i + 1
            while j < n and command[j] != '"':
                if command[j] == "\\":
                    j += 2
                    continue
                # A substitution can nest quotes of its own, so where this
                # string ends is no longer something a scan can know.
                if command[j] == "`" or command.startswith(("$(", "${"), j):
                    return None
                j += 1
            if j >= n:
                return None
            quoted.append((i, j + 1))
            plain = False
            i = j + 1
        elif command.startswith("$'", i):
            # `$'…'` has escape rules of its own (`\'` does not close it): `echo
            # $'\'' ; <verb> ; echo $'\''` RAN the verb where POSIX quoting reads
            # it as arguments. Read with those rules, it is a quoted word (#1057).
            j = i + 2
            while j < n and command[j] != "'":
                j += 2 if command[j] == "\\" else 1
            if j >= n:
                return None
            quoted.append((i, j + 1))
            plain = False
            i = j + 1
        elif ch == "`" or command.startswith('$"', i):
            return None
        else:
            plain = plain and ch != "$"
            i += 1
    return _Word(command[start:i], tuple(quoted), plain), i


def _lex(command: str) -> tuple[list[_Simple], list[tuple[int, int]]] | None:
    """*command* as its simple commands plus recognised comment spans, in order,
    or None when any part is unmodelled."""
    simples: list[_Simple] = []
    comments: list[tuple[int, int]] = []
    words: list[_Word] = []
    redirects: list[tuple[str, str, _Word]] = []
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch in " \t":
            i += 1
            continue
        if command.startswith("\\\n", i):
            i += 2
            continue
        if ch == "#":
            # A `#` that starts a word opens a comment to the end of the line —
            # measured in bash, zsh, an interactive zsh with no rc, and bash with
            # `interactive_comments` off. The newline stays: it ends a statement.
            end = command.find("\n", i)
            comments.append((i, n if end < 0 else end))
            i = n if end < 0 else end
            continue
        if ch in "()`":
            return None
        op = ""
        if ch == "\n":
            op = ch
        elif ch == ";":
            op = ";;" if command.startswith(";;", i) else ch
        elif command.startswith(("&&", "||", "|&"), i):
            op = command[i : i + 2]
        elif ch == "|" or (ch == "&" and not command.startswith("&>", i)):
            op = ch
        if op:
            simples.append(_Simple(words, redirects, op))
            words, redirects = [], []
            i += len(op)
            continue
        io = ""
        if ch not in "<>&":
            read = _read_word(command, i)
            if read is None:
                return None
            word, i = read
            if not (word.plain and word.text.isdigit() and command[i : i + 1] in ("<", ">")):
                words.append(word)
                continue
            io = word.text
        redirect = next(
            (candidate for candidate in _REDIRECT_OPS if command.startswith(candidate, i)), ""
        )
        if not redirect:
            return None
        i += len(redirect)
        while i < n and command[i] in " \t":
            i += 1
        read = _read_word(command, i)
        # An operator with no word after it is refused, and this is also what
        # refuses a heredoc: `<<` is not in `_REDIRECT_OPS`, so it reads as `<`
        # whose target would start with the second `<`.
        if read is None or not read[0].text:
            return None
        target, i = read
        redirects.append((io, redirect, target))
    simples.append(_Simple(words, redirects, ""))
    return simples, comments


def _dequote(text: str) -> str:
    return text.replace("'", "").replace('"', "").replace("\\", "")


def _command_name(simple: _Simple) -> str:
    """The raw word that names what runs, past assignments and `builtin`/`command`."""
    prefixed = False
    for word in simple.words:
        if not prefixed and _ASSIGN_PREFIX.match(word.text):
            continue
        if word.plain and word.text in _COMMAND_PREFIXES:
            prefixed = True
            continue
        if prefixed and word.plain and word.text.startswith("-"):
            continue
        return word.text
    return ""


def _ends_analysis(simple: _Simple) -> bool:
    """True when *simple* changes what the rest of the command means.

    That is a compound-command word or brace group, a name rebinding (see
    `_UNMODELLED_NAMES`), or a name this cannot read at all. `$cmd` could be
    any of them, and so could a glob: with a file named `hash` in the directory,
    `h?sh -p /bin/sh grep` rebinds `grep` in bash and zsh, and `al?as` did the
    same to `echo` in zsh (PR #1002 review).
    """
    name = _command_name(simple)
    return (
        "$" in name
        or name.startswith(("{", "}"))
        or _GLOB_NAME.search(name) is not None
        or _dequote(name) in _UNMODELLED_NAMES
    )


def _head(word: _Word) -> str:
    """The command a plain first word names, or "" when which one is not certain."""
    if not word.plain:
        return ""
    directory, slash, name = word.text.rpartition("/")
    if not slash or directory in _SYSTEM_BIN_DIRS:
        return name
    return ""


def _flows_onward(simple: _Simple) -> bool:
    """True when this command's standard output can reach anything but the
    terminal or /dev/null.

    THE EXEMPTION IS FOR PROSE THAT IS DISPLAYED OR SEARCHED, NOT PROSE THAT IS
    SENT. `echo "terraform destroy" | sh` executes it; so does `printf … | bash`
    and `grep -rn … script.sh | sh`. A redirection counts wherever it is written
    — before the prose as much as after it — and so does a descriptor duplicated
    onto a stream that already points at a file: `echo "…" 2>run.sh >&2` staged
    the script. Plumbing that cannot carry the prose does not count: `2>&1`,
    `>&2`, `2>err.log` and anything sent to /dev/null leave stdout where it was.

    Upstream only: the last stage of `cat runbook.md | grep -c '…'` sends its
    output nowhere, so it stays exempt. And a descriptor that has pointed at a
    file STAYS a file, because zsh's MULTIOS writes `echo "…" >run.sh >/dev/null`
    to both — the last redirection does not win there.
    """
    if simple.then in ("|", "|&"):
        return True
    where = {"1": "tty", "2": "tty"}

    def point(fd: str, dest: str) -> None:
        if where.get(fd) != "file":
            where[fd] = dest

    for io, op, target in simple.redirects:
        value = target.text if target.plain else ""
        dest = "null" if value == "/dev/null" else "file"
        if op == "<<<":
            continue
        if op in ("<", "<>"):
            if (io or "0") in where:
                point(io or "0", "null" if op == "<" else dest)
        elif op in ("<&", ">&"):
            fd = io or ("0" if op == "<&" else "1")
            if value in where:
                point(fd, where[value])
            elif value == "-":
                point(fd, "null")
            elif op == ">&" and not io and value and not value.isdigit():
                point("1", dest)  # `>&word` is `&>word`
                point("2", dest)
            else:
                point(fd, "file")
        elif op.startswith("&>"):
            point("1", dest)
            point("2", dest)
        else:
            point(io or "1", dest)
    return where["1"] not in ("tty", "null")


def _message_regions(simple: _Simple) -> list[tuple[int, int]]:
    """The quoted regions of *simple* that are a commit message's value.

    Scoped to the VCS verbs and subcommands that TAKE a message, as the secret-read
    path already is. The regex this replaces accepted `-m` after ANY head, and
    `bash -c -m "…"` and `sh -c -m "…"` both RAN the "message" it blanked.
    """
    words = simple.words
    at = 0
    while at < len(words) and _ASSIGN_PREFIX.match(words[at].text):
        at += 1
    if at >= len(words) or _head(words[at]) not in _MESSAGE_VERBS:
        return []
    tokens = [word.text if word.plain or word.text.startswith("-") else "" for word in words]
    if not _takes_message(tokens, at):
        return []
    attached = tuple(f"{flag}=" for flag in _MESSAGE_FLAGS)
    regions: list[tuple[int, int]] = []
    for k in range(at + 1, len(words)):
        word = words[k]
        if word.plain and word.text in _MESSAGE_FLAGS:
            if k + 1 < len(words):
                regions.extend(words[k + 1].quoted)
        elif word.text.startswith(attached):
            regions.extend(word.quoted)
    return regions


# Operators that would start, pipe or substitute a command if `#` were a word.
_COMMENT_CAN_RUN = re.compile(r"[;&|()`<>]")


def _prose_spans(command: str) -> list[tuple[int, int]]:
    """Character spans in *command* that are prose or a shell comment, not execution.

    A quoted region qualifies when the simple command holding it is headed by a
    command that only prints or searches its arguments, or when it is the value
    of a commit-message flag — AND that command's output goes nowhere but the
    terminal. Prose that is DISPLAYED or SEARCHED is inert; prose that is SENT
    somewhere is not. A comment span (#1061) is inert only when it holds no
    shell operator: a shell that reads `#` as a word would run what follows a
    separator, so such a span stays visible. Without one, a blind dequote of
    the raw text would rejoin a quote-split phrase sitting inside it.
    """
    lexed = _lex(command)
    if lexed is None:
        return []
    simples, comments = lexed
    if any(_ends_analysis(simple) for simple in simples):
        return []
    # #1063 review: `#` is a plain word under bash `interactive_comments` off or
    # zsh's `histchars`, so a comment holding a separator stays in the views.
    spans = [c for c in comments if not _COMMENT_CAN_RUN.search(command, *c)]
    for simple in simples:
        if not simple.words or _flows_onward(simple):
            continue
        head = _head(simple.words[0])
        grep_args = _git_grep_args(simple)
        if head in _PROSE_HEADS or head in _PROSE_PATTERN_TOOLS:
            if head == "printf" and any(
                _dequote(word.text).startswith("-v") for word in simple.words[1:]
            ):
                # `printf -v c "…"; $c` RAN in bash: the text became a command
                # through the variable, and no verb was left anywhere to see.
                # Dequoted, because `printf '-v' c "…"` assigns too (#1043).
                continue
            regions = [region for word in simple.words[1:] for region in word.quoted]
        elif grep_args is not None:
            # #1039: `git grep <pattern>` searches its argument like `grep`, so a
            # quoted pattern naming a destructive verb is prose, not the verb —
            # `git grep 'terraform destroy'` asked before this. `git grep` never
            # executes its own quoted arguments; the one exec path,
            # `-O`/`--open-files-in-pager`, is caught by `_git_grep_runs_pager`
            # whether its value is blanked or not.
            regions = [region for word in simple.words[grep_args:] for region in word.quoted]
        else:
            regions = _message_regions(simple)
        spans.extend(
            (start, end)
            for start, end in regions
            if not _HAS_SUBSTITUTION.search(command, start, end)
        )
    return spans


def _without_prose(command: str) -> str:
    """*command* with prose spans blanked to spaces, same length and offsets."""
    spans = _prose_spans(command)
    if not spans:
        return command
    chars = list(command)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = " "
    return "".join(chars)


# ── #1035: a search tool can run a program ──────────────────────────────────
# `rg --pre CMD` runs `CMD PATH` per file, so `rg --pre terraform x destroy`
# runs `terraform destroy` — a verb no deny rule can see, blanked or not. Where
# the program's arguments come from differs per tool (a PATH, a `%`, a shell),
# so modelling it would be one fail-open per modelling error. Refusing the flag
# models nothing. From each tool's --help: rg 14 (`--pre`, `--hostname-bin`),
# ugrep 7 (`--filter`, `--pager`, `--view`, `--config`/`---`), ag and ack
# (`--pager`; ack `--ackrc`). ag's getopt_long and ack's Getopt::Long accept
# an unambiguous prefix (`--pag`); rg and ugrep rejected one when run.
_SEARCH_EXEC_FLAGS: dict[str, frozenset[str]] = {
    "rg": frozenset({"pre", "hostname-bin"}),
    "ag": frozenset({"pager"}),
    "ack": frozenset({"pager", "ackrc"}),
    **{
        name: frozenset({"filter", "pager", "view", "config"})
        for name in ("grep", "egrep", "fgrep", "ugrep", "ug")
    },
}
_SEARCH_ABBREVIATES = frozenset({"ag", "ack"})


#: Heads that run a later word as a command. Their own option grammars are NOT
#: modelled (`env -C DIR`, `sudo -u USER`, `timeout 5`): every tool name after
#: one is checked instead, so an option argument spelling a tool name cannot
#: hide the real one (PR #1037 review).
_RUNS_A_COMMAND = frozenset(
    {
        *_COMMAND_PREFIXES,
        "env",
        "exec",
        "nice",
        "nohup",
        "time",
        "sudo",
        "doas",
        "timeout",
        "xargs",
        "stdbuf",
        "setsid",
        "ionice",
        "chrt",
        "taskset",
        "caffeinate",
        "unbuffer",
        "watch",
    }
)


def _tool_flag(tool: str, words: list[str]) -> str | None:
    """The exec-capable flag *tool* was given in *words*, or None."""
    for word in words:
        if word == "--":
            return None  # what follows is a pattern or a path
        if word.startswith("---") and "config" in _SEARCH_EXEC_FLAGS[tool]:
            return f"{tool} ---"  # ugrep's short spelling of --config
        if word.startswith("--"):
            name = word[2:].partition("=")[0]
            for flag in _SEARCH_EXEC_FLAGS[tool]:
                if name == flag or (
                    tool in _SEARCH_ABBREVIATES and len(name) > 1 and flag.startswith(name)
                ):
                    return f"{tool} --{flag}"
    return None


def _search_runs_program(command: str) -> str | None:
    """The search tool in *command* that was given a program to run, or None.

    Only the word in command position counts, so `echo rg --pre x` runs no
    search tool. Past a wrapper, every tool name is a candidate.
    """
    for statement in _statements(command):
        leaves: list[list[str]] = [[]]
        for word in statement:
            if _is_pipe(word):
                leaves.append([])
            else:
                leaves[-1].append(word)
        for leaf in leaves:
            at = _verb_index(leaf)
            if at >= len(leaf):
                continue
            names = [word.rsplit("/", 1)[-1] for word in leaf]
            starts = [at]
            if names[at] in _RUNS_A_COMMAND:
                starts = range(at + 1, len(leaf))
            for k in starts:
                if names[k] in _SEARCH_EXEC_FLAGS:
                    found = _tool_flag(names[k], leaf[k + 1 :])
                    if found:
                        return found
                if names[k] == "git":
                    found = _git_grep_runs_pager(leaf[k + 1 :])
                    if found:
                        return found
    return None


# ── #1039: `git grep` opens its hits in a program ───────────────────────────
# `git grep -O<pager>` / `--open-files-in-pager[=<pager>]` runs <pager> FILE for
# every match, so `git grep -O'terraform destroy' needle` runs `terraform
# destroy`; the pager value is not a quoted PATTERN, so blanking never touched
# it, and no deny rule saw the verb. All four spellings ran a `touch` payload:
# `-O'…'`, the clustered `-iO'…'`, `--open-files-in-pager='…'`, and git's
# unambiguous long-option abbreviation `--op='…'`. Short `-o` (only-matching)
# is lowercase and inert; the flag we key on is the UPPER-case `O` and the long
# name it abbreviates. Refusing the flag models nothing, as with the search
# tools above.
_GIT_GREP_PAGER_LONG = "open-files-in-pager"
_GIT_GREP_SHORT_PAGER = re.compile(r"-[A-Za-z0-9]*O")  # `-1O` too: -NUM is context


#: Wrappers that exec the rest of their argv as given — no shell, no search path of
#: their own — with the only options modelled for each (#1043). Any other option
#: keeps the prompt: `env -S'bash -c "eval \$3" x' git grep '…'` RAN the pattern.
_EXEC_WRAPPERS: dict[str, frozenset[str]] = {
    "builtin": frozenset(),
    "command": frozenset({"-p"}),
    "env": frozenset({"-", "-i", "--ignore-environment"}),
    "nice": frozenset(),
    "nohup": frozenset(),
    "noglob": frozenset(),
    "nocorrect": frozenset(),
    "time": frozenset({"-p"}),
}
#: An assignment that decides which `git` runs (zsh ties `path` to PATH).
_SEARCH_PATH_ASSIGN = re.compile(r"(?:PATH|path)(?:\[[^\]]*\])?\+?=")


def _git_grep_args(simple: _Simple) -> int | None:
    """Index of the first argument `git grep` itself takes in *simple*, or None.

    Past `VAR=…` prefixes, `_EXEC_WRAPPERS` and git's global options. Only the
    words from here on are prose: `git -c core.fsmonitor='…' grep` and a
    GIT_CONFIG_* prefix RAN their value, and with a PATH prefix a planted
    ./git ran the pattern (#1043).
    """
    words = simple.words
    i = 0
    while i < len(words):
        if _ASSIGN_PREFIX.match(words[i].text):
            if _SEARCH_PATH_ASSIGN.match(words[i].text):
                return None
            i += 1
            continue
        options = _EXEC_WRAPPERS.get(_head(words[i]))
        if options is None:
            break
        i += 1
        while i < len(words) and words[i].plain and words[i].text in options:
            i += 1
    if i >= len(words) or _head(words[i]) != "git":
        return None
    i += 1
    while i < len(words):
        text = words[i].text
        if text in _VCS_GLOBAL_ARG_FLAGS:
            i += 2
            continue
        if text.startswith("-"):
            i += 1
            continue
        break
    return i + 1 if i < len(words) and words[i].text == "grep" else None


def _git_grep_runs_pager(after_git: list[str]) -> str | None:
    """The `git grep -O`/`--open-files-in-pager` flag in *after_git*, or None.

    *after_git* is the word list following the `git` command word (its own
    global options, the `grep` subcommand, then grep's arguments).
    """
    words = after_git
    i = 0
    while i < len(words):
        word = words[i]
        if word in _VCS_GLOBAL_ARG_FLAGS:  # `git -C DIR grep …`, `git -c k=v grep …`
            i += 2
            continue
        if word.startswith("-"):  # `--git-dir=…` and other attached globals
            i += 1
            continue
        break
    if i >= len(words) or words[i] != "grep":
        return None
    for word in words[i + 1 :]:
        if word == "--":  # what follows is a pattern or a path, not a flag
            break
        if word.startswith("--"):
            name = word[2:].partition("=")[0]
            if len(name) >= 2 and _GIT_GREP_PAGER_LONG.startswith(name):
                return "git grep --open-files-in-pager"
            if len(name) >= 5 and "textconv".startswith(name):  # runs the diff driver's filter
                return "git grep --textconv"
        elif word.startswith("-") and _GIT_GREP_SHORT_PAGER.match(word):
            return "git grep -O"
    return None


# ── #1043: a name split by quoting is still the name ────────────────────────
# The shell joins adjacent quoted pieces, so `RIPGREP_'CONFIG_PATH'=…` sets the var
# and `terraform "destroy"` runs the verb; no text check saw either. One mechanism
# instead of a regex per spelling: every check also reads the command after quote
# removal. A matching VIEW, never a parse — it only adds matches, so a rough edge
# can flag a harmless command (ask, or deny in an autonomous mode such as
# bypassPermissions) but never clears one the raw text flags.
_ANSI_C_QUOTED = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.DOTALL)
_ANSI_C_ESCAPE = re.compile(
    r"\\(?:x([0-9A-Fa-f]{1,2})|u([0-9A-Fa-f]{1,4})|U([0-9A-Fa-f]{1,8})|([0-7]{1,3})|c(.)|(.))",
    re.DOTALL,
)
_ANSI_C_LETTERS = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}


def _ansi_c_escape(escape: re.Match[str]) -> str:
    hexa, short, long, octal, control, other = escape.groups()
    code = hexa or short or long
    if code:
        decoded = chr(min(int(code, 16), 0x10FFFF))
    elif octal:
        decoded = chr(int(octal, 8) & 0xFF)
    elif control is not None:
        decoded = chr(ord(control) & 0x1F)
    else:
        decoded = _ANSI_C_LETTERS.get(other, other)
    # A word or environment name cannot hold a NUL byte, so bash drops a
    # decoded NUL from it entirely: `terraform des$'\x00'troy` reaches bash as
    # `terraform destroy`. Elide it here too (\0, \x00, \u0000, octal and
    # control forms all land here), or the dequoted view still reads
    # `des\0troy` and evaluate() never recognizes the spliced word (#1043
    # review; PI-881 bump).
    return "" if decoded == "\x00" else decoded


def _dequoted(command: str) -> str:
    """*command* after quote removal: `$'…'` decoded, quotes and backslashes gone."""
    text = command.replace("\\\n", "")
    text = _ANSI_C_QUOTED.sub(lambda m: _ANSI_C_ESCAPE.sub(_ansi_c_escape, m[1]), text)
    text = text.replace('$"', '"')
    return re.sub(r"\\(.)|['\"]", lambda m: m[1] or "", text, flags=re.DOTALL)


# ── #1039: exec flags injected through a config file named in the environment ─
# `RIPGREP_CONFIG_PATH=cfg rg` runs `--pre=CMD` from cfg; `ACKRC=cfg ack` runs its
# `--pager`. Three review rounds each found another way to set the var (after
# `env`, `+=`, `VAR=… export`, `declare -x`, inside `bash -c '…'`), so this is
# presence-based, not a shell model: the var SET anywhere in the command beside
# its tool named anywhere asks. Order is ignored on purpose — fail closed.
_CONFIG_ENV: dict[str, str] = {"RIPGREP_CONFIG_PATH": "rg", "ACKRC": "ack"}


def _config_env_runs_program(views: tuple[str, ...]) -> str | None:
    """A config-path env var set beside the search tool that reads it, or None.

    *views* are the prose-blanked command, raw and dequoted (#1043); a match in
    either counts.
    """
    for var, tool in _CONFIG_ENV.items():
        set_at = re.compile(rf"(?<![A-Za-z0-9_]){var}\+?=")
        exported = re.compile(
            rf"\b(?:export|declare|typeset|local|readonly)\b[^;&|\n]*(?<![A-Za-z0-9_]){var}\b"
        )
        tool_at = re.compile(rf"(?<![A-Za-z0-9_.-]){tool}(?![A-Za-z0-9_.-])")
        set_here = any(set_at.search(view) or exported.search(view) for view in views)
        named = any(tool_at.search(view) for view in views)
        if named and (set_here or _inherited_config_runs(var, tool)):
            return var
    return None


_CONFIG_READ_LIMIT = 64 * 1024


def _inherited_config_runs(var: str, tool: str) -> bool:
    """*var* came in with the session and its config file holds an exec flag.

    The file is read, so a benign inherited config does not make every search ask;
    an unreadable one fails closed. Both tools take one argument per line.
    """
    path = os.environ.get(var)
    if not path:
        return False
    cfg = Path(path).expanduser()
    try:
        if not cfg.is_file():  # a FIFO or /dev/zero would hang every command
            return True
        with cfg.open("rb") as handle:
            text = handle.read(_CONFIG_READ_LIMIT + 1)
    except OSError:
        return True
    if len(text) > _CONFIG_READ_LIMIT:
        return True
    lines = text.decode("utf-8", errors="replace").splitlines()
    args = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    return _tool_flag(tool, args) is not None


# ── #1039: PS4 command substitution runs under xtrace ────────────────────────
# With tracing on, the shell expands PS4 before every command, so a `$(…)` in PS4
# runs each time: `PS4='$(id)'; set -x; echo hi` runs `id` (reproduced in bash and
# zsh). Four review rounds found new ways to set PS4 (`+=`, `PS4[0]=`, inside
# `bash -c`), so nothing is parsed: PS4 named anywhere, tracing enabled anywhere,
# and anything in the command able to run a program — together they ask.
_PS4_NAMED = re.compile(r"(?<![A-Za-z0-9_])PS4(?![A-Za-z0-9_])")
_XTRACE_ON = re.compile(
    r"(?i)\bx_?trace\b|\bset\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*x|"
    r"(?<![A-Za-z0-9_])(?:ba|z|k|da)?sh\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*x"
)
# `$NAME` / `${NAME}` / `${NAME:-word}` read a variable and run nothing; any other
# `$` or backtick (`$(…)`, `$((a[$(…)]))`, `${!ref}`, `${a[…]}`) may run a program.
_PLAIN_PARAM = re.compile(
    r"\$\{[A-Za-z_][A-Za-z0-9_]*(?:[:#%/^,]?[-=?+]?[A-Za-z0-9_ .:/+-]*)?\}"
    r"|\$[A-Za-z_][A-Za-z0-9_]*|\$[0-9#?$!*@-]"
)


def _trace_runs_program(views: tuple[str, ...]) -> str | None:
    """PS4 named beside xtrace and a construct able to run a program, or None.

    *views* are the prose-blanked command, raw and dequoted (#1043), so
    `bash -c '…'` bodies and lines that do not tokenise are seen as well.
    """
    named = any(_PS4_NAMED.search(view) for view in views)
    if not (named and any(_XTRACE_ON.search(view) for view in views)):
        return None
    rests = [_PLAIN_PARAM.sub("", view) for view in views]
    return "PS4 with set -x" if any("$" in rest or "`" in rest for rest in rests) else None


# ── Secret-file exposure (PI-893) ───────────────────────────────────────────
# The scaffold's secret machinery is write/commit-oriented: gitleaks and the
# pre-commit gate stop you COMMITTING a secret, .gitignore stops you tracking
# one, and this table stopped you destroying things. Nothing stopped `cat .env`,
# so the values land in the transcript — which is re-sent on every following
# turn and outlives the session that read them.
#
# `permissions.deny` in the scaffolded settings.json closes the Read TOOL. It
# cannot close Bash, because a permission rule matches a tool's arguments and
# Bash's argument is one opaque string. That is this check's job.
_SECRET_PATH = re.compile(
    r"""
    (?:^|[\s=:'"(<@])                 # a token boundary, never mid-word
                                       # `@` because `curl -d @.env` is exfil
    (?:[\w.@~${}-]*/)*                 # optional directory prefix, incl. an
                                       # expansion: `$PWD/.env`, `${HOME}/.netrc`
                                       # and `"$HOME/.ssh/id_rsa"` all reach a
                                       # real file and all missed without this
    (?:
        # `.env`, `<stem>.env` and `.env.<anything>` EXCEPT the four
        # documented example spellings. Those are committed, value-free, and
        # the file an agent reads to learn which variables exist — denying
        # them would be a false positive on the safe half of the convention.
        #
        # THE STEM IS NOT DECORATION. Without `[\w-]*` the check missed
        # `prod.env`, `staging.env` and `my.env.local` — three ordinary
        # spellings of the file it exists to guard. A mutation run found it:
        # deleting the leading token boundary changed no test result, which
        # meant nothing pinned that part of the pattern, which meant nobody
        # had checked what it excluded. direnv's `.envrc` is here for the
        # same reason — it routinely holds `export AWS_SECRET_...`.
        [\w-]*\.env(?:\.(?!example|sample|template|dist)[\w-]+)*(?![\w.-])
      | \.envrc(?![\w.-])
      | id_(?:rsa|dsa|ecdsa|ed25519)(?![\w.-])
      | \.(?:netrc|pgpass|npmrc)(?![\w.-])
        # `key` covers the `server.key` / `tls.key` convention. It was absent
        # while `.gitignore` in every scaffolded repo already lists `*.key`,
        # so the tree classified the file as a secret and the guard read it
        # out loud: measured, `cat server.key` and `cat tls.key` were ALLOWED
        # while `cat id_rsa` and `cat secrets.pem` asked. A false positive
        # here costs one confirmation, which is the cheap side of the trade.
      | [\w.-]*\.(?:pem|p12|pfx|jks|keystore|key)(?![\w.-])
      | [\w.-]*(?:service[-_]?account|credentials|client[-_]secret)[\w.-]*\.json(?![\w.-])
      | secrets?/[\w./-]+
    )
    """,
    re.VERBOSE,
)

# Commands that cannot put a file's CONTENTS anywhere: they act on the name,
# the metadata or the directory entry. `rm .env` is not exposure — it may be
# unwise, but the values do not reach the transcript, and flagging it would nag
# on the cleanup that follows every scaffold demo.
#
# `echo`/`printf` are here for one specific daily command: `echo ".env" >>
# .gitignore`. WRITING a secret file is also not exposure — the values came
# from the session, they did not enter it.
_EXPOSURE_SAFE_VERBS = frozenset(
    {
        "ls",
        "ll",
        "stat",
        "file",
        "test",
        "[",
        "[[",
        "touch",
        "mkdir",
        "rmdir",
        "rm",
        "chmod",
        "chown",
        "ln",
        "echo",
        "printf",
        "basename",
        "dirname",
        "find",
        "which",
        "type",
    }
)

# Tools whose FIRST non-flag argument is a pattern or a program, not a path.
# `grep -rn ".env" src/` searches for the string and opens nothing named by it;
# scanning that argument as a path made a routine search prompt. `sed`/`awk`
# take a script first and their paths after, so only the first is skipped.
_PATTERN_FIRST_ARG = frozenset(
    {"grep", "egrep", "fgrep", "rg", "ag", "ack", "sed", "awk", "gawk", "nawk"}
)


# A command substitution hides a whole command inside another one, and the
# outer verb is the one the exemption looks at. `echo "$(cat .env)"` prints the
# secret while presenting `echo` as its head (PR #942 review, P1). Both
# spellings, one nesting level — a deeper nest is unusual enough to leave to
# the ask/deny posture rather than pretend to a parser.
_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")

# `find` is exempt only when it is doing what makes it exempt. With an action,
# or piped into something that reads, it becomes the READER'S argument list:
# `find . -name .env -exec cat {} +` and `find . -name .env | xargs cat` both
# print the file, and neither stage looks dangerous on its own — the path is in
# the find, the verb is downstream (PR #942 review, P1).
#
# NOT "any pipe": `find . -name .env | wc -l` counts matches and reads nothing,
# and a guard that prompts on it is the false positive that gets guards turned
# off. The downstream verb has to actually consume contents.
# SEPARATORS AS TOKENS, NOT AS CHARACTERS IN A STRING. `shlex` with
# punctuation_chars=True yields each shell operator as its own token and leaves
# an operator that appeared inside quotes buried in the token it belongs to, so
# `echo '.env |& xargs cat'` tokenizes to two tokens and nothing splits. Getting
# this from the tokenizer also retires the redirection lookaround the regex
# needed: shlex already emits `>&` and `&>` as distinct tokens, so a bare `&` is
# unambiguously a separator and `2>&1` cannot be mistaken for one.
_PIPE_TOKENS = frozenset({"|", "|&"})
# A NEWLINE IS A STATEMENT SEPARATOR, and shlex does not think so by default:
# it is whitespace, so `cmd1\ncmd2` came back as ONE statement with `cmd2` read
# as an argument to `cmd1`. That is not cosmetic — it reopened the bypass this
# module exists to close. Measured before the fix, with a dotenv path:
#     ls -la<newline>cat <dotenv>     -> allow   (WRONG, a read got through)
#     cat README.md<newline>echo …    -> ask     (WRONG, the other direction)
# The first is the one that matters: two lines merge, the head becomes `ls`
# (exposure-safe), no reader appears in `heads` because `cat` is now an
# argument, the producer is exempted, and the file is read. Caught in review of
# PR #953 by Copilot; the regex this replaced listed `\n` explicitly and I
# dropped it.
_BREAK_TOKENS = frozenset({"&&", "||", ";", "&", "\n"})
# A RUN OF PUNCTUATION IS ONE TOKEN, AND EXACT MEMBERSHIP MISSED EVERY RUN.
# PR #972 review, Codex P1, reproduced against this file before fixing:
#     ls -la;<newline>cat <dotenv>      -> allow   (token was ";\n")
#     ls -la &&<newline>cat <dotenv>    -> allow   (token was "&&\n")
#     ls -la<newline><newline>cat …     -> allow   (token was "\n\n")
# `punctuation_chars` makes shlex COALESCE adjacent punctuation into a single
# token, so the ordinary shell formatting a person actually types — an operator
# at end of line, or a blank line between statements — produced a token that is
# not in the set above, the statements merged, and #953's newline fix was only
# ever load-bearing for the one spelling its test used. `is_break` therefore asks
# whether a token is made ENTIRELY of separator characters rather than whether it
# equals one of them. `>` and `<` are deliberately excluded: they are in
# _PUNCTUATION_CHARS so that `2>&1` tokenizes correctly, but a redirection does
# not start a new statement, and treating `>` as a break would split
# `cat <dotenv> > out` into two leaves and lose the read.
# A PIPE IS NOT A STATEMENT BREAK, and the first draft of this fix forgot it —
# 26 assertions went red in one run, every one of them a `producer | xargs cat`
# case. That direction is the dangerous one: splitting a pipeline into separate
# leaves means the producer's secret path and the downstream reader are never
# considered together, `ls <dotenv>` reads as an exposure-safe verb on its own,
# and the read is ALLOWED. Under-splitting merges statements (the bug above);
# over-splitting severs data paths. Both fail open, so this has to be accurate
# rather than conservative in either direction.
_BREAK_CHARS = frozenset(";&|\n")


def _is_pipe(token: str) -> bool:
    """True when *token* is a pipe, INCLUDING a run that swallowed the newline.

    `_is_break` already knew that `|<newline>` continues a pipeline and returns
    False for it. Nothing downstream agreed: `_statement_exposes` asked
    `token in _PIPE_TOKENS`, which a coalesced `"|\n"` fails, so the token was
    appended to the leaf as an ordinary WORD. The pipeline then had one leaf,
    `heads` held only the producer, no reader verb was visible, the producer was
    exempted and the read went through. Reproduced against this file before
    fixing (Codex P1 on studio#12, the same shape #972 fixed for the break side):

        ls <dotenv> |<newline>xargs cat   -> allow

    Break wins over pipe in a mixed run: `|<newline>;` ends the pipeline, and
    saying otherwise would merge two statements — the hole `_BREAK_TOKENS` closes.
    """
    if token in _PIPE_TOKENS:
        return True
    if not token or not all(ch in _BREAK_CHARS for ch in token):
        return False
    rest = token.replace("&&", "\x00").replace("||", "\x00").replace("|&", "\x01")
    if ";" in rest or "\x00" in rest or "&" in rest:
        return False
    return "|" in rest or "\x01" in rest


def _is_break(token: str) -> bool:
    """True when *token* is a run of punctuation that separates STATEMENTS.

    Exact matches are decided by the two sets first; the rest of this handles a
    coalesced run like ``";\\n"`` or ``"&&\\n"``, which is what `punctuation_chars`
    actually emits for ordinary shell formatting.
    """
    if not token or token in _PIPE_TOKENS:
        return False
    if token in _BREAK_TOKENS:
        return True
    # Anything carrying a character outside the separator set — a redirection
    # (`2>&1`, `&>`), a subshell paren, a word — is not a separator run at all.
    if not all(ch in _BREAK_CHARS for ch in token):
        return False
    # Peel the two-character operators so a LONE `&` (background, a real break)
    # can be told apart from the `&` inside `&&` or `|&`.
    rest = token.replace("&&", "\x00").replace("||", "\x00").replace("|&", "\x01")
    if ";" in rest or "\x00" in rest or "&" in rest:
        return True
    # Only pipes and newlines remain. A NEWLINE AFTER A PIPE CONTINUES THE
    # PIPELINE — `ls |<newline>cat` is one command, not two — so a run is a break
    # only when it carries a newline and no pipe at all.
    return "\n" in rest and "|" not in rest and "\x01" not in rest


# Newline added to shlex's punctuation set, and removed from its whitespace, so
# it is EMITTED as a token instead of being discarded. Doing it through the
# tokenizer rather than by splitting the string on newlines first is what keeps
# a QUOTED newline intact: `echo 'a<newline>b'` stays one token, where a
# pre-split would tear it in half and leave both halves unparsable.
_PUNCTUATION_CHARS = "();<>|&\n"
# The token AFTER one of these is prose, not a path. Replaces a regex that
# required the quotes to still be in the string — which they are not, after
# tokenizing — and it now also covers an unquoted message the regex never saw.
_MESSAGE_FLAGS = frozenset({"-m", "-am", "--message"})
# ONLY WHERE `-m` ACTUALLY MEANS A MESSAGE, WHICH IS NOT EVERYWHERE.
# PR #972 review, Codex P1, reproduced against this file before fixing:
#     less -m <dotenv>   -> allow
# The elision was unconditional, so ANY `-m` swallowed the token after it. In
# `less` that flag is a display mode and takes no argument, so the elision ate
# the filename instead and the leaf became `['less']` — no path left for
# _SECRET_PATH to match. The same shape reaches `sort -m`, `uniq -m`, `chmod -R`
# style flags in any tool that spells a boolean `-m`. Scoping the carve-out to
# the commands that HAVE commit messages keeps what #953 bought (`git commit -m
# do-not-cat-<dotenv>` stays quiet) and returns every other command to the
# ordinary path. An exemption is only ever as safe as the set it applies to.
_MESSAGE_VERBS = frozenset({"git", "hg", "svn", "bzr", "jj"})
# AND ONLY UNDER A SUBCOMMAND THAT ACTUALLY TAKES A MESSAGE. Scoping the
# carve-out to the VCS verb fixed `less -m` and left the same shape one level
# down: `git diff -m` selects how merge commits are shown and takes NO argument,
# so the elision ate the path after it. Reproduced against this file before
# fixing (Codex P1 on estate#38), with a modified tracked dotenv in the tree:
#     git diff -m <dotenv>   -> allow   (the diff prints the secret)
# The previous round wrote "an exemption is only ever as safe as the set it
# applies to" and then applied it to every subcommand there is. An UNKNOWN
# subcommand does not elide: the guard stays strict, which is the direction that
# costs a false positive rather than a bypass.
_MESSAGE_SUBCOMMANDS = frozenset(
    {
        # git
        "commit",
        "tag",
        "merge",
        "revert",
        "cherry-pick",
        "stash",
        "notes",
        # hg / bzr
        "ci",
        "backout",
        "graft",
        # svn — every subcommand that takes a log message
        "copy",
        "cp",
        "delete",
        "del",
        "remove",
        "rm",
        "import",
        "mkdir",
        "move",
        "mv",
        "rename",
        "ren",
        "lock",
        # jj
        "describe",
        "desc",
        "new",
        "split",
        "squash",
    }
)
# Global flags that consume the NEXT token, so the subcommand is not simply the
# first non-flag word: `git -C /path commit -m ...` must still find `commit`.
# The long spellings are here because the short ones alone cost a false positive:
# `jj --repository /repo describe -m "<prose>"` read `/repo` as the subcommand,
# found no message subcommand, and scanned the commit message as a path (Codex P2
# on #979). Any list like this is incomplete by construction, which is why it is
# the second of two defences rather than the only one.
_VCS_GLOBAL_ARG_FLAGS = frozenset(
    {
        "-C",
        "-c",
        "-R",
        "-d",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--exec-path",
        "--cwd",
        "--repository",
        "--directory",
        "--config",
        "--config-toml",
        "--config-dir",
        "--config-option",
        "--encoding",
        "--at-operation",
        "--at-op",
    }
)


def _takes_message(leaf: list[str], verb_at: int) -> bool:
    """True when a message-TAKING subcommand appears BEFORE the message flag.

    NOT "the first non-flag word", and NOT "any word in the leaf". Both are wrong
    in a way that matters, and they are wrong in opposite directions:

      * first-non-flag depends on knowing every global that eats its argument, and
        that list can never be complete. `jj --repository /repo describe` cost a
        false positive on exactly that gap.
      * any-word-in-the-leaf reads `git diff -m <dotenv> commit` as a commit,
        elides the path, and the diff prints the file. That one is a BYPASS, so it
        is the direction that decides the shape.

    Scanning only the tokens before the message flag gets both: an argument sitting
    AFTER `-m` can never masquerade as a subcommand, and a global's argument before
    it is harmless unless it happens to spell a subcommand — which the skip list
    above then covers. Two narrow defences rather than one wide one.
    """
    skip = False
    for token in leaf[verb_at + 1 :]:
        if skip:
            skip = False
            continue
        flag, sep, _ = token.partition("=")
        if token in _MESSAGE_FLAGS or (sep and flag in _MESSAGE_FLAGS):
            return False
        if token in _VCS_GLOBAL_ARG_FLAGS:
            skip = True
            continue
        if token.startswith("-"):
            continue
        if token in _MESSAGE_SUBCOMMANDS:
            return True
    return False


# A SHELL ASSIGNMENT PREFIX IS NOT THE COMMAND.
# `FOO=bar git commit -m "docs: describe .env handling"` puts `FOO=bar` in
# leaf[0], so the verb read as `FOO=bar`, `_MESSAGE_VERBS` did not match, the
# commit message was scanned as an ordinary argument and the line prompted
# (Codex P2 on #974). Every rule keyed on the verb had the same blind spot —
# the reader set, the exposure-safe set and the message carve-out alike.
# Skipping the prefixes cannot open a bypass: the assignment TOKENS stay in the
# list, so `FOO=<dotenv> cat x` still matches _SECRET_PATH on the value.
_ASSIGN_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")  # `+=`, `a[i]=` (#1039)


def _verb_index(leaf: list[str]) -> int:
    """Index of the verb in *leaf*, skipping `NAME=value` prefixes."""
    i = 0
    while i < len(leaf) and _ASSIGN_PREFIX.match(leaf[i]):
        i += 1
    return i if i < len(leaf) else 0


def _strip_comments(command: str) -> str:
    """Remove the comments a SHELL would remove, and only those.

    `#` opens a comment at the START OF A WORD and nowhere else, and quoting
    suppresses the rule outright. The three cases this has to keep apart:

        cat README#old <dotenv>      -> `#` is mid-word, nothing is a comment
        cat README.md # notes <dotenv-mention>  -> prose, dropped
        cat '#a' <dotenv>            -> `#a` is a FILENAME, nothing is dropped

    Clearing shlex's `commenters` (the #972 P1 fix) got the first case right
    and created the mirror-image false positive in the second, where the prose
    after a real `#` stayed in the token stream and its mention of a dotenv
    path prompted the operator (Codex P2 on #974). Doing it here instead of in
    shlex is what makes the third case safe: shlex reports no quoting, so a
    token-level rule would read `#a` as a comment opener and DISCARD the secret
    argument behind it — trading a false positive for a bypass.

    A newline ending a comment is KEPT. It separates statements, and swallowing
    it would merge the next command into this one, which is the exact hole
    `_BREAK_TOKENS` exists to close.
    """
    out: list[str] = []
    in_single = in_double = escaped = in_comment = False
    at_word_start = True
    for ch in command:
        if in_comment:
            if ch == "\n":
                in_comment = False
                out.append(ch)
                at_word_start = True
            continue
        if escaped:
            out.append(ch)
            escaped = False
            at_word_start = False
            continue
        if in_single:
            out.append(ch)
            in_single = ch != "'"
            continue
        if in_double:
            out.append(ch)
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            at_word_start = False
            continue
        if ch in "'\"":
            out.append(ch)
            in_single = ch == "'"
            in_double = ch == '"'
            at_word_start = False
            continue
        if ch == "#" and at_word_start:
            in_comment = True
            continue
        out.append(ch)
        at_word_start = ch.isspace() or ch in _PUNCTUATION_CHARS
    return "".join(out)


def _tokenize(command: str) -> list[str] | None:
    """Split *command* into shell tokens, or None when it cannot be parsed.

    None means unbalanced quotes. Callers fall back to the character split,
    which over-splits rather than under-splits: for a guard, keeping the old
    false positive on an unparsable command is the safe direction.
    """
    command = _strip_comments(command)
    lex = shlex.shlex(command, posix=True, punctuation_chars=_PUNCTUATION_CHARS)
    lex.whitespace_split = True
    # Newline must stop being whitespace or it is thrown away before punctuation
    # handling ever sees it, which is exactly how it stopped separating.
    lex.whitespace = lex.whitespace.replace("\n", "")
    # SHLEX THINKS `#` STARTS A COMMENT. BASH DOES NOT, MID-WORD.
    # PR #972 review, Codex P1, reproduced against this file before fixing:
    #     cat README#old <dotenv>   -> allow
    # shlex's default `commenters` is "#", so everything from that character to
    # end of line was DISCARDED — the tokenizer returned ['cat', 'README'] and
    # the secret argument simply did not exist as far as every later rule was
    # concerned. Bash only treats `#` as a comment at the start of a word, so an
    # unquoted `#` inside one is an ordinary character and the truncation is pure
    # loss. A guard that silently drops the rest of the command is the worst shape
    # available: it fails open and leaves no trace of what it dropped.
    # Real comments are already gone — `_strip_comments` above removed them with
    # the quoting context shlex cannot report at this level.
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


_FIND_ACTS = re.compile(r"\s-(?:exec|execdir|ok|okdir)\b")
_READER_VERBS = frozenset(
    {
        "xargs",
        "cat",
        "bat",
        "less",
        "more",
        "head",
        "tail",
        "nl",
        "tee",
        "strings",
        "xxd",
        "od",
        "grep",
        "egrep",
        "rg",
        "sed",
        "awk",
        "cut",
        "cp",
        "curl",
        "wget",
        "scp",
        "base64",
        "source",
    }
)


# WHAT REPLACES A SUBSTITUTION IS LOAD-BEARING, AND A SPACE WAS THE WRONG
# CHOICE. `_tokenize` strips comments, and `#` opens one at the START OF A WORD;
# blanking with whitespace is exactly what moves a mid-word `#` to a word start.
# Reproduced against this file before fixing (Codex P1 on studio#12):
#     cat $(echo README)#suffix <dotenv>   -> allow
# bash reads `README#suffix` and the dotenv as two arguments; the blank turned
# the outer text into `cat  #suffix <dotenv>`, the comment stripper ate the rest
# of the line, and the secret argument stopped existing. `_` keeps the word
# joined the way the shell joins it. It stays a word character on purpose: the
# stem is not decoration in _SECRET_PATH either, so `cat $(f).env` still reads as
# `_.env` and still matches.
_SUBSTITUTION_BLANK = "_"


def _leaf_commands(command: str) -> list[str]:
    """Every simple command in *command*, including ones inside substitutions.

    The outer text keeps its shape with substitutions blanked out, so an
    exempt verb wrapping a read is judged on the read, not on the wrapper.
    """
    out: list[str] = []
    pending = [command]
    while pending and len(out) < 100:
        chunk = pending.pop()
        inner = [g for match in _SUBSTITUTION.finditer(chunk) for g in match.groups() if g]
        if inner:
            pending.extend(inner)
            chunk = _SUBSTITUTION.sub(_SUBSTITUTION_BLANK, chunk)
        out.extend(re.split(r"[;&|\n]+", chunk))
    return out


def _statements(command: str) -> list[list[str]]:
    """Split *command* into statements as token lists, pipelines kept intact.

    `_leaf_commands` collapses `;`, `&` and `|` into one separator, which loses
    the distinction that matters for the reader check: `a | b` shares a data path
    and `a && b` does not. Substitutions are inlined the same way, so a read
    hidden inside one is still judged.

    TOKENS, NOT SUBSTRINGS, and this is the fourth defect in this one function
    that says why. Every previous cut split the raw string, so it could not tell
    an operator from the same characters inside a quoted argument, and
    `echo '.env |& xargs cat'` — which reads nothing — was asked about. Measured
    on the character split: all five separators were affected (`|`, `|&`, `&&`,
    `||`, `;`), not just the `|&` that was reported. Three earlier rounds each
    fixed one spelling with another lookaround; this replaces the mechanism.
    """
    out: list[list[str]] = []
    pending = [command]
    while pending and len(out) < 100:
        chunk = pending.pop()
        inner = [g for match in _SUBSTITUTION.finditer(chunk) for g in match.groups() if g]
        if inner:
            pending.extend(inner)
            chunk = _SUBSTITUTION.sub(_SUBSTITUTION_BLANK, chunk)
        tokens = _tokenize(chunk)
        if tokens is None:
            # Unparsable (unbalanced quotes). Fall back to the character split
            # this function used before — it cannot tell a quoted separator from
            # a real one, so it may over-split, and over-splitting only ever
            # makes the guard MORE eager. Deliberate: the alternative is to skip
            # an unparsable command, and a guard that skips what it cannot read
            # is a guard with a documented bypass.
            #
            # The segments must come out as REAL TOKENS, not as one token holding
            # the whole segment: the caller reads `leaf[0]` as the verb, so a
            # single-token segment has no recognisable verb, matches no reader
            # and no safe producer, and the fallback silently stops guarding.
            # Measured while writing this — the corpus went 28 red on a forced
            # fallback, and every one of those was this, not the split.
            out.extend(
                segment.replace("|&", " | ").replace("|", " | ").split()
                for segment in re.split(r"(?:&&|\|\||;|\n|(?<![>&|])&(?![>&]))+", chunk)
                if segment.strip()
            )
            continue
        current: list[str] = []
        for token in tokens:
            if _is_break(token):
                if current:
                    out.append(current)
                current = []
            else:
                current.append(token)
        if current:
            out.append(current)
    return out


def _exposes_secret(command: str) -> str | None:
    """Return a label if *command* could read a secret-bearing file, else None.

    Segment-wise, because `ls .env && cat .env` is two commands and only the
    second one reads: a whole-string match would be decided by the harmless
    verb that happens to come first.
    """
    # PER STATEMENT, NOT PER COMMAND. Computing the reader set over the whole
    # string made any reader anywhere taint every safe segment, so
    # `cat README.md && echo ".env" >> .gitignore` was flagged as a secret read
    # — `cat` is a reader, `echo` was therefore not exempt, and the `.env` being
    # written INTO .gitignore matched. Prompting on that is the false positive
    # this guard cannot afford: it is ordinary work, and a guard that blocks
    # ordinary work gets switched off. `|` shares a data path, `&&` and `;` do
    # not, so the reader question is only meaningful inside one pipeline.
    for statement in _statements(command):
        found = _statement_exposes(statement)
        if found:
            return found
    return None


def _statement_exposes(statement: list[str]) -> str | None:
    """The original per-segment check, scoped to one statement's pipeline.

    Takes TOKENS. Joining is safe wherever a regex still wants a string: any
    separator left inside a token was quoted, and joining never re-splits — it
    was the splitting that could not tell the two apart.
    """
    leaves: list[list[str]] = []
    current: list[str] = []
    for token in statement:
        if _is_pipe(token):
            if current:
                leaves.append(current)
            current = []
        else:
            current.append(token)
    if current:
        leaves.append(current)
    heads = {leaf[_verb_index(leaf)].rsplit("/", 1)[-1] for leaf in leaves if leaf}
    # A PRODUCER IS ONLY SAFE WHILE NOTHING DOWNSTREAM CAN READ WHAT IT NAMES.
    # This gate existed for `find` alone, so every other producer in
    # _EXPOSURE_SAFE_VERBS was exempted unconditionally and the pipeline that
    # actually reads the file was skipped along with it. Measured before the fix:
    #   find . -name .env | xargs cat   -> ask      (gated, correct)
    #   printf '.env\n'   | xargs cat   -> ALLOWED  (exempt, wrong)
    #   echo .env         | xargs cat   -> ALLOWED  (exempt, wrong)
    #   ls .env           | xargs cat   -> ALLOWED  (exempt, wrong)
    # The reader segment carries no path of its own, so once the naming segment
    # is skipped nothing is left to match and the contents reach the transcript.
    producers_are_safe = not _FIND_ACTS.search(" " + " ".join(statement)) and not (
        heads & _READER_VERBS
    )
    for leaf in leaves:
        # Drop a commit/tag message: it is prose that happens to contain a path,
        # not an argument naming one. Dropping the token AFTER the flag also
        # covers `-m do-not-cat-.env`, which the old quote-anchored regex could
        # not see because it required the quotes to still be present.
        # The verb decides whether `-m` is a message flag at all — read it from
        # the RAW leaf, before any elision, or the check would depend on the
        # elision it is meant to gate.
        _raw_at = _verb_index(leaf) if leaf else 0
        _raw_head = leaf[_raw_at].rsplit("/", 1)[-1] if leaf else ""
        _elide_message = _raw_head in _MESSAGE_VERBS and _takes_message(leaf, _raw_at)
        tokens: list[str] = []
        skip = False
        for token in leaf:
            if skip:
                skip = False
                continue
            if _elide_message and token in _MESSAGE_FLAGS:
                skip = True
                continue
            flag, _, inline = token.partition("=")
            if _elide_message and inline and flag in _MESSAGE_FLAGS:
                continue
            tokens.append(token)
        if not tokens:
            continue
        verb_at = _verb_index(tokens)
        head = tokens[verb_at].rsplit("/", 1)[-1]  # /bin/cat and cat are one verb
        if head == "find" and not producers_are_safe:
            pass  # an action or a pipe turns it into a reader's argument list
        elif head in _EXPOSURE_SAFE_VERBS and producers_are_safe:
            continue
        if head in _PATTERN_FIRST_ARG:
            rest = [t for t in tokens[verb_at + 1 :] if not t.startswith("-")]
            if rest:
                tokens = [t for t in tokens if t is not rest[0]]
        if _SECRET_PATH.search(" " + " ".join(tokens)):
            return "read of a secret-bearing file"
    return None


# Fully autonomous mode: no human is watching the prompt, so "ask" is
# meaningless — block outright. Other modes (default, plan, acceptEdits)
# still surface an interactive permission prompt for Bash.
_AUTONOMOUS_MODES = {"bypassPermissions", "dangerouslySkipPermissions"}


# THE MARKER CONTRACT (PI-901) — `context: ambient` is the owner opting a
# repo back in to the ambient (global) agent layer. Anchored at column 0: a
# top-level YAML key cannot be indented, and matching an indented one would let
# a `context: ambient` nested under some unrelated block opt the whole repo out.
# Quoted keys/values and a space before the colon ARE valid top-level YAML, and
# project-init's own `_CONTEXT_KEY_RE` preserves exactly those spellings on
# upgrade — a spelling the writer keeps but the reader misses is an opt-out that
# survives in the file and is then ignored. KEEP IN STEP with the ambient
# layer's own marker reader, which reads the same key with the same
# tolerances — two readers of one contract that disagree is the failure the
# contract exists to prevent.
#
# A COMMENT NEEDS WHITESPACE BEFORE IT (PR #927 review). `#` only begins a YAML
# comment when preceded by whitespace; otherwise it is part of the plain scalar.
# So `context: ambient#typo` is the value `ambient#typo` — NOT `ambient` — and
# the first cut read it as an opt-out, silently discarding that repo's allowlist.
# The direction is safe for this guard (no allowlist ⇒ keep guarding) and it is
# still wrong: it disables a control the owner declared, on a typo, and the
# orchestrator's real YAML parser resolves the same line differently, which is
# precisely the three-readers divergence the shared fixtures exist to prevent.
#
# TWO SPELLINGS, ONE OPT-OUT (#963): the standard's key is `scope: user`
# (D-12) and the legacy `context: ambient` is still read. Either alone opts out.
_CONTEXT_AMBIENT_RE = re.compile(
    r"""^["']?(?:context["']?[^\S\n]*:[^\S\n]*["']?ambient|scope["']?[^\S\n]*:[^\S\n]*["']?user)"""
    r"""["']?(?:[^\S\n]+\#.*)?[^\S\n]*$""",
    re.MULTILINE,
)


def _declares_ambient(config: Path) -> bool:
    """True iff *config* carries a top-level ``scope: user`` or ``context: ambient`` declaration.

    Unreadable is not ambient: an unreadable config supplies no allowlist
    either, so the guard already keeps guarding, and inventing a verdict from a
    failed read would be a guess.
    """
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(_CONTEXT_AMBIENT_RE.search(text))


def _port_root() -> Path | None:
    """The workspace root the governed repos live under, or None.

    The marker contract's PORT_ROOT rule, re-implemented here rather than
    shared — the ambient layer's own walker is POSIX shell and cannot import
    Python, so the two are pinned by the contract's shared marker fixtures
    (M33-M37), not by common code::

        PORT_ROOT = $PORT_ROOT if set and non-empty, otherwise $HOME/port

    EMPTY IS UNSET. ``PORT_ROOT=`` is someone clearing the variable, never
    someone naming the root the empty string — and here the wrong reading is
    worse than inert: ``Path("")`` is ``.``, which would put the stop at this
    process's cwd and hide a real repo's own config from the walk.

    HOME UNSET (or empty) ⇒ NO ROOT. ``$HOME/port`` would then spell ``/port``,
    a guess about the machine, and a walk that stops at a guessed path
    un-governs whatever lives there. ``os.environ`` is read rather than
    ``Path.home()`` for exactly this: ``Path.home()`` falls back to the password
    database when HOME is unset and would invent the default the contract says
    not to.

    Trailing slashes need no code: pathlib drops them at construction, so
    ``~/port/`` and ``~/port`` are one Path before the equality test. Resolved
    for the same reason ``start`` is — the stop compares physical paths, and a
    symlinked spelling of the root must not walk past it. A root that does not
    exist keeps its spelling, which is correct rather than merely tolerable: a
    directory that is not there is no ancestor of any cwd.

    Read at CALL time, never cached at import: the shared fixture runner loads
    this module once and sets the environment per case.
    """
    exported = os.environ.get("PORT_ROOT") or ""
    if exported:
        root = Path(exported)
    else:
        home = os.environ.get("HOME") or ""
        if not home:
            return None
        root = Path(home) / "port"
    with contextlib.suppress(OSError, RuntimeError):
        root = root.resolve()
    return root


def _find_config(start: Path) -> Path | None:
    """Walk up from *start* to the project's .agents/config.yaml, if any.

    A SYMLINKED marker is refused and the walk continues (PI-903; the marker
    contract). ``is_file()`` follows symlinks, so this used to
    accept an ``.agents/`` — or an ``.agents/config.yaml`` — pointing anywhere on
    disk. That matters more here than it does for a boundary verdict: this
    function locates the file ``safety.allow`` is read from, so a link planted
    outside the repo supplies its own allowlist and switches the destructive-
    command deny table off wholesale. A symlink is writable from outside the
    repo's own review, which is exactly what the guard is defending.

    Refusing is the safe direction (no allowlist ⇒ keep guarding), and it makes
    this walk agree with the ambient layer's own marker reader, which has
    refused symlinked markers since the 2026-07-24 marker-forgery finding. Nothing surfaced the
    disagreement while it existed.

    Two further rules from the same frozen contract:

    ``context: ambient`` (the marker contract) — the owner declaring that this
    repo does NOT govern itself and the ambient layer keeps acting here. A repo
    that has opted out of governed status does not get to relax the deny table
    with its own ``safety.allow``, so the declaration returns None (no
    allowlist) rather than continuing the walk. Deciding AT the marker is the
    contract's rule and the reason it is not a ``continue``: the innermost
    marker wins, so an explicit inner opt-out must not fall through and be
    overruled by an outer repo's config. A symlink is refused because it is
    forged; an ``ambient`` value is honoured because it is the owner speaking.

    ``$HOME`` (the marker contract) — the walk stops before it. A marker sitting in
    the home directory itself otherwise supplies an allowlist to every command
    run anywhere beneath it, and it is written by accident rather than by
    attack: project-init run once in the wrong cwd scaffolds one there. Paths
    outside $HOME are untouched and still walk to ``/``. Resolved first, because
    the stop is an equality test and ``~/.`` names the same directory as ``~``.

    ``PORT_ROOT`` (the marker contract; ``_port_root``) — the walk ALSO stops
    before the workspace root the repos live under. Same hazard one level down,
    and a likelier one: the operator is told to create that directory and to
    put root instruction files in it, and one ``.agents/config.yaml`` there
    would supply ``safety.allow`` to every repo beneath it. Two stops, not one
    replacing the other: the root defaults UNDER $HOME, so the $HOME stop still
    decides every path outside the workspace. And an UNSET PORT_ROOT is not an
    opt-out: the variable is routinely left unset, so a stop that held only for
    an exported value would be off exactly where it is needed. The default is
    stopped at just as an exported value is.
    """
    # RuntimeError as well as OSError, and the difference is measurable rather
    # than defensive (PR #927 review): `Path.resolve()` raises RuntimeError on a
    # SYMLINK LOOP under Python 3.11 and 3.12 and stopped doing so in 3.13 —
    # checked on all three. Both older versions are in this repo's CI matrix and
    # this template ships to projects running whichever Python they have. An
    # escaping exception here does not crash the session (the guard's outer
    # handler is fail-open by design) — it makes the guard STAND DOWN, so a
    # planted loop anywhere in the walk path switches the deny table off for
    # that command. A path that cannot be resolved is used as spelled instead.
    with contextlib.suppress(OSError, RuntimeError):
        start = start.resolve()
    try:
        home: Path | None = Path.home().resolve()
    except (RuntimeError, OSError):
        home = None  # no home to stop before; inventing one would be a guess
    port_root = _port_root()
    for candidate in (start, *start.parents):
        # BEFORE examining either root, not at it: a session whose cwd IS the
        # root resolves exactly like one beneath it (M30, M36).
        if candidate in (home, port_root):
            break
        agents = candidate / ".agents"
        config = agents / "config.yaml"
        if agents.is_symlink() or config.is_symlink():
            continue
        if config.is_file():
            return None if _declares_ambient(config) else config
    return None


def _unquote(value: str) -> str:
    """Strip one pair of matching surrounding quotes, leaving mismatched or
    single quotes intact so ``'foo"`` is not silently corrupted (PI-187 review).
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_inline_allow(raw: str, problems: list[str]) -> list[str]:
    """Parse the inline form, ``allow: ["a", "b"]``.

    A REGEX IS NOT JSON (PI-943). ``["^cat \\.env$"]`` is the natural way to
    write an escaped dot, and it is not valid JSON — ``\\.`` is not a legal
    escape — so ``json.loads`` raised, the caller fell open, and the operator's
    allowlist silently did not exist. They saw a prompt for the very command
    they had just allowlisted, with nothing anywhere saying why.

    Strict parse first, so an operator who correctly wrote ``\\\\.`` keeps the
    literal backslash they asked for. Only on failure are backslashes escaped
    and the parse retried — which is what someone writing a raw regex meant.
    Doing it in that order is what keeps the lenient path from changing the
    meaning of input that was already valid.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        try:
            parsed = json.loads(raw.replace("\\", "\\\\"))
        except json.JSONDecodeError as exc:
            problems.append(f"inline `allow:` could not be parsed ({exc.msg})")
            return []
    # A non-list allow (JSON string/object/number) must not be iterated
    # character-by-character into an over-permissive allowlist or crash the
    # guard — ignore it and keep guarding (PI-187 review).
    if not isinstance(parsed, list):
        problems.append("`allow:` is not a list — ignored")
        return []
    return [p for p in parsed if isinstance(p, str)]


def _allow_patterns(root: Path) -> tuple[list[re.Pattern[str]], list[str]]:
    """Read safety.allow from .agents/config.yaml → (patterns, problems).

    Accepts both an inline JSON list (``allow: ["a", "b"]``) and a multi-line
    YAML list (``allow:`` on its own line followed by ``- "a"`` items). The
    inline-only parser silently dropped the natural YAML form to ``[]`` (PI-187).

    FAIL-OPEN IS RIGHT FOR A MISSING CONFIG AND WRONG FOR A MALFORMED ONE
    (PI-943). The two are indistinguishable to the operator, and the malformed
    case means a rule they wrote is not in force. So problems are collected and
    returned rather than swallowed, and the caller puts them in front of the
    person who is about to wonder why their allowlist did nothing.

    *root* is the Bash tool's cwd, which may be a subdirectory after `cd` —
    the config is located by walking up the tree.
    """
    config = _find_config(root)
    problems: list[str] = []
    if config is None:
        return [], problems
    patterns: list[str] = []
    try:
        in_safety = False
        in_allow = False
        for line in config.read_text(encoding="utf-8").splitlines():
            if line.startswith("safety:"):
                in_safety = True
                continue
            if not in_safety:
                continue
            if line.strip() and not line.startswith((" ", "\t")):
                break  # a column-0 key ends the safety block
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if in_allow and stripped.startswith("- "):
                patterns.append(_unquote(stripped[2:].strip()))
                continue
            in_allow = False
            if stripped.startswith("allow:"):
                raw = stripped.split(":", 1)[1].strip()
                if raw:
                    patterns.extend(_parse_inline_allow(raw, problems))
                else:
                    in_allow = True  # multi-line YAML list follows
    except OSError as exc:
        problems.append(f"could not read {config}: {exc.strerror or exc}")
        return [], problems

    # Compiled ONE AT A TIME. The old `[re.compile(p) for p in ...]` inside the
    # try meant a single malformed pattern discarded every other rule in the
    # file, including the ones that had parsed perfectly (PI-943).
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        if not pattern:
            continue
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            problems.append(f"safety.allow pattern {pattern!r} is not a valid regex ({exc.msg})")
    return compiled, problems


def _find_obs_dir(start: Path) -> Path | None:
    """Locate the overlay marker dir (.agents/observability/), or None.

    Prefers ``$CLAUDE_PROJECT_DIR``; otherwise walks up from *start* (the Bash
    cwd, which may be a subdirectory after ``cd``), mirroring ``_find_config``.
    """
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        obs = Path(env) / ".agents" / "observability"
        return obs if obs.is_dir() else None
    for candidate in (start, *start.parents):
        obs = candidate / ".agents" / "observability"
        if obs.is_dir():
            return obs
    return None


def _redact_command(command: str) -> str:
    """Truncate to 500 chars and redact common secret patterns."""
    cmd = command[:500]
    cmd = re.sub(r"(?i)(token|key|secret|password|auth|api_key)=[\w-]+", r"\1=***", cmd)
    return re.sub(r"://[^@]+@", r"://***@", cmd)


def usage_log(payload: dict, root: Path, decision: str, command: str) -> None:
    """Append a self-log line iff the observability overlay is installed (#406).

    Shipped-always-dormant: no-ops unless ``.agents/observability/`` exists.
    Uses the *already-parsed* ``payload`` (no second stdin read) and is fully
    fail-open — it must never raise or block the guard.
    """
    try:
        obs = _find_obs_dir(root)
        if obs is None:
            return
        line = {
            # time.gmtime keeps this portable across every Python 3 (no
            # datetime.UTC, which is 3.11+) — scaffolded projects may run older.
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hook": "prod_guard",
            "event": "PreToolUse",
            "project": str(obs.parent.parent),
            "decision": decision,
            "command": _redact_command(command),
        }
        session = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
        if session:
            line["session"] = session
        with (obs / "usage.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except Exception:  # noqa: BLE001 — logging must never break the guard
        return


def _verdict(reason: str, permission_mode: str, problems: list[str] | None = None) -> dict:
    """Build the hook verdict. Autonomous modes have no human to ask (ADR-012).

    *problems* is appended to the reason. This is the one place the operator is
    guaranteed to read: they are staring at a prompt for a command they believe
    they allowlisted, which is exactly the moment to tell them the allowlist
    did not load (PI-943). stderr from a PreToolUse hook that exits 0 is not
    reliably surfaced, so it cannot be the only channel.
    """
    if problems:
        reason += " NOTE: safety.allow was not fully applied — " + "; ".join(problems) + "."
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny" if permission_mode in _AUTONOMOUS_MODES else "ask",
            "permissionDecisionReason": reason,
        }
    }


def evaluate(
    command: str,
    permission_mode: str,
    allow: list[re.Pattern[str]],
    problems: list[str] | None = None,
) -> dict | None:
    """Return the hook verdict for *command*, or None to let it through."""
    if any(p.search(command) for p in allow):
        return None
    # Computed once, not per rule: 20-odd rules over the same string.
    prose_free = _without_prose(command)
    # #1043: every check also reads the command after quote removal, so
    # `terraform "destroy"` is the verb it runs. Only the checks that consume
    # `views` below — config-env and PS4 — read the prose-blanked forms.
    views = (prose_free, _dequoted(prose_free))
    pairs = ((command, views[0]), (_dequoted(command), views[1]))
    for pattern, label in DENY_RULES:
        # #965: the verb is real only if it survives blanking the prose. A rule
        # that matches ONLY inside a commit message or a grep pattern was
        # reading documentation, not an operation.
        if any(pattern.search(whole) and pattern.search(bare) for whole, bare in pairs):
            return _verdict(
                f"prod_guard: '{label}' is a destructive operation. "
                "If this is intentional and safe, add a matching regex to "
                "safety.allow in .agents/config.yaml, or run it yourself. "
                "(Guardrail only — real protection is credential separation, "
                "see .agents/docs/guides/secrets.md.)",
                permission_mode,
                problems,
            )
    runner = _search_runs_program(command) or _config_env_runs_program(views)
    if runner is not None:
        return _verdict(
            f"prod_guard: '{runner}' makes a search tool run another program, "
            "which no deny rule can inspect. Run that program directly so it is "
            "checked, add a matching regex to safety.allow in .agents/config.yaml, "
            "or run the command yourself.",
            permission_mode,
            problems,
        )
    tracer = _trace_runs_program(views)
    if tracer is not None:
        return _verdict(
            f"prod_guard: '{tracer}' runs a PS4 command substitution on every "
            "traced command, which no deny rule can inspect. Drop the substitution "
            "from PS4, add a matching regex to safety.allow in .agents/config.yaml, "
            "or run the command yourself.",
            permission_mode,
            problems,
        )
    exposure = _exposes_secret(command)
    if exposure is not None:
        return _verdict(
            f"prod_guard: '{exposure}' — its contents would enter the transcript "
            "and be re-sent on every following turn. Read the .example file, or "
            "have the value injected as an environment variable. If the read is "
            "genuinely needed, add a matching regex to safety.allow in "
            ".agents/config.yaml, or run the command yourself. "
            "(Guardrail only — real protection is credential separation, "
            "see .agents/docs/guides/secrets.md.)",
            permission_mode,
            problems,
        )
    return None


def main() -> int:
    """Read the PreToolUse payload from stdin; print a verdict if any."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0  # non-dict JSON (e.g. a list) → fail open, never raise
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}  # tool_input present but non-dict → fail open, never raise
    command = (tool_input.get("command") or "").strip()
    if not command:
        return 0
    mode = payload.get("permission_mode") or payload.get("permissionMode") or ""
    root = Path(payload.get("cwd") or ".")
    try:
        allow, problems = _allow_patterns(root)
        for problem in problems:
            # Best-effort second channel. Not the primary one — see _verdict.
            print(f"prod_guard: {problem}", file=sys.stderr)
        verdict = evaluate(command, mode, allow, problems)
    except Exception:  # noqa: BLE001 — guardrail must never break the session
        verdict = None

    decision = "allow"
    if verdict is not None:
        raw_decision = verdict.get("hookSpecificOutput", {}).get("permissionDecision", "allow")
        decision = "block" if raw_decision == "deny" else raw_decision

    # Self-log this firing from the same parsed payload (no second stdin read,
    # #406). Dormant unless the observability overlay is installed; fail-open.
    usage_log(payload, root, decision, command)

    if verdict is not None:
        sys.stdout.write(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] in (["-h"], ["--help"]):  # --help does no work (#992)
        print((__doc__ or "").strip())
        sys.exit(0)
    sys.exit(main())
