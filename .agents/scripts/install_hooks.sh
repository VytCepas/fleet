#!/usr/bin/env bash
# install_hooks.sh: the old name of install-hooks.sh, kept until 2026-12-01 so that
# a tool or a note that still names this path keeps working. Run install-hooks.sh.
exec "$(dirname "$0")/install-hooks.sh" "$@"
