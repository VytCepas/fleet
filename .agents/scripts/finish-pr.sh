#!/usr/bin/env bash
# Thin shim — actual logic lives in .agents/hooks/dag_workflow.py.
exec "$(dirname "$0")/../hooks/python-resolve.sh" "$(dirname "$0")/../hooks/dag_workflow.py" finish "$@"
