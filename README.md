# rextio-lsp

**Language Server for [Rextio](https://github.com/rextio/rextio) — native-promotion routes and guidance in your editor.**

A Python (pygls) LSP server that runs inside your project's environment, imports the Rextio analyzer in-process, and reports per-function execution routes (`native-direct`, `native-plugin:<id>`, `native-shim`, `fallback-python`, `fallback-accelerated:numba`, `rejected:<RXT>`) with actionable promotion guidance.

## Designed to coexist

rextio-lsp registers **only** Rextio-semantic capabilities and stays out of everything else:

- Provides: diagnostics (`source: "rextio"`, RXT/RXTP codes only, default Hint/Info severity), hover (route info + rejection and advisory guidance), code lens (per-function route badges), code actions (promotion quick fixes)
- Does **not** provide: completion, formatting, rename, definition, references, syntax/style linting — those remain with your existing Python LSP (Pylance/pyright, ruff, …)
- Activates only when `rextio.toml` exists in the workspace; silent no-op when Rextio isn't installed in the project environment

## Features

**M1**

- Whole-project `rextio check` on open/save (debounced), published as `source: "rextio"` diagnostics
- Severity is capped: rejection codes → Warning, informational codes (RXT075/080/090/091) → Hint, everything else → Information (never Error)
- Hover on a function definition line shows its route, native status, and rejection guidance (from the capability manifest)
- Acquisition prefers importing the project's own `rextio` in-process, falling back to a discovered `rextio` binary subprocess
- Tooling-contract majors `{1, 2}` fully supported: major 2 maps every column as 0-based UTF-8 bytes; major 1 keeps legacy `RXT000` 1-based code-point mapping. Other majors → degraded (generic) diagnostics without guidance enrichment

**M2**

- `initializationOptions` contract (see below): toggle code lens, pin the interpreter used for binary discovery
- Code lens: one `Rextio: <route>` lens per analyzed function, carrying the informational `rextio.showRouteInfo` command with `[qualname]` (registered only when `codeLens.enable` is true)
- Code actions (quickfix): on a rejected function that carries an explicit `@rextio.native` marker, offer *"Rextio: keep on Python fallback (@rextio.exempt)"* — rewrites the decorator to `@rextio.exempt` (indentation preserved)
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
- `interpreter.path` — path to the project's Python interpreter. When set, it is consulted **first** when locating the `rextio` binary for the subprocess fallback (before the project `.venv`/`venv` and `PATH`).

## Install

Install into the project environment so the server stays in version lock-step with the project's `rextio` and its plugins:

```console
pip install rextio-lsp
```

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

## License

MIT
