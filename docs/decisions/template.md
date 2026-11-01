---
kind: decision-record
status: proposed
alias: D-0
date: YYYY-MM-DD
supersedes: -
superseded_by: -
---

# {short title: the problem and the chosen solution}

<!-- MADR-style template (https://adr.github.io/madr/). Copy to <NNNN>-<kebab-slug>.md, where NNNN is the
     next free number, and set `alias` to D-<that number without its zeros>. The add-decision-record skill does
     this for you. Delete comments before committing.

     The front matter is the one shape every governed repo shares:
       status        proposed | accepted | rejected | superseded
       supersedes    - , or the records this one replaces: D-<n>, comma-separated
       superseded_by - , or the record that replaces this one. Both sides are written: a record that names
                     a successor is `superseded`, and the successor names it back.
       revised_by    add this line only when a later record changes part of this one and this one stays accepted
     A record in another repo is written <system>/D-<n>. This repo's own records are D-<n> here and
     <this-repo-name>/D-<n> anywhere else.

     Diagrams: don't paste a render into the record. Draw it with the diagram skill and link its folder.
     Write the href relative to THIS file's home (docs/decisions/). Linking, not pasting, keeps the record
     from drifting when the diagram is re-rendered (map-not-territory). -->

## Context and Problem Statement

<!-- 2-5 sentences: what forces this decision? What breaks or stalls if it
     is not made? Phrase the problem as a question when possible. -->

## Considered Options

- Option 1
- Option 2
- Option 3

## Decision Outcome

Chosen option: "Option 1", because <!-- justification: meets criterion X,
resolves the problem, fewest downsides -->.

### Consequences

- Good: <!-- what becomes easier -->
- Bad: <!-- what becomes harder; accepted trade-offs -->
