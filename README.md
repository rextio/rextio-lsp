# rextio-lsp

**Language Server for [Rextio](https://github.com/rextio/rextio) — native-promotion routes and guidance in your editor.**

A Python (pygls) LSP server that runs inside your project's environment, imports the Rextio analyzer in-process, and reports per-function execution routes (`native-direct`, `native-plugin:<id>`, `native-shim`, `fallback-python`, `fallback-accelerated:numba`, `rejected:<RXT>`) with actionable promotion guidance.

## Designed to coexist

rextio-lsp registers **only** Rextio-semantic capabilities and stays out of everything else:

- Provides: diagnostics (`source: "rextio"`, RXT/RXTP codes only, default Hint/Info severity), hover (route info), inlay hints / code lens (route badges), code actions (promotion quick fixes)
- Does **not** provide: completion, formatting, rename, definition, references, syntax/style linting — those remain with your existing Python LSP (Pylance/pyright, ruff, …)
- Activates only when `rextio.toml` exists in the workspace; silent no-op when Rextio isn't installed in the project environment

## Status

**Pre-development.** Scaffolded ahead of the Rextio core contract (Phase 0). v1 analysis strategy: on-save trigger, whole-project analysis with core-side per-module memoization.

Install (once released): `pip install rextio-lsp` into the project environment — this keeps the server in version lock-step with the project's `rextio` and its plugins.

## License

MIT
