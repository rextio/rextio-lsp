# Changelog

## 0.1.0 — 2026-07-12

Initial release of the Rextio LSP server (pygls >= 2.1; consumes the rextio
tooling contract — `rextio` itself is not a package dependency and the server
no-ops silently when it is absent).

### Contract-shaped analysis

- Data model is exactly the tooling contract: per-function `route` /
  `native_status` / `rejection_codes` + diagnostics from
  `rextio check --format json` (gated on `contract_version` major 1, with
  graceful degradation), and rule guidance + `config_fingerprint` from
  `rextio capabilities`.
- Acquisition: in-process (import the project's rextio, capture the CLI's
  JSON) with subprocess fallback; an explicit
  `initializationOptions.interpreter.path` and a project-venv rextio in a
  different environment than the server take precedence over in-process.
  Null/junk positions in the contract JSON are tolerated per field.
- Capabilities manifest cached by the spec's composite key
  (`config_fingerprint` + rextio version + plugin id@version list; a null
  plugin version is treated as uncacheable).

### Editor features (deliberately narrow)

- Diagnostics (`source: "rextio"`, RXT/RXTP codes; Warning only for rejected
  native candidates, Hint for informational codes, never Error), published
  on didOpen/didSave via a debounced, generation-guarded whole-project check
  (a mid-run `rextio.toml` change or deletion discards the stale result;
  stale diagnostics are cleared on didClose, on acquisition failure, and on
  config deletion).
- Hover on function definitions: route, native status, and manifest-sourced
  guidance for rejection and advisory codes.
- Code lens (`Rextio: <route>`, gated by `initializationOptions.codeLens.enable`)
  and a quick fix replacing `@rextio.native` with `@rextio.exempt`
  (tokenize-based span detection; the action is withheld whenever the edit
  cannot be proven safe).
- Nothing else is advertised: no completion, formatting, rename, definition,
  or references — the server coexists with a project's primary Python LSP.

### Positions and platforms

- Contract columns (UTF-8 byte offsets; RXT000's 1-based character offset
  special-cased) are converted to UTF-16 LSP positions via document text.
- File URIs are converted with pygls' own helpers (Windows drive letters and
  UNC paths included).
- `**/rextio.toml` is watched (dynamic registration when the client supports
  it) and invalidates the per-root caches.
