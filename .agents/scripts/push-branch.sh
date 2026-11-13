#!/usr/bin/env bash
# Thin shim — actual logic lives in .agents/hooks/dag_workflow.py.
# Kept under .agents/scripts/ so existing skill paths and agent muscle
# memory keep working.
exec "$(dirname "$0")/../hooks/python-resolve.sh" "$(dirname "$0")/../hooks/dag_workflow.py" push "$@"
