# RFC: C/C++ include resolution via compile_commands.json

## Part 1 — How the graph is created

The pipeline is a pure functional chain. Each stage is a single function in its own
module, communicating through plain Python dicts and NetworkX graphs — no shared
state, no side effects outside `graphify-out/`.

```
detect()  →  extract()  →  build()  →  cluster()  →  analyze()  →  report()  →  export()
```

### Stage 1: `detect.py` — File discovery & classification

`detect(root)` walks the directory tree with `os.walk()` and performs five jobs:

1. **Exclude noise dirs** — skips `.venv`, `node_modules`, `.git`, `__pycache__`,
   `dist`, `build`, `.next`, `.nuxt`, and ~40 other known build-artifact / cache
   directory names (see `_SKIP_DIRS` set).

2. **Ignore files** — reads `.gitignore` and `.graphifyignore` recursively up to
   the VCS root with `git` last-match-wins semantics, including `!` negation
   patterns. Also reads `$GIT_DIR/info/exclude` for worktree repos.

3. **Classify every file by extension** into one of five types:

   | `FileType` | Extensions |
   |---|---|
   | `CODE` | `.py`, `.js`, `.ts`, `.tsx`, `.go`, `.rs`, `.java`, `.cpp`, `.c`, `.h`, `.rb`, `.cs`, `.swift`, `.kt`, `.php`, `.scala`, `.lua`, `.zig`, `.sh`, `.sql`, `.json`, etc. (~80) |
   | `DOCUMENT` | `.md`, `.mdx`, `.txt`, `.rst`, `.html`, `.yaml`, `.yml`, `.docx`, `.xlsx` |
   | `PAPER` | `.pdf` (plus text files with ≥3 academic signals: arXiv IDs, `\cite`, DOIs, "we propose", etc.) |
   | `IMAGE` | `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg` |
   | `VIDEO` | `.mp4`, `.mov`, `.webm`, `.mkv`, `.avi` |

   Extensionless files with a known shebang (`#!/usr/bin/env python3`) are
   classified as `CODE`. Package manifests (`pyproject.toml`, `go.mod`, `pom.xml`,
   `package.json`) are routed to `CODE` for deterministic AST-style extraction.

4. **Skip sensitive files** — silently excludes `.env`, `.pem`, `id_rsa`,
   `credentials.json`, and any file inside `.ssh/`, `.aws/`, `.gcloud/`,
   `secrets/`, `credentials/` directories. Three-stage analysis avoids false
   positives: a file named `token-economics.md` is kept; `api_token.txt` is dropped.

5. **Corpus health check** — warns if < 50K words ("you may not need a graph") or
   > 500K words / > 500 files (token cost). PDF/docx word counts are cached.

Output: `dict[FileType, list[str]]` — absolute paths grouped by type.

### Stage 2: `extract.py` — Per-file structural extraction

`extract(paths)` runs a **two-pass** process on code files.

#### Pass 2a — Per-file tree-sitter AST parsing

Each code file is parsed with a **language-specific tree-sitter grammar**. The
project ships ~25 tree-sitter packages (Python, JS, TS, Go, Rust, Java, C, C++,
Ruby, C#, Kotlin, Swift, PHP, Lua, Scala, Zig, Bash, JSON, Groovy, etc.).

For each file, `_extract_generic(path, config)` walks the AST and collects:

```json
{
  "nodes": [
    {
      "id": "path_to_file_ClassName",
      "label": "ClassName",
      "file_type": "code",
      "source_file": "path/to/file.py",
      "source_location": "L42"
    }
  ],
  "edges": [
    {
      "source": "file_nid",
      "target": "module_nid",
      "relation": "calls|imports|inherits|contains|method",
      "confidence": "EXTRACTED|INFERRED|AMBIGUOUS"
    }
  ]
}
```

The generic walker:

1. Collects class/function/interface/enum **definitions** as nodes.
2. Walks **call expressions** — resolves the callee to a local symbol or emits
   a `calls` edge with the callee name.
3. Walks **import/include statements** via a language-specific `import_handler`
   (e.g. `_import_python`, `_import_js`, `_import_c`, `_import_java`).
4. Walks **inheritance / implements / type references** for OOP languages.
5. Marks function/class definitions as `_callable` for the cross-file pass.

Language-specific post-passes then run:

- **Python/JS/TS rationale extraction** — docstrings and `# NOTE:` / `// WHY:`
  comments become `rationale` nodes with `rationale_for` edges.
- **JS/TS doc-reference extraction** — `ADR-0011` / `RFC 793` citations in
  comments create `cites` edges.
- **Svelte/Astro/Vue** — regex fallbacks recover imports from template markup
  invisible to tree-sitter.

**Caching**: A content-hash-based AST cache at `graphify-out/cache/` avoids
re-parsing unchanged files. `graphify update` uses this for fast incremental
rebuilds.

#### Pass 2b — Cross-file resolution passes

After per-file extraction, three global passes run across all results:

1. **Symbol resolution** (`_augment_symbol_resolution_edges`) — resolves JS/TS
   imports through workspace manifests, `tsconfig.json` path aliases, and
   `package.json` `exports` maps. Python relative imports are resolved to
   absolute file paths. Creates `imports`, `imports_from`, `re_exports` edges.

2. **ID remapping** — normalizes node IDs from absolute-path-derived to canonical
   `{parent_dir}_{stem}` form so graph.json is portable across machines and AST
   IDs match semantic (LLM-generated) IDs.

3. **Decl/def merging** (`_merge_decl_def_classes`) — collapses C/C++/ObjC
   header-declared classes with their implementation-side definitions into single
   nodes.

#### Pass 2c — Semantic extraction (LLM-based, only for non-code files)

For **documents, papers, images, and video transcripts**, the pipeline calls an
LLM backend (Gemini, Claude, OpenAI, Kimi, Ollama, DeepSeek, Bedrock) via
`extract_corpus_parallel()`. This splits files into token-budgeted chunks,
prompts the LLM to extract entities and relationships, and returns `nodes`,
`edges`, and `hyperedges`. A separate content-hash-based **semantic cache**
persists LLM results independently of the AST cache.

### Stage 3: `build.py` — Graph assembly

`build(extractions)` merges all extraction results into a single NetworkX graph:

1. **Node deduplication** (3 layers):
   - Per-file `seen_ids` set prevents duplicate emission within a file.
   - NetworkX `G.add_node()` is idempotent — same ID overwrites attributes
     (semantic nodes win over AST nodes for richer labels).
   - Explicit `seen` set deduplicates cached + new semantic results.

2. **Ghost node merging** — LLM-extracted nodes sharing `(basename, label)` with
   an AST-extracted node are collapsed into the AST canonical node, preventing
   ghost duplicates. AST nodes always win. When two AST nodes share the same key
   (name collision across directories), the key is marked ambiguous and no merge
   occurs.

3. **ID normalization** — a `norm_to_id` map lets edges with slightly different
   casing/punctuation survive. Also registers old-stem ID forms as aliases so
   stale cached edges still resolve after ID migration.

4. **Cross-language phantom edge guard** — drops `INFERRED` `calls` edges
   between different language families, and drops `imports`/`references` edges
   when both endpoints are known code of different families.

5. **Direction preservation** — stores `_src`/`_tgt` attributes on edges so
   display functions show correct direction even in undirected graphs.

#### Incremental mode (`build_merge`)

For `graphify update`, loads existing `graph.json`, replaces changed files'
contributions, prunes deleted files' nodes/edges, and carries forward hyperedges
from unchanged files.

### Stage 4: `cluster.py` — Community detection

`cluster(G, resolution=1.0)` partitions the graph via the **Leiden algorithm**:

1. **Algorithm**: Leiden (graspologic) with `random_seed=42` and `trials=1` for
   deterministic output. Falls back to Louvain (built into NetworkX) if
   graspologic is not installed.

2. **Isolates** become single-node communities.

3. **Hub exclusion** (optional) — nodes above a degree percentile are excluded
   from partitioning and reattached by majority-vote neighbor community.

4. **Oversized community splitting** — communities > 25% of graph nodes (min 10)
   are recursively split with a second Leiden pass.

5. **Cohesion re-splitting** — communities with cohesion < 0.05 and ≥ 50 nodes
   are re-split (catches doc-hub nodes like `README.md` bridging unrelated
   subsystems).

6. **Deterministic indexing** — communities sorted by size descending, lexical
   tie-breaking → same grouping always gets the same integer IDs.

Output: `{community_id: [node_id, ...]}` — largest community = ID 0.

### Stage 5: `analyze.py` — Graph analysis

Computes three analysis artifacts:

1. **God Nodes** — top-N most-connected real entities by degree, excluding
   file-level hubs, concept/rationale nodes, JSON key noise, and built-in noise
   labels (`str`, `int`, `Path`, `Any`, `Mock`, etc.).

2. **Surprising Connections** — edges revealing non-obvious architecture, scored
   by: confidence weight (AMBIGUOUS=3 > INFERRED=2 > EXTRACTED=1), cross-file-type
   bonus, cross-repo bonus, cross-community bonus, peripheral→hub bonus, semantic
   similarity multiplier (1.5×). Structural suppression drops INFERRED cross-language
   code↔doc edges (resolver pollution).

3. **Suggested Questions** — ambiguous-edge questions, bridge-node questions,
   verification questions for god nodes with many INFERRED edges, exploration
   questions for isolated nodes, module-split questions for low-cohesion communities.

Also provides `find_import_cycles()` (circular import detection at file level).

### Stage 6: `report.py` — Human-readable report

Generates `GRAPH_REPORT.md` with: corpus check, summary statistics, community hub
navigation, god nodes, surprising connections, import cycles, hyperedges,
per-community breakdowns (cohesion + members), ambiguous edges for review,
knowledge gaps, work-memory lessons, and suggested questions.

### Stage 7: `export.py` — Output artifacts

Writes `graphify-out/` output: `graph.json`, `GRAPH_REPORT.md`,
`.graphify_analysis.json`, `.graphify_labels.json`. Optional: Obsidian vault,
GraphML, SVG visualization, Neo4j/FalkorDB push, interactive HTML.

---

## Part 2 — compile_commands.json include resolution

### Problem

The current C/C++ include resolution in `_resolve_c_include_path`
(`graphify/extractors/resolution.py:478`) is minimal:

```python
def _resolve_c_include_path(raw: str, str_path: str) -> Path | None:
    candidate = (Path(str_path).parent / raw).resolve()
    if candidate.is_file():
        return candidate
    return None
```

It only checks **one location** — the including file's own directory. This
resolves `#include "foo.h"` for same-directory headers but fails for:

- **Headers in separate `include/` directories** (`#include "core/engine.h"` → real path `include/core/engine.h`)
- **Build-system include paths** (`-I src/ -I third_party/libfoo/include`)
- **Generated headers** (protobuf `.pb.h` in build output directory)
- **CMake target interface includes** (propagated via `target_include_directories`)

When resolution fails, the fallback in `_import_c` (`graphify/extract.py:436`)
creates a stub node with only the leaf basename:

```python
module_name = raw.split("/")[-1].split(".")[0]
tgt_nid = _make_id(module_name)  # "engine" – never matches a real file node
```

This produces **dangling edges** — imports that point to orphan nodes instead of
the actual header's file node. The downstream `_merge_decl_def_classes` pass
(which collapses header-declared classes with `.cpp` implementations) can only
operate on resolved pairs, so it misses connections.

The impact on graph quality is significant for any C/C++ project with headers
outside the source directory — which is nearly every real project.

### What compile_commands.json provides

`compile_commands.json` is a JSON compilation database (generated by CMake with
`-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`, or by tools like `bear` for non-CMake
builds). Each entry records how a source file was compiled:

```json
{
  "directory": "/project/build",
  "command": "clang++ -I../include -I../third_party -DFOO_VERSION=3 -std=c++20 -c ../src/engine.cpp",
  "file": "../src/engine.cpp"
}
```

From this, graphify can extract:

| Field | Use |
|---|---|
| `-I <path>` flags | Include directories for resolving `#include "..."` |
| `-D <name>=<value>` | Future: tag nodes with build configuration context |
| `file` | Confirms which `.cpp` files are actually compiled (live code vs. dead) |
| `directory` | Resolves relative `-I` paths to absolute |

### Design

Follow the existing graphify pattern hierarchy for optional external data
sources:

```
CLI flag  >  environment variable  >  auto-discovery  >  current behavior (fallback)
```

This mirrors how `GRAPHIFY_API_TIMEOUT`, `GRAPHIFY_MAX_WORKERS`,
`GRAPHIFY_GOOGLE_WORKSPACE`, and LLM backend detection already work.

#### Tier 1 — Auto-discovery (zero-config)

Walk **upward** from the scan root, stopping at the VCS root, looking for
`compile_commands.json` in conventional locations:

```python
_COMPILE_COMMANDS_SEARCH_PATHS = [
    "build/compile_commands.json",              # CMake single-config
    "build/Release/compile_commands.json",      # CMake multi-config
    "build/Debug/compile_commands.json",
    "build/RelWithDebInfo/compile_commands.json",
    "build/MinSizeRel/compile_commands.json",
    "out/build/compile_commands.json",          # VS Code CMake Tools
    "compile_commands.json",                    # bear / bazel / project root
]
```

Stop at the first hit. Print a diagnostic so the user knows it was used:

```
[graphify extract] using compile_commands.json at build/ (142 entries, 27 unique include dirs)
```

The search ceiling is the VCS root (already computed in `detect.py`'s
`_find_vcs_root` for `.graphifyignore` resolution), preventing leakage outside
the repository.

If nothing is found, **fall through silently** to Tier 4 — current behavior.
A project without `compile_commands.json` behaves identically to today.

#### Tier 2 — Environment variable

`GRAPHIFY_COMPILE_COMMANDS` overrides auto-discovery:

```bash
# Point at the file directly
export GRAPHIFY_COMPILE_COMMANDS=/workspace/out/debug/compile_commands.json

# Point at a directory (finds compile_commands.json inside)
export GRAPHIFY_COMPILE_COMMANDS=/workspace/out/build

# Disable auto-discovery entirely, force current bare-resolution behavior
export GRAPHIFY_COMPILE_COMMANDS=""
```

Use cases: CI pipelines with known but non-standard locations, monorepos with
multiple build roots, and users who want to opt out.

Matches the `GRAPHIFY_GOOGLE_WORKSPACE` pattern — env var checked via a
dedicated function, overriding auto-detection when set.

#### Tier 3 — CLI flag

`--compile-commands PATH` on `graphify extract`:

```bash
graphify extract . --compile-commands ../build
graphify extract . --compile-commands /workspace/out/compile_commands.json
```

Takes the same `--flag PATH` form as `--postgres DSN` and `--out DIR`. CLI flag
sets the env var internally (following `--api-timeout`'s pattern at
`graphify/cli.py:2064`) so downstream code reads one source of truth.

#### Tier 4 — Current behavior (fallback)

When none of the above resolve, `_resolve_c_include_path` and `_import_c` behave
exactly as today — same-directory-only resolution, bare-name stubs for everything
else. Zero regression.

### Data model

A lightweight dataclass for parsed compile entries:

```python
# graphify/extractors/compile_db.py  (new module)

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

@dataclass
class CompileEntry:
    directory: Path          # working directory from the compile command
    include_dirs: list[Path] # -I flags, resolved to absolute paths
    defines: dict[str, str]  # -D FOO=bar  (for future conditional-tagging use)
```

### Data flow

```
cli.py / watch.py
  │
  ├─ parse --compile-commands flag
  ├─ check GRAPHIFY_COMPILE_COMMANDS env var
  └─ auto-discover compile_commands.json
       │
       ▼
  discover_compile_commands(root: Path) -> dict[Path, CompileEntry] | None
       │
       ▼
  extract(paths, ..., compile_commands=db)   # NEW kwarg, default None
       │
       ├─ per-file: look up db.get(Path(str_path))
       │    → CompileEntry with include_dirs
       │
       ▼
  _import_c(node, ..., include_dirs=entry.include_dirs)
       │
       ▼
  _resolve_c_include_path(raw, str_path, include_dirs=include_dirs)
       │
       ├─ 1. Same directory (current behavior)
       ├─ 2. Each include_dir  (NEW)
       └─ 3. None found → fallback to bare-name stub (current behavior)
```

### Modified function signatures

**`graphify/extractors/resolution.py`** — `_resolve_c_include_path` gains an
optional parameter:

```python
def _resolve_c_include_path(
    raw: str,
    str_path: str,
    include_dirs: list[Path] | None = None,
) -> Path | None:
    # 1. Same directory (current behavior — always tried)
    candidate = (Path(str_path).parent / raw).resolve()
    if candidate.is_file():
        return candidate

    # 2. Include directories from compile_commands.json (NEW)
    if include_dirs:
        for inc in include_dirs:
            candidate = (inc / raw).resolve()
            if candidate.is_file():
                return candidate

    return None
```

**`graphify/extract.py`** — `_import_c` gains an `include_dirs` parameter:

```python
def _import_c(
    node, source, file_nid, stem, edges, str_path,
    scope_stack=None,
    include_dirs=None,   # NEW
) -> None:
    ...
    if child.type != "system_lib_string":
        resolved = _resolve_c_include_path(raw, str_path, include_dirs)  # passes through
```

**`graphify/extract.py`** — `extract()` gains a `compile_commands` kwarg:

```python
def extract(
    paths: list[Path],
    cache_root: Path | None = None,
    *,
    parallel: bool = True,
    max_workers: int | None = None,
    compile_commands: dict[Path, CompileEntry] | None = None,  # NEW
) -> dict:
```

The per-file extraction dispatch would pass include dirs through to
`_extract_generic` and then to the language-specific `import_handler`. For C
and C++ (the only consumers), the include dirs are looked up from the
`compile_commands` dict keyed by source file path.

### Implementation footprint

| File | Change |
|---|---|
| **New: `graphify/extractors/compile_db.py`** | `discover_compile_commands(root) → dict[Path, CompileEntry] \| None` — auto-discovery, JSON parsing, `-I` extraction, path resolution |
| **`graphify/extractors/resolution.py`** | `_resolve_c_include_path` gains `include_dirs` parameter |
| **`graphify/extract.py`** | `extract()` gains `compile_commands` kwarg; `_import_c` passes include dirs; per-file dispatch forwards data |
| **`graphify/cli.py`** | `--compile-commands` flag, `GRAPHIFY_COMPILE_COMMANDS` env var check, auto-discovery call, passed to `extract()` |
| **`graphify/watch.py`** | Re-run discovery on each `update` cycle (compile_commands.json may change after CMake re-run) |

### Non-goals

- **Parsing system headers** (`#include <...>`). These are deliberately skipped
  to avoid noise from STL/libc headers.
- **Preprocessor conditional evaluation**. tree-sitter parses all branches of
  `#ifdef`/`#else`/`#endif` regardless of defines. `-D` flags are stored for
  future node-tagging use but do not affect AST parsing.
- **Template instantiation**. tree-sitter has no concept of template
  instantiation; `compile_commands.json` does not change this.
- **Non-CMake build systems out of the box**. `compile_commands.json` is the
  standard format; `bear` generates it for Make, `bazel` can with
  `--experimental_export_compile_commands`. The auto-discovery locations are
  conventional but extensible.

### Edge cases & error handling

| Scenario | Behavior |
|---|---|
| `compile_commands.json` found but unparseable | Print warning, fall through to current behavior |
| `compile_commands.json` found but contains zero entries | Print warning, fall through |
| `-I` path no longer exists on disk | Skip that include dir, try remaining ones |
| `-I` path is relative but `directory` field is absent/malformed | Resolve relative to scan root, warn if unresolvable |
| `--compile-commands` points to non-existent file | Print error, exit (explicit user action = user deserves feedback) |
| `GRAPHIFY_COMPILE_COMMANDS=""` (empty) | Skip all tiers, use current behavior |
| File in compile_commands.json has no `-I` flags | CompileEntry has empty `include_dirs` — same-directory resolution only |
| Multiple entries for the same source file | Use the first entry's include dirs |
| `compile_commands.json` contains entries for files outside the scan root | Skip those entries (they won't be in the detected file list) |

### Backward compatibility

All existing callers of `extract()`, `_import_c`, and `_resolve_c_include_path`
are unaffected — new parameters have default `None`, producing identical behavior.
The `LanguageConfig` structs (`_C_CONFIG`, `_CPP_CONFIG`) are unchanged.
Auto-discovery only triggers when no explicit config is provided and a
`compile_commands.json` is found in a conventional location — for the vast
majority of non-C++ projects, this is a no-op.
