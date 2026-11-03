# justfile — the canonical command interface (scaffolded by project-init).
# `just --list` shows every recipe. Recipes are thin wrappers — logic lives
# in the tools and their configs, never in this file.

# install/sync dev dependencies (PEP 735 dependency-group; add tools with `uv add --dev`).
# A fresh scaffold has no pyproject.toml (or one without [dependency-groups])
# yet — `uv sync --group dev` hard-fails on both, which would break CI's first
# step before the day-one guards in typecheck/test-cov can even run.
[doc("install/sync dev dependencies (PEP 735 dependency-group; add tools with `uv add --dev`).")]
setup:
    sh -c 'if [ ! -f pyproject.toml ]; then echo "No pyproject.toml yet — nothing to sync."; elif grep -q "^\[dependency-groups\]" pyproject.toml; then uv sync --group dev; else uv sync; fi'

# lint project code (docstring + complexity gates per ruff.toml).
# `ruff format --check` verifies formatting without writing — `just format`
# writes. Without this, an unformatted file merged green (#726).
# The discovery check runs with this repo's two deliberate collection gaps lifted:
# addopts ignores tests/check/integration (CI's own job runs it) and test_properties.py
# skips without hypothesis, so a plain collection would flag both as "run nowhere".
[doc("lint project code (docstring + complexity gates per ruff.toml).")]
lint:
    uv run ruff check .
    uv run ruff format --check .
    sh -c 'if command -v shellcheck >/dev/null 2>&1; then find .agents -name "*.sh" -exec shellcheck -S error -x {} +; else echo "shellcheck not installed — skipping shell lint (CI still runs it). Install: https://github.com/koalaman/shellcheck#installing or run \`mise install\`."; fi'
    sh -c 'if command -v shfmt >/dev/null 2>&1; then find .agents -name "*.sh" -exec shfmt -d -i 2 {} +; else echo "shfmt not installed — skipping shfmt check (CI still runs it). Install: https://github.com/mvdan/sh#shfmt or run \`mise install\`."; fi'
    bash .agents/scripts/lint_context_budget.sh
    uv run python .agents/scripts/check_test_contract.py exit-codes
    PYTEST_ADDOPTS="-o addopts=" uv run --with pytest --with hypothesis python .agents/scripts/check_test_contract.py discovery
    just docs-check

# docs front matter and generated-file headers, strict: every docs/**/*.md carries kind, owner and status; a
# generated-by: header equals its contracts/fresh-checks.tsv row, and each listed file equals a fresh regeneration
# (judged by the vendored doc-shape.sh and doc-fresh-verify.sh; the meta-file standard).
[doc("docs front matter + generated-file freshness (gates/doc-check.sh --strict)")]
docs-check:
    sh gates/doc-check.sh --strict

# the naming standard over this repo, with naming-exemptions.tsv applied (gates/naming-check.sh). The judge is the standard's
# checkout, named by NAMING_STANDARD_REPO; without one the gate says UNCHECKED and exits 3, never a pass (verbs.tsv, standard#1428).
[doc("naming standard check (gates/naming-check.sh); exit 3 is unchecked, never a pass")]
naming:
    sh gates/naming-check.sh

# static type check (strict mode per mypy.ini; add mypy with `uv add --dev mypy`)
# no-op on a fresh scaffold with no src/ yet — mypy errors on a missing path.
# --install-types fetches missing dependency stubs (types-PyYAML etc.) so
# untyped deps don't fail the strict gate with import-untyped (#592); it
# shells out to pip, which uv-managed environments omit — hence --with pip.
[doc("static type check (strict mode per mypy.ini; add mypy with `uv add --dev mypy`)")]
typecheck:
    if [ -d src ]; then uv run --with "mypy>=1.10" --with pip mypy --install-types --non-interactive src/; else echo "No src/ directory yet — nothing to type-check."; fi

# auto-format project code
format:
    uv run ruff format .

# run the test suite (xdist pulled in on demand so -n works without declaring it).
# The cross-repo test contract (PI-1044): it ends with `<project>: N passed, M failed`,
# printed by the root conftest.py, and exits with pytest's own code.
[doc("run the test suite; ends with `<project>: N passed, M failed` (the test contract)")]
test:
    sh -c 'if find tests -type f \( -name "test_*.py" -o -name "*_test.py" \) 2>/dev/null | grep -q .; then uv run --with pytest-xdist pytest -n auto --tb=short -q; else echo "No test files yet — nothing to test."; echo "projects-orchestrator: 0 passed, 0 failed"; fi'

# fail-fast quiet run for the edit-test loop (token-efficiency; PI-641) —
# stops at the first failure so agents ingest one traceback, not the suite's
[doc("fail-fast quiet run for the edit-test loop (token-efficiency; PI-641)")]
test-quick:
    sh -c 'if find tests -type f \( -name "test_*.py" -o -name "*_test.py" \) 2>/dev/null | grep -q .; then uv run --with pytest pytest -x -q --tb=short; else echo "No test files yet — nothing to test."; fi'

# tests with the coverage gate (CI always runs this, not `test` — PI-569).
# without src/ yet, still run the plain test suite (tests/ may exist before
# src/ does) — only the coverage instrumentation/threshold is skipped, since
# 0% coverage on zero application code would trip --cov-fail-under before any
# code exists. Silently skipping pytest entirely here would let a real test
# failure through `just ci` unnoticed.
[doc("tests with the coverage gate (CI always runs this, not `test` — PI-569).")]
test-cov:
    sh -c 'if [ -d src ]; then uv run --with pytest-xdist --with pytest-cov pytest -n auto --tb=short -q --cov=src --cov-fail-under=70; elif find tests -type f \( -name "test_*.py" -o -name "*_test.py" \) 2>/dev/null | grep -q .; then uv run --with pytest-xdist pytest -n auto --tb=short -q; else echo "No src/ or test files yet — nothing to test."; fi'

# mutation testing on core logic (slow; requires [tool.mutmut] source_paths
# in your own pyproject.toml, scoped to deterministic pure-logic modules —
# skip I/O-heavy code, hooks, template renderers). CI runs this nightly.
# Workers default to 1: mutmut forks os.cpu_count() workers, which a container
# reports as the HOST's count — 8 forks in a 1-CPU/1-GiB runner OOM-killed and
# then hung for 3h. Raise it per run: MUTMUT_MAX_CHILDREN=4 just test-mutation.
[doc("mutation testing on core logic")]
test-mutation:
    uv run --with mutmut mutmut run --max-children "${MUTMUT_MAX_CHILDREN:-1}"
    uv run --with mutmut mutmut export-cicd-stats

# dependency vulnerability scan against known CVEs/advisories (PI-568).
# complements package_guard.py, which only blocks installing a package that
# doesn't exist or looks typosquatted — this catches a real, correctly-
# spelled dependency with a known vulnerability already in the lockfile.
[doc("dependency vulnerability scan against known CVEs/advisories (PI-568).")]
audit:
    sh -c 'if [ -f pyproject.toml ] || [ -f requirements.txt ] || [ -f uv.lock ]; then uv run --with pip-audit pip-audit; else echo "No Python dependency manifest yet — nothing to audit."; fi'

# What changed in the FLEET since the last digest run (#98) — not a dependency
# scan; that's `just audit` above. Set PO_DIGEST_WEBHOOK to also push the delta
# to Slack. This is what the systemd timer in contrib/systemd/ runs on a
# cadence; see docs/how-to/scheduled-audit-digest.md.
[doc("fleet audit digest: what changed since the last run (#98)")]
digest:
    sh -c 'uv run projects-orchestrator audit --digest ${PO_DIGEST_WEBHOOK:+--webhook "$PO_DIGEST_WEBHOOK"}'

# Autonomously open PRs that fix the fleet's red lint/test gates (#154). Spends a
# budget-capped coding agent per project and opens DRAFT PRs only — nothing
# merges. PO_HEAL_LIMIT caps projects per pass (default 3). This is what the
# systemd timer in contrib/systemd/ runs; see docs/how-to/scheduled-heal.md.
[doc("autonomously heal the fleet's red lint/test gates via PRs (#154)")]
heal-all:
    sh -c 'uv run projects-orchestrator heal --all --limit "${PO_HEAL_LIMIT:-3}"'

# generate a CycloneDX SBOM of the runtime dependency tree (#574). release.yml
# attaches this to GitHub Releases; run locally on demand. --no-dev so it
# reflects what ships; uvx keeps cyclonedx-py out of the scanned .venv.
[doc("generate a CycloneDX SBOM of the runtime dependency tree (#574)")]
sbom:
    uv sync --no-dev
    uvx --from cyclonedx-bom cyclonedx-py environment .venv -o sbom.cdx.json

# dependency license compliance scan (#579) — fail on copyleft (GPL/AGPL; also
# LGPL, since --partial-match is substring-based). Tune the --fail-on deny-list
# to your policy. Non-blocking in CI initially (see ci.yml's license-scan job).
[doc("dependency license compliance scan (#579)")]
license:
    uv run --with pip-licenses pip-licenses --from=mixed --fail-on "GPL;AGPL" --partial-match

# property-based tests with Hypothesis (#580). Property tests are opt-in per file
# (import hypothesis); this recipe makes Hypothesis available without adding it as
# a permanent dependency. Pattern/tooling, NOT a blocking gate: CI runs this in
# the nightly `fuzz` job, never on a PR (#727). Hypothesis draws fresh seeds each
# run, so the nightly job explores inputs a per-PR run would only repeat.
[doc("property-based tests with Hypothesis (#580)")]
fuzz:
    uv run --with hypothesis --with pytest-xdist pytest -n auto --tb=short -q

# what CI runs (the full gate)
ci: setup lint typecheck test-cov audit

# fast local gate for the pre-push hook: lint + parallel tests (CI is the full
# backstop, so pre-push stays fast and doesn't re-run typecheck/coverage/audit)
[doc("fast local gate for the pre-push hook")]
fast-ci: lint test

# route CI to a self-hosted runner when Actions minutes run out (PI-666);
# requires a registered runner — see .agents/docs/guides/self-hosted-ci-runner.md
[doc("route CI to a self-hosted runner when Actions minutes run out (PI-666)")]
ci-local-on:
    gh variable set CI_RUNS_ON --body self-hosted

# back to GitHub-hosted runners
ci-local-off:
    gh variable delete CI_RUNS_ON

# serve the docs site locally
docs:
    uv run --with mkdocs-material --with "mkdocstrings[python]" mkdocs serve

# scan staged changes for secrets (same scan as the pre-commit git hook)
scan:
    gitleaks git --pre-commit --staged --redact --no-banner --verbose

# regenerate .agents/docs/CODE_MAP.md (low-token "what does what" map; read before grepping)
code-map:
    uv run python .agents/scripts/gen_code_map.py

# is the vendored project-init contract still what upstream ships? (#106)
# The contract tests guard the copy vendored HERE; this guards that copy against
# upstream. Exits 1 on drift, 0 when fresh OR unreachable (a flaky network is not
# a contract change). CI runs it weekly and opens an issue; run it locally at will.
[doc("check the vendored project-init contract against upstream (#106)")]
contract-freshness:
    uv run python .agents/scripts/check_contract_freshness.py


# install THIS tool onto the machine from this checkout (#317). A dry run by
# default: it prints the plan and the gates and installs nothing. --apply runs
# `uv tool install --reinstall` only from a clean main in sync with origin/main;
# --check diffs the installed build against HEAD and exits 1 on drift.
# --no-project: the script is stdlib only, and a project run syncs .venv first
# (creates it, installs, reaches the index), so the dry run would not be read-only.
# --python keeps the floor the project set: tomllib needs 3.11.
[doc("install this tool from the checkout: dry run, --apply or --check (#317)")]
[positional-arguments]
install *args:
    uv run --no-python-downloads --no-project --python '>=3.11' python .agents/scripts/install_tool.py "$@"
