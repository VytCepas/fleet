---
name: request-review
description: Marks a draft PR ready for review, moving it Draft → In Review and triggering board automation. Use when a draft PR is finished and ready for reviewers.
when_to_use: Use when the user says "request review", "mark the PR ready", "this PR is ready", or a draft PR is ready to move from Draft to In Review.
argument-hint: "[pr-number]"
allowed-tools: Bash(.agents/scripts/*) Read
---

Mark PR $ARGUMENTS (or current branch's PR if omitted) ready for review.

## Steps

1. **Promote to ready**:
   ```bash
   .agents/scripts/promote-review.sh $ARGUMENTS
   ```
   `board-automation.yml` moves the board card to **In Review** automatically.

2. **Next steps**: Reviewers will be pinged. When they request changes, they'll post comments on the PR.
