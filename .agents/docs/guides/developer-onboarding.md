# Developer onboarding — personal machine setup

This page covers **per-developer machine state** that the scaffolder
deliberately does not manage: global git configuration and editor sync are
personal, not repository state, so they are documented here instead of being
scaffolded (see the project-init decision in PI-140).

Everything repo-level (linting, commands, env examples) ships with the
repository. The one per-clone step: run `.agents/scripts/install-hooks.sh`
once to activate the `pre-commit` (gitleaks secret scan), `commit-msg`,
and `pre-push` git hooks — git does not enable repository hooks
automatically. Install [gitleaks](https://github.com/gitleaks/gitleaks#installing)
for fast local secret scanning; without it the pre-commit scan is skipped
and CI catches leaks instead.

## Dependency updates (Renovate)

The repo ships a `renovate.json` (weekly grouped updates, GitHub Actions
pinned by digest, lockfile maintenance — managers activate automatically
from the files present). Renovate PRs arrive as `chore: Update …`, the
canonical no-issue title format the PR validators accept. One per-org
step: install the [Renovate GitHub App](https://github.com/apps/renovate)
on the repository.

To centralize policy across an organization, replace the `extends` list
with a shared preset. Keep the PR-title settings unless the org preset
provides them — Renovate's defaults (`chore(deps): …`) fail this repo's
PR-title validator:

```json
{
  "extends": ["github>your-org/renovate-config"],
  "semanticCommits": "disabled",
  "commitMessagePrefix": "chore:"
}
```

## Global gitignore

Keep OS and editor junk out of every repo you touch, without bloating each
project's `.gitignore`:

```bash
git config --global core.excludesFile ~/.gitignore_global
cat >> ~/.gitignore_global <<'EOF'
.DS_Store
Thumbs.db
*.swp
.idea/
EOF
```

Project `.gitignore` files stay focused on project artifacts (build output,
env files, caches) — never add personal editor noise to them.

## Recommended global git config

```bash
git config --global init.defaultBranch main
git config --global pull.rebase true          # no accidental merge commits on pull
git config --global push.autoSetupRemote true # first push sets upstream
git config --global fetch.prune true          # drop deleted remote branches
```

Identity (use your work email on work machines):

```bash
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"
```

## Commit signing (if your org requires it)

```bash
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global commit.gpgsign true
```

## Editor settings

- **VS Code Settings Sync** is an account-level feature — enable it in VS Code
  itself (`Settings Sync: Turn On`). It is intentionally not configured by the
  repository.
- Repo-level editor settings (format-on-save, recommended extensions) live in
  the repository's `.vscode/` directory when the project opted into that
  overlay — personal themes and keybindings never belong there.

## Checklist

- [ ] Global gitignore configured
- [ ] Global git config applied (rebase pulls, pruning, identity)
- [ ] Commit signing set up, if required
- [ ] Repo hooks installed: run `.agents/scripts/install-hooks.sh` once per clone
- [ ] gitleaks installed for the local pre-commit secret scan

## Multi-agent support tiers

Only the Claude Code path is functionally CI-tested; treat other agents as
best-effort. If the project was scaffolded with extra agents (`--agents`):

| Agent | What you get | Setup per clone |
|---|---|---|
| Claude Code | full tier: hooks, skills, settings | none (plus `install-hooks.sh` like everyone) |
| Codex | shared skills at `.agents/skills/`, command guard wired via `.codex/hooks.json` (advisory; some Codex versions gate project hooks behind a one-time trust/enable step) | none |
| Antigravity (`agy`) | skills at `.agents/skills/`, command guard via `.agents/hooks.json` (experimental), MCP via `.agents/mcp_config.json` | none (`.agents/` is auto-discovered) |
| Ollama-based (Aider, Goose, …) | AGENTS.md instructions, portable scripts, markdown memory | none |

The git hooks and CI checks are the real enforcement boundary for every
agent — agent-side hooks are fast-feedback guardrails only.

## Working from a phone or tablet

To continue a Claude Code session from your phone, use **Remote Control**
(`claude remote-control`, or `/remote-control` in a live session) — it drives a
session that still runs **on your machine**, so these hooks, your MCP servers, and
git credentials stay active, and it needs no inbound ports. **Claude Code on the
web / cloud sessions** run in a sandbox that honors only repo-committed config (not
your local setup), so git + CI are the guaranteed guardrails there. For a fully
headless box or a non-Claude tool, attach over `tmux` + a mesh VPN (e.g. Tailscale)
instead.
