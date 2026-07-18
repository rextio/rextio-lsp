# Changelog

## 0.1.2 — 2026-07-18

Package version `0.1.2`. This release adds the tooling-contract 2.2 promotion-
assessment consumer surface while preserving legacy contract behavior.

### Added

- Show automatic native-promotion eligibility for undecorated functions.
- Preserve failed automatic probes as actionable Warning-level editor guidance.
- Show assessment status with each route CodeLens, including readable skip reasons.
- Use exact function-name ranges for hover and source ranges for CodeLens anchors.

### Changed

- Hide promotion hover, diagnostics, and CodeLens noise for proven `@rextio.exempt` functions.
- Trust the additive assessment fields only for tooling-contract `>=2.2.0,<3.0.0`.
- Keep contract 1.x, 2.0/2.1, malformed payloads, and unsupported majors on legacy behavior.

### Fixed

- Keep assessment blockers out of LSP Error severity while retaining improvement suggestions.
- De-duplicate matching legacy and assessment diagnostics without hiding unrelated diagnostics.
- Tolerate malformed marker, assessment, and range additions without dropping legacy reports.
- No-op cleanly when the optional `rextio` core is absent instead of failing package discovery.
- Withhold quick fixes on Python 3.11 tokenizer `ERRORTOKEN`s instead of risking malformed edits.

### Required release order

Release Train B deploys `rextio-lsp` **0.1.2** before core `rextio` **0.1.4**:
the tolerant LSP consumer is released first, and the tooling-contract **2.2.0**
producer follows. Core must not ship before or simultaneously with this LSP.

## 0.1.1 — 2026-07-14

Package version `0.1.1`. This release finalizes the dual-map work. It follows
the previous release, **0.1.0** (2026-07-12).

### Tooling contract dual-map

- Supports tooling-contract majors `{1, 2}` (not major-only `1`). Major 2 is
  the standardized producer: every diagnostic column, including `RXT000`, is a
  0-based UTF-8 byte offset. Major 1 retains the legacy `RXT000` special case
  (1-based Unicode code-point `SyntaxError.offset`). Non-`RXT000` diagnostics
  stay UTF-8-byte → UTF-16 on both majors.
- Closes the mixed-version hole where a major-1-only gate would silently accept
  a 2.x producer and misplace `RXT000`. Unsupported majors (e.g. 3+) still
  degrade to generic diagnostics.
- **Safe deployment order (required):** merge and release dual-map
  **rextio-lsp 0.1.1** first; then core **rextio 0.1.2** (tooling-contract
  major `2`, `contract_version` `2.0.0`); then **rextio-numpy 0.1.1**. Core
  must not ship alone first: a contract-2 producer against a major-1-only
  LSP would misplace `RXT000`. `rextio` remains a tooling-contract peer, not a
  runtime package dependency of this LSP.

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

- Contract columns (UTF-8 byte offsets; under contract 1.x, RXT000's 1-based
  code-point offset special-cased) are converted to UTF-16 LSP positions via
  document text.
- File URIs are converted with pygls' own helpers (Windows drive letters and
  UNC paths included).
- `**/rextio.toml` is watched (dynamic registration when the client supports
  it) and invalidates the per-root caches.
