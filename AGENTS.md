# rextio-lsp development guide

This repository implements **rextio-lsp**, a narrow pygls language server that
consumes Rextio’s tooling-contract JSON and surfaces native-promotion routes
and guidance in the editor.

`rextio` is **not** a runtime package dependency. The server is a
tooling-contract peer (JSON surfaces only).

---

## Release gate (cross-package)

Strict publish / deploy sequence — **no** simultaneous / before-or-with wording:

1. Merge and release **rextio-lsp 0.1.1** first (dual-map consumer).
2. Then release **core rextio 0.1.2** (tooling-contract major `2`,
   `contract_version` `2.0.0`).
3. Then release **rextio-numpy 0.1.1** (requires core `>= 0.1.2`).

Core must not ship alone first: a contract-2 producer against major-1-only
**rextio-lsp 0.1.0** can misplace `RXT000`.

### Tag / upload gate (before publishing this package)

Until a formal 0.1.1 tag and PyPI upload exist, README / CHANGELOG may correctly
state that this line is an **untagged / unuploaded RC** and that
`pip install rextio-lsp` still resolves published **0.1.0**.

**Before tagging and uploading 0.1.1**, replace or remove those statements so
the PyPI long description is not stale:

- [ ] Drop or rewrite “untagged / unuploaded / not on PyPI yet” wording in
      README and CHANGELOG for the 0.1.1 section
- [ ] Drop or rewrite the “pip still resolves 0.1.0” install note
- [ ] Confirm package version in `src/rextio_lsp/__about__.py` is the version
      being published
- [ ] Confirm dual-map majors `{1, 2}` and the deployment order above remain
      accurate
- [ ] Confirm 0.1.0 history in CHANGELOG is preserved as a separate section

Do **not** claim 0.1.1 is published before the tag/upload actually land.

---

## Product scope

### Must provide

1. Whole-project `rextio check --format json` diagnostics (`source: "rextio"`)
   on didOpen/didSave (debounced, generation-guarded).
2. Hover on function definitions: route, native status, rejection + advisory
   guidance from `rextio capabilities`.
3. Optional code lens (`Rextio: <route>`), gated by
   `initializationOptions.codeLens.enable`.
4. Quick fix: rewrite `@rextio.native` → `@rextio.exempt` when the edit is
   proven safe (tokenize-based span detection).
5. **Dual-map** tooling-contract majors `{1, 2}`:
   - major 2: every column (including `RXT000`) is 0-based UTF-8 byte offset;
   - major 1: legacy `RXT000` is 1-based Unicode code-point `SyntaxError.offset`;
   - non-`RXT000` stays UTF-8-byte → UTF-16 on both majors;
   - unsupported majors (e.g. 3+) → degraded generic diagnostics.
6. Silent no-op when `rextio` is absent or `rextio.toml` is missing.
7. Never advertise completion, formatting, rename, definition, or references.

### Severity contract

- Rejection codes → Warning
- Informational codes (`RXT075`, `RXT080`, `RXT090`, `RXT091`) → Hint
- Everything else → Information
- **Never** Error

### Compatibility floors

- Python `>= 3.11`
- `pygls >= 2.1, < 3`
- Package version source of truth: `src/rextio_lsp/__about__.py`
- Contract support: `SUPPORTED_CONTRACT_MAJORS = frozenset({1, 2})` in
  `src/rextio_lsp/contract.py`

---

## Non-goals

Do not implement these unless explicitly requested:

* Becoming a full Python language server (completion, rename, go-to-def, …)
* Importing rextio analyzer internals as the data model (contract JSON only)
* Adding a hard `rextio` package dependency
* Supporting tooling-contract major 3+ as a fully mapped major without a
  deliberate dual/triple-map design
* Shipping VS Code extension logic here (lives in `rextio-vscode`)
* Plugin lowering / NumPy native surface work (lives in core / `rextio-numpy`)

---

## Architecture

```text
Editor (LSP client)
  -> rextio-lsp (stdio, pygls)
       -> discovery: find rextio.toml / project root / rextio binary
       -> engine: acquire check + capabilities JSON
            (in-process or subprocess; order from interpreter + env match)
       -> contract: parse JSON into frozen dataclasses; dual-map columns
       -> server: diagnostics / hover / code lens / code actions
```

### Core acquisition

Both paths are supported; preference is **not** “always in-process when
co-installed”:

| Condition | Preferred path |
|---|---|
| Explicit `initializationOptions.interpreter.path` and a `rextio` binary next to that interpreter | **Subprocess** via that neighbour binary |
| Project-venv `rextio` in a **different** environment than the server | **Subprocess** via that project binary |
| Server and project share the same environment (common single-venv install) | **In-process** `import rextio` |
| Bare `PATH` discovery only | Does **not** displace in-process |

Whichever path is not preferred is still tried as a fallback. If both fail, the
server skips analysis for that command (silent no-op for the editor).

Module layout (`src/rextio_lsp/`):

| Module | Role |
|---|---|
| `__about__.py` | Version literal only (setuptools dynamic version) |
| `contract.py` | Tooling-contract parse + dual-map column helpers |
| `discovery.py` | Project root / config / binary discovery |
| `engine.py` | Acquire check/capabilities (in-process or subprocess) |
| `server.py` | pygls server, feature handlers, publish pipeline |
| `__main__.py` | `rextio-lsp` / `python -m rextio_lsp` entry |

Principles:

1. **Contract is the model.** Never import rextio internal types.
2. **Narrow surface.** Only Rextio-semantic capabilities.
3. **Generation-guarded analysis.** Drop stale whole-project runs after
   `rextio.toml` change/delete.
4. **Positions are hard.** Dual-map RXT000 carefully; always convert UTF-8
   bytes → UTF-16 with document text when available.
5. **Degrade, don't crash.** Unsupported majors and missing rextio are soft.
6. **Environment-aware acquisition.** Prefer the project’s rextio when the
   client pins an interpreter or the project venv differs from the server.

---

## Development commands

```console
pip install -e '.[dev]'
ruff check src tests
mypy
pytest -q
```

- Integration tests marked `needs_rextio` skip when `rextio` is not importable.
- Prefer unit tests with fixture JSON under `tests/fixtures/` over requiring a
  live core install for dual-map coverage.

---

## Documentation rules for agents

1. Work only under this repository. Do not invent release state for peer
   packages beyond the deployment-order gate above.
2. Distinguish **published 0.1.0** from an **unreleased 0.1.1 RC** until tag
   and upload land; then apply the tag/upload gate checklist.
3. State the release order as **lsp 0.1.1 → core 0.1.2 → numpy 0.1.1**.
   Never use “simultaneously”, “at the same time”, or “before or with”.
4. Describe acquisition as environment-aware (interpreter path + same/foreign
   venv), not “always prefer in-process when co-installed”.
5. Keep feature claims grounded in this branch’s code and tests.
6. Do not commit, push, tag, merge, create a PR, or publish unless the user
   explicitly asks.
7. Do not document or track untracked tool directories (for example
   `.claude-octopus/`).
8. Prefer editing tracked Markdown only when the task is documentation;
   leave source, tests, lockfiles, and generated artifacts alone unless asked.
9. User-facing README is the PyPI long description: avoid relative links that
   break on PyPI; use durable absolute GitHub URLs or omit internal-only files.

Primary user-facing docs: `README.md`, `CHANGELOG.md`.
