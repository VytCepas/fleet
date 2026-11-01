"""Read and search the fleet's memory — the "all-knowing" layer.

Every project-init project keeps small structured facts under its memory
directory (``.agents/memory/*.md``, or ``.claude/memory/*.md`` on a legacy
scaffold — :func:`resolve_config` prefers ``.agents``; this docstring named only
the legacy spelling until #217 — with ``name``/``description``/``type``
frontmatter, indexed by ``MEMORY.md``). The orchestrator reads that contract
across the whole fleet, so one query answers "what do my projects know
about X?" without opening any of them. Reading never raises; malformed
files degrade to untyped entries.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from projects_orchestrator.descriptor import TIER_GRAPH, TIER_RAG, ProjectDescriptor

_log = logging.getLogger(__name__)

_INDEX_FILES = {"MEMORY.md", "SCHEMA.md", "README.md"}

_MAX_FILE_BYTES = 262_144

_MAX_GRAPH_BYTES = 4_194_304

# Retrieval surfaces, in the degrade-by-tier order of project-init ADR-025 §4: a reader picks
# the richest surface its tier provides and degrades to the grep baseline, whose
# anchors never move — so a tier-0 read stays correct against a tier-3 child.
MODE_RAG = "rag"
MODE_GRAPH = "graph"
MODE_GREP = "grep"

# Node keys a graphify export may use for a fact's title / prose. The graph is
# produced by an external tool whose schema is not part of the frozen contract,
# so the reader is deliberately tolerant: it takes the first key it finds and
# degrades to the grep baseline on anything it cannot read.
_GRAPH_NAME_KEYS = ("name", "label", "title", "id")
_GRAPH_TEXT_KEYS = ("description", "summary", "text", "body", "content")


@dataclass(frozen=True)
class MemoryFile:
    """One memory fact file from one project.

    Attributes:
        project: Owning project name.
        path: Absolute path to the file.
        name: Frontmatter title (file stem when missing).
        description: Frontmatter one-line summary.
        type: ``user`` | ``feedback`` | ``project`` | ``reference`` | ``unknown``.
        body: Markdown body without the frontmatter block.
    """

    project: str
    path: Path
    name: str
    description: str = ""
    type: str = "unknown"
    body: str = ""


@dataclass(frozen=True)
class ProjectMemory:
    """Everything one project remembers.

    Attributes:
        project: Project name.
        memory_path: The memory directory that was read.
        files: Parsed fact files (index/schema files excluded).
        index_present: Whether ``MEMORY.md`` exists.
        warnings: Non-fatal read problems.
    """

    project: str
    memory_path: Path | None
    files: tuple[MemoryFile, ...] = ()
    index_present: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryHit:
    """One search match.

    Attributes:
        file: The memory file that matched.
        line_number: 1-based line of the match within the body (0 = metadata).
        line: The matching line (or the description for metadata hits).
        score: The document's BM25 relevance.
        matched: How many distinct query terms the document contains. It sorts
            before ``score``: a note with more of the query ranks first.
    """

    file: MemoryFile
    line_number: int
    line: str
    score: float = 0.0
    matched: int = 0


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split ``---`` YAML frontmatter from the markdown body.

    TWO SHAPES ARE LIVE for the same fields (#217): flat (``type: reference``)
    and nested under ``metadata:`` — and the nested one is what the harness's
    own memory-writing instructions prescribe. Every value was stringified one
    level deep, so a nested block arrived as the *repr* of a dict
    (``"{'type': 'reference'}"``) and ``type`` read as ``unknown`` for a memory
    written to spec. A memory written to the documented format was second-class
    to the tool built to find it.

    A nested ``metadata:`` mapping's scalar entries are therefore folded into
    the top level as a FALLBACK. An explicit top-level key always wins: the flat
    form is the one that already worked, and a file carrying both most likely
    means the outer one. ``contracts/memory-format.md`` does not specify the
    field's location at all, so both spellings are honoured rather than one
    being declared wrong.
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        _log.debug("memory frontmatter is not valid YAML: %r", exc)
        return {}, parts[2]
    if not isinstance(meta, dict):
        return {}, parts[2]
    flat = {str(k): str(v) for k, v in meta.items()}
    nested = meta.get("metadata")
    if isinstance(nested, dict):
        # setdefault, not assignment: the outer key wins where both exist.
        for key, value in nested.items():
            if not isinstance(value, dict):
                flat.setdefault(str(key), str(value))
    return flat, parts[2]


def _read_memory_file(path: Path, project: str) -> MemoryFile | None:
    """Parse one memory markdown file; ``None`` when unreadable."""
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log.debug("cannot read memory file %s: %r", path, exc)
        return None
    meta, body = _split_frontmatter(text)
    return MemoryFile(
        project=project,
        path=path,
        name=meta.get("name", path.stem),
        description=meta.get("description", ""),
        type=meta.get("type", "unknown"),
        body=body.strip(),
    )


def _is_dir(path: Path) -> bool:
    """``Path.is_dir`` that answers False instead of raising; ADR-003.

    The bare call was the #210 defect in a second module: this function's own
    docstring says it never raises, and a stat raises for reasons that have
    nothing to do with the project being malformed — an unreadable parent, a
    dead mount. Found by the never-raise meta-test (#187), which is the point
    of having one.
    """
    try:
        return path.is_dir()
    except OSError as exc:
        _log.debug("cannot stat %s: %r", path, exc)
        return False


#: Bounds on a memory SOURCE's recursion, so a mispointed one cannot walk a home
#: directory (#306). A project's own memory directory is never recursive.
_SOURCE_MAX_DEPTH = 6
_SOURCE_MAX_FILES = 5000


def _source_markdown(root: Path, warnings: list[str]) -> list[Path]:
    """Every ``*.md`` under ``root``, bounded in depth and count; never raises (#306)."""
    found: list[Path] = []
    top = nested = 0
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        if depth >= _SOURCE_MAX_DEPTH:
            dirnames[:] = []
        for name in sorted(filenames):
            if name.endswith(".md"):
                if depth:
                    nested += 1
                elif name not in _INDEX_FILES:
                    top += 1
                if len(found) >= _SOURCE_MAX_FILES:
                    warnings.append(
                        f"memory source read stopped at {_SOURCE_MAX_FILES} files — not all indexed"
                    )
                    return found
                found.append(Path(dirpath) / name)
    if nested > top:
        warnings.append(
            f"memory source top level holds {top} markdown file(s), subdirectories "
            f"{nested} — all read, but this is not a flat memory directory"
        )
    return found


def _load_memory_dir(memory_path: Path, label: str, *, recursive: bool = False) -> ProjectMemory:
    """Read one memory directory's fact files under ``label``; never raises.

    A project's directory is flat by the memory-file contract, so it keeps the
    flat glob; a ``memory_sources`` directory makes no such promise (#306).
    """
    files: list[MemoryFile] = []
    warnings: list[str] = []
    candidates = (
        _source_markdown(memory_path, warnings) if recursive else sorted(memory_path.glob("*.md"))
    )
    for path in candidates:
        if path.name in _INDEX_FILES:
            continue
        parsed = _read_memory_file(path, label)
        if parsed is None:
            warnings.append(f"unreadable memory file: {path.name}")
        else:
            files.append(parsed)

    return ProjectMemory(
        project=label,
        memory_path=memory_path,
        files=tuple(files),
        index_present=(memory_path / "MEMORY.md").is_file(),
        warnings=tuple(warnings),
    )


def load_project_memory(descriptor: ProjectDescriptor) -> ProjectMemory:
    """Read one project's memory directory; never raises.

    Args:
        descriptor: The project whose memory to load.

    Returns:
        The project's memory; a missing directory yields an empty result
        with a warning rather than an error.
    """
    memory_path = descriptor.memory_path
    if memory_path is None or not _is_dir(memory_path):
        return ProjectMemory(
            project=descriptor.name,
            memory_path=memory_path,
            warnings=("no memory directory",),
        )
    return _load_memory_dir(memory_path, descriptor.name)


def memory_source_label(path: Path) -> str:
    """How hits from an external memory source are labelled (pure).

    The source's path, with the home directory written as ``~``. A path always
    contains a ``/``, and a project name never can (it is one directory name or
    a descriptor slug), so a source can never be mistaken for a project in the
    ``<project>/<file>`` locations the search prints.
    """
    text = str(path)
    home = str(Path.home())
    if home not in ("", "/") and (text == home or text.startswith(home + "/")):
        return "~" + text[len(home) :]
    return text


def load_memory_sources(
    paths: tuple[Path, ...], taken: tuple[Path | None, ...] = ()
) -> list[ProjectMemory]:
    """Read the fleet file's extra memory directories (#247); never raises.

    Each is read by the same loader as a project's memory, so it is searched,
    and ranked, alongside the projects'. A source that is missing or not a
    readable directory yields an empty memory carrying a warning that names it,
    never a crash and never a silent drop. A source that IS one of the projects'
    own memory directories (``taken``) is skipped, so no fact is counted twice.

    Args:
        paths: ``memory_sources`` from the fleet file, already ``~``-expanded.
        taken: The memory directories the projects already contribute.

    Returns:
        One :class:`ProjectMemory` per source, in the order declared.
    """
    seen = {_resolved(path) for path in taken if path is not None}
    memories: list[ProjectMemory] = []
    for path in paths:
        label = memory_source_label(path)
        if _resolved(path) in seen:
            _log.info("memory source %s is already a project's memory; read once", label)
            continue
        seen.add(_resolved(path))
        if not _can_list(path):
            memories.append(
                ProjectMemory(
                    project=label,
                    memory_path=path,
                    warnings=(f"memory source {label} is not a readable directory — skipped",),
                )
            )
            continue
        memories.append(_load_memory_dir(path, label, recursive=True))
    return memories


def _can_list(path: Path) -> bool:
    """Whether ``path`` is a directory this process can list; never raises.

    ``Path.glob`` swallows a permission error and yields nothing, so an
    unreadable source would otherwise read as an empty one: a silent drop.
    """
    if not _is_dir(path):
        return False
    try:
        with os.scandir(path) as entries:
            next(entries, None)
    except OSError as exc:
        _log.debug("cannot list %s: %r", path, exc)
        return False
    return True


def _resolved(path: Path) -> Path:
    """``path`` resolved, or as given when it cannot be; never raises."""
    try:
        return path.resolve()
    except (OSError, RuntimeError) as exc:
        _log.debug("cannot resolve %s: %r", path, exc)
        return path


def retrieval_mode(descriptor: ProjectDescriptor) -> str:
    """Pick a project's memory retrieval surface from its tier (pure).

    The project-init ADR-025 §4 reader rule: ``tier ≥ 3`` with an endpoint queries RAG;
    ``tier ≥ 2`` with a graph reads the graph; everything else greps the
    ``memory_path`` baseline. A higher tier only *offers* a richer surface —
    when the surface is undeclared (a tier-3 child that has not run its RAG
    setup, say) the mode degrades to the next one down.

    Args:
        descriptor: The project to choose a retrieval surface for.

    Returns:
        One of :data:`MODE_RAG`, :data:`MODE_GRAPH`, :data:`MODE_GREP`.
    """
    if descriptor.memory_tier >= TIER_RAG and descriptor.rag_endpoint:
        return MODE_RAG
    if descriptor.memory_tier >= TIER_GRAPH and descriptor.graph_path is not None:
        return MODE_GRAPH
    return MODE_GREP


def _first_str(node: dict[str, object], keys: tuple[str, ...]) -> str:
    """Return the first non-empty string value among ``keys`` (pure)."""
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def load_graph_facts(descriptor: ProjectDescriptor) -> tuple[MemoryFile, ...]:
    """Read a graphify graph's nodes as memory facts; never raises.

    Best-effort against the external graph schema: accepts a top-level list of
    nodes or a ``{"nodes": [...]}`` object, reads a name and prose from the
    common node keys, and returns nothing for an unreadable, oversized, or
    unrecognized graph (the caller keeps the grep baseline).
    """
    path = descriptor.graph_path
    if path is None:
        return ()
    try:
        if path.stat().st_size > _MAX_GRAPH_BYTES:
            return ()
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        _log.debug("graph file %s unreadable: %r", path, exc)
        return ()
    nodes = data.get("nodes") if isinstance(data, dict) else data
    if not isinstance(nodes, list):
        return ()
    facts: list[MemoryFile] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = _first_str(node, _GRAPH_NAME_KEYS)
        if not name:
            continue
        description = _first_str(node, _GRAPH_TEXT_KEYS)
        facts.append(
            MemoryFile(
                project=descriptor.name,
                path=path,
                name=name,
                description=description,
                type="graph",
                body=description,
            )
        )
    return tuple(facts)


def load_memory(descriptor: ProjectDescriptor) -> ProjectMemory:
    """Load one project's memory via its tier's retrieval surface; never raises.

    The grep baseline (``memory_path`` files) is always read — it is the
    anchor that never moves. At ``tier ≥ 2`` the graph's facts are *added* to
    it, so a query still finds everything grep would plus what only the graph
    knows. (RAG, :data:`MODE_RAG`, is a live query handled by the caller; this
    loader returns the local surfaces it can read without a network call.)
    """
    base = load_project_memory(descriptor)
    if descriptor.memory_tier >= TIER_GRAPH and descriptor.graph_path is not None:
        return replace(base, files=base.files + load_graph_facts(descriptor))
    return base


# BM25 (Robertson/Sparck Jones) over memory FILES, one fact file = one document.
# k1 and b are the textbook defaults: k1 caps how much a repeated term can add,
# b scales the length normalisation (0 = none, 1 = full).
BM25_K1 = 1.2
BM25_B = 0.75

# Field weights, BM25F-style: a term in the name counts three times and in the
# description twice. They keep the preference the hard-coded 3/2/1 scores used
# to encode, since the name and description are the highest-signal surfaces,
# while the rest of the score now comes from the corpus instead of a constant.
_FIELD_WEIGHTS = ((3, "name"), (2, "description"), (1, "body"))

_TOKEN = re.compile(r"\w+")


def _query_terms(query: str) -> tuple[str, ...]:
    """Split a query into distinct lowercased terms, in order (pure).

    Terms split on whitespace only, so ``fly.io`` stays one term and matches the
    way it always did.
    """
    return tuple(dict.fromkeys(query.lower().split()))


def _weighted_fields(memory_file: MemoryFile) -> tuple[tuple[int, str], ...]:
    """Each searchable field with its weight, lowercased (pure)."""
    return tuple((weight, getattr(memory_file, field).lower()) for weight, field in _FIELD_WEIGHTS)


def _term_frequency(fields: tuple[tuple[int, str], ...], term: str) -> int:
    """Weighted occurrences of ``term`` across a document's fields (pure).

    Matching is by SUBSTRING, as the scan it replaces was, so every document
    the old search found is still found: ``postgres`` still matches
    ``PostgreSQL``. BM25 changes the order, never the recall.
    """
    return sum(weight * text.count(term) for weight, text in fields)


def _document_length(fields: tuple[tuple[int, str], ...]) -> int:
    """Weighted token count, the length BM25 normalises by (pure)."""
    return sum(weight * len(_TOKEN.findall(text)) for weight, text in fields)


def _bm25_scores(files: list[MemoryFile], terms: tuple[str, ...]) -> list[tuple[float, int]]:
    """Score every document against ``terms``, with how many it matched (pure).

    ``(0.0, 0)`` means no term matched.

    IDF is the non-negative form ``ln(1 + (N - n + 0.5) / (n + 0.5))``. The
    classic form goes negative once a term is in more than half the documents,
    which would rank a document BELOW one that does not contain the term.
    """
    docs = [_weighted_fields(f) for f in files]
    lengths = [_document_length(d) for d in docs]
    total = len(docs)
    average = (sum(lengths) / total) if total else 0.0
    tfs = [[_term_frequency(d, term) for term in terms] for d in docs]
    idf = [
        math.log(1 + (total - n + 0.5) / (n + 0.5))
        for n in (sum(1 for row in tfs if row[i]) for i in range(len(terms)))
    ]
    scores: list[tuple[float, int]] = []
    for row, length in zip(tfs, lengths, strict=True):
        norm = BM25_K1 * (1 - BM25_B + BM25_B * (length / average if average else 0.0))
        score = sum(idf[i] * tf * (BM25_K1 + 1) / (tf + norm) for i, tf in enumerate(row) if tf)
        scores.append((score, sum(1 for tf in row if tf)))
    return scores


def _file_hits(
    memory_file: MemoryFile, terms: tuple[str, ...], score: float, matched: int
) -> list[MemoryHit]:
    """The lines of one matching document to show, each carrying its score (pure).

    One metadata hit (line 0) when a term is in the name or description, then
    every body line containing a term.
    """
    hits: list[MemoryHit] = []
    metadata = f"{memory_file.name}\n{memory_file.description}".lower()
    if any(term in metadata for term in terms):
        hits.append(
            MemoryHit(
                file=memory_file,
                line_number=0,
                line=memory_file.description,
                score=score,
                matched=matched,
            )
        )
    for number, line in enumerate(memory_file.body.splitlines(), start=1):
        lowered = line.lower()
        if any(term in lowered for term in terms):
            hits.append(
                MemoryHit(
                    file=memory_file,
                    line_number=number,
                    line=line.strip(),
                    score=score,
                    matched=matched,
                )
            )
    return hits


def search_memory(memories: list[ProjectMemory], query: str) -> list[MemoryHit]:
    """Search all loaded memories, ranked by BM25 relevance (pure).

    Each fact file is one document, and the corpus is every file across the
    memories passed in, so a term's rarity is judged fleet-wide. The query is
    split on whitespace into terms, and a document matches when it contains
    ANY term: ``descriptor drift`` finds a note that uses the two words apart.

    Order is by how many distinct terms a note contains, then by BM25
    (:data:`BM25_K1`, :data:`BM25_B`), with the name and description weighted
    above the body. Coverage comes first because BM25 alone does not guarantee
    it: its length normalisation can score a short note with one term above a
    long note with every term, and a note that answers the whole query should
    not be buried under one that answers half. Among notes matching the same
    number of terms, a rare term outranks a common one.

    Args:
        memories: Per-project memories (see :func:`load_project_memory`).
        query: Text to look for in names, descriptions, and bodies.

    Returns:
        Hits sorted by terms matched, then score, highest first, then
        project, file and line. A document's metadata hit (line 0) precedes
        its body lines.
    """
    terms = _query_terms(query)
    if not terms:
        return []

    files = [memory_file for memory in memories for memory_file in memory.files]
    hits: list[MemoryHit] = []
    for memory_file, (score, matched) in zip(files, _bm25_scores(files, terms), strict=True):
        if matched:
            hits.extend(_file_hits(memory_file, terms, score, matched))

    hits.sort(
        key=lambda h: (
            -h.matched,
            -h.score,
            h.file.project.lower(),
            h.file.path.name,
            h.line_number,
        )
    )
    return hits
