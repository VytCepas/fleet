#!/usr/bin/env bash
# _py.sh: the old name of python-resolve.sh, kept until 2026-12-01 so that a tool
# or a note that still names this path keeps working. Run python-resolve.sh.
exec "$(dirname "$0")/python-resolve.sh" "$@"
