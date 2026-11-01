# Decision records

One record per decision of projects-orchestrator, in `docs/decisions/<NNNN>-<slug>.md`, copied from [`template.md`](template.md)
(the `add-decision-record` skill does the copy). The id is `D-<n>` inside this repo and `projects-orchestrator/D-<n>` everywhere else,
so a citation in another repo's document never names a different record.

Every record opens with the same front matter, shared by every governed repo: `kind`, `status` (`proposed`, `accepted`,
`rejected` or `superseded`), `alias` (`D-<n>`), `date`, `supersedes` and `superseded_by` (`-` for none). A supersession is
written on both records. The naming standard's lint (`standard.decision-fields`, `standard.decision-supersession`) holds the
records to it.
