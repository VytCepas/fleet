---
description: Hook reference — what each hook does and how to invoke manually
globs: [".agents/settings.json", ".agents/hooks/**"]
alwaysApply: false
---

## Hooks in this project

All hooks fire automatically — do not invoke them manually in normal flow.

| Hook | Event | Trigger |
|---|---|---|
| `github_command_guard.sh` | PreToolUse | Bash — steers agents toward lifecycle scripts |
| `pre-commit-gate.sh` | PreToolUse | Bash containing `git commit` |
| `post-edit-lint.sh` | PostToolUse | Edit / Write / MultiEdit |
| `workflow-state-reminder.sh` | UserPromptSubmit | injects workflow lifecycle context |

Security enforcement is agent-agnostic (ADR-007), not a Claude hook:
secret scanning runs as a git pre-commit hook (gitleaks) plus a CI
`secret-scan` job; lifecycle gating runs as git `commit-msg`/`pre-push`
hooks plus the `validate-pr` workflow. Claude-side security guidance comes
from the `security-guidance` plugin enabled in `settings.json`.

**Manual invocation** (debugging or one-off runs only):
```bash
just lint                                # the same gate the hooks enforce
# (with --no-plugin scaffolds the hook scripts live in .agents/hooks/)
```

Do not read hook scripts to understand their logic — descriptions above are sufficient. Only open a hook file if you are modifying it. To add a new hook, run the `add-hook` skill.
