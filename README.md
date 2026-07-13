# rextio-lsp

**Language Server for [Rextio](https://github.com/rextio/rextio) — native-promotion routes and guidance in your editor.**

A Python (pygls) LSP server that consumes Rextio’s tooling-contract JSON and reports per-function execution routes (`native-direct`, `native-plugin:<id>`, `native-shim`, `fallback-python`, `fallback-accelerated:numba`, `rejected:<RXT>`) with actionable promotion guidance.

## Status: 0.1.1 release candidate

This repository branch is the **0.1.1 release candidate** for package version
`rextio-lsp` **0.1.1**. It is an **untagged / unuploaded** RC on this branch —
**not** a claim that 0.1.1 is published on PyPI. The last **published** cut is
**`rextio-lsp` 0.1.0** (2026-07-12 on PyPI).

### What 0.1.1 adds (dual-map)

- Tooling-contract majors **`{1, 2}`** are fully supported (0.1.0 gated major `1` only).
- **Major 2** (core 0.1.2 producer, `contract_version` `2.0.0`): every diagnostic column, including `RXT000`, is a 0-based UTF-8 byte offset.
- **Major 1** (published core through 0.1.1): legacy `RXT000` columns remain 1-based Unicode code-point `SyntaxError.offset`; all other codes stay UTF-8-byte → UTF-16.
- Unsupported majors (e.g. 3+) still degrade to generic diagnostics without guidance enrichment.

`rextio` is **not** a package dependency of this server. The server acquires the tooling-contract JSON via **in-process** import or a **discovered subprocess** (see Features), choosing order from the configured interpreter and environment match, and no-ops silently when Rextio is absent.

### Safe deployment order

Publish / deploy in this **strict** order — no simultaneous ship:

1. **`rextio-lsp` 0.1.1** (this dual-map consumer) — merge and release **first**
2. **core `rextio` 0.1.2** (tooling-contract major `2`)
3. **`rextio-numpy` 0.1.1** (requires core `>= 0.1.2`)

Core must not publish alone first: a contract-`2.x` producer against major-1-only **rextio-lsp 0.1.0** is an unsupported pairing and can misplace `RXT000`.

See the [CHANGELOG](https://github.com/rextio/rextio-lsp/blob/main/CHANGELOG.md) for the full RC notes and the preserved 0.1.0 history.

## Designed to coexist

rextio-lsp registers **only** Rextio-semantic capabilities and stays out of everything else:

- Provides: diagnostics (`source: "rextio"`, RXT/RXTP codes only; Warning for rejection codes, Hint for informational codes, Information otherwise — never Error), hover (route info + rejection and advisory guidance), code lens (per-function route badges), code actions (promotion quick fixes)
- Does **not** provide: completion, formatting, rename, definition, references, syntax/style linting — those remain with your existing Python LSP (Pylance/pyright, ruff, …)
- Activates only when `rextio.toml` exists in the workspace; silent no-op when Rextio isn't installed in the project environment

## Features

**M1**

- Whole-project `rextio check` on open/save (debounced), published as `source: "rextio"` diagnostics
- Severity is capped: rejection codes → Warning, informational codes (RXT075/080/090/091) → Hint, everything else → Information (never Error)
- Hover on a function definition line shows its route, native status, and rejection guidance (from the capability manifest)
- Acquisition supports both **in-process** (`import rextio`) and **subprocess** (discovered `rextio` binary). Order is environment-aware: an explicit `initializationOptions.interpreter.path` whose neighbouring `rextio` exists prefers that subprocess; a project-venv `rextio` in a **different** environment than the server also prefers subprocess; when the server and project share the same environment, in-process is used (equivalent, no spawn). The non-preferred path remains a fallback. A bare `PATH` hit does not displace in-process.
- Tooling-contract majors `{1, 2}` fully supported: major 2 maps every column as 0-based UTF-8 bytes; major 1 keeps legacy `RXT000` 1-based code-point mapping. Other majors → degraded (generic) diagnostics without guidance enrichment

**M2**

- `initializationOptions` contract (see below): toggle code lens, pin the interpreter used for binary discovery
- Code lens: one `Rextio: <route>` lens per analyzed function, carrying the informational `rextio.showRouteInfo` command with `[qualname]` (registered only when `codeLens.enable` is true)
- Code actions (quick fix): on a rejected function that carries an explicit `@rextio.native` marker, offer *"Rextio: keep on Python fallback (@rextio.exempt)"* — rewrites the decorator to `@rextio.exempt` (indentation preserved)
- Hover also surfaces an **Advisory** section for informational codes present on the function, not just rejections
- Watches `**/rextio.toml`; on change, drops the project's cached capability manifest and re-analyzes open documents
- Real diagnostic spans when the contract provides `end_line`/`end_column` (else a zero-width range)
- Latency instrumentation: each whole-project check is logged via `window/logMessage` (Info when > 2.0s, else Log)

## initializationOptions

The server reads the following shape (all keys optional; defaults shown):

```json
{
  "codeLens": { "enable": true },
  "interpreter": { "path": null }
}
```

- `codeLens.enable` — when `false`, the code lens capability is **not** advertised at all.
- `interpreter.path` — path to the project's Python interpreter. When set, the server looks for `rextio` next to that interpreter **first** (before project `.venv`/`venv` and `PATH`). If that neighbour binary exists, subprocess acquisition via it takes precedence over in-process; if it does not, discovery continues and in-process may still win.

## Install

Install into the project environment so the server stays in version lock-step with the project's `rextio` and its plugins:

```console
pip install rextio-lsp
```

> **Note:** `pip install rextio-lsp` currently resolves the **published** package (**0.1.0**) until 0.1.1 is tagged and uploaded. To exercise this dual-map RC, install from a source checkout of this branch (see Development).

The server speaks LSP over stdio via the `rextio-lsp` console script (equivalently `python -m rextio_lsp`).

## Editor setup

### Neovim (nvim-lspconfig, manual command)

rextio-lsp is not yet a built-in lspconfig server, so register it manually:

```lua
local configs = require("lspconfig.configs")
local lspconfig = require("lspconfig")

if not configs.rextio_lsp then
  configs.rextio_lsp = {
    default_config = {
      -- run the server from the project's own environment
      cmd = { ".venv/bin/rextio-lsp" },
      filetypes = { "python" },
      root_dir = lspconfig.util.root_pattern("rextio.toml"),
      init_options = {
        codeLens = { enable = true },
        interpreter = { path = vim.fn.getcwd() .. "/.venv/bin/python" },
      },
    },
  }
end

lspconfig.rextio_lsp.setup({})
```

Enable code lens rendering with `vim.lsp.codelens.refresh()` (e.g. on `BufEnter`/`CursorHold`) and `:lua vim.lsp.codelens.display()`.

### Generic stdio client

Any LSP client that launches a stdio subprocess works. The essentials:

- **command**: `rextio-lsp` (or `python -m rextio_lsp`) from the project environment
- **transport**: stdio
- **languages**: `python`
- **root**: nearest directory containing `rextio.toml`
- **initializationOptions**: the shape documented above

## Development

```console
pip install -e '.[dev]'
ruff check src tests
mypy
pytest -q
```

Integration tests marked `needs_rextio` are auto-skipped when `rextio` is not importable.

Contributor and agent guidance lives in the repository (`AGENTS.md`).

## Compatibility floors

| Component | Floor on this RC branch |
|---|---|
| Package version | `0.1.1` (RC; see `__about__.__version__`) |
| Python | `>= 3.11` |
| pygls | `>= 2.1, < 3` |
| Tooling-contract majors | `{1, 2}` fully supported; other majors → degraded |
| `rextio` package dep | **none** (peer contract consumer only) |

## License

MIT
