# .agents/docs — Internal Knowledge Base

This folder is the **system of record** for architectural decisions and development standards. It is version-controlled alongside code and is the primary source of context for AI agents.

## Structure

```
docs/
├── adr/           # Architecture Decision Records — why decisions were made
├── development/   # Standards, conventions, testing strategy
└── guides/        # How-to guides for common workflows
```

## How to use

**Agents:** Read `.agents/docs/decisions/` before starting tasks. If you establish a new pattern or make a non-obvious decision, write a brief ADR.

