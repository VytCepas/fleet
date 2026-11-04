---
kind: decision-record
owner: @VytCepas
status: accepted
alias: D-1
date: 2026-11-04
supersedes: -
superseded_by: -
---

# Decisions are recorded here, one file each

## Context

A decision that lives only in a chat or a pull request is lost to the next reader. Several repos each number their own
records, so a bare `D-<n>` names a different decision in each of them.

## Decision

Every decision is a file `docs/decisions/NNNN-<slug>.md`, numbered from `0001`, under this front matter. This file is the model to copy.

| Field | Meaning |
|---|---|
| `kind` | `decision-record` |
| `status` | `proposed`, `accepted`, `rejected` or `superseded` |
| `alias` | `D-<n>`, the file number without its leading zeros |
| `date` | the day it was decided, `YYYY-MM-DD` |
| `supersedes`, `superseded_by` | the records this one replaces and the one that replaces it; `-` when none |

A record is cited `D-<n>` inside this repo and `<system>/D-<n>` anywhere else, where `<system>` is the repo's name: the same number
is a different record in another repo. Supersession is a pair: when a record names a successor in `superseded_by`, its status is
`superseded` and the successor lists it in `supersedes`, and the other way round.

## Consequences

- A record is found by its id from any repo, and a reader can tell what replaced it.
- Changing a decision is a new record that supersedes the old one, never an edit that erases it.

