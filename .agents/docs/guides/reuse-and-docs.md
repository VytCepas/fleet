# Reuse and docs guide — what to reach for, and what to read first

Reference detail for the rule stated in [AGENTS.md](../../../AGENTS.md), kept
here so the always-loaded instruction file stays lean.

## Reuse before you write

Reach for what already exists, in this order:

1. the framework's built-in (routing, images, forms, metadata, caching);
2. a component or utility this repo already depends on;
3. a maintained library (active releases, a licence you can ship);
4. your own code.

Writing your own is allowed. When you do, the code carries a short comment
saying which library you rejected, why (bundle size, licence, fit), and what
would make you revisit, so the next person can check the decision rather than
rediscover it.

## Read the docs for the installed version

Before writing against a framework or library API, read the docs for the
version in the lockfile, not the one you remember. Models and people both carry
an older API in their heads. In order:

1. docs the package ships with the installed version (for example under
   `node_modules/<package>/`);
2. a docs-lookup MCP such as Context7, if this project configures one;
3. the official docs site, at the matching version.

## What a ready PR records

Before a PR leaves draft, its body carries two filled lines, and `validate-pr`
fails without them:

```text
Reuse: none
Reuse: date picker hand-built; rejected <library> (bundle size); revisit if it needs ranges
Docs: framework docs shipped in node_modules, locked version
Docs: n/a
```

`Reuse: none` means nothing was hand-built in place of a library. `Docs: n/a`
fits a change that touches no framework or library API. The check proves a
decision was written down, not that it was right; review judges that.

