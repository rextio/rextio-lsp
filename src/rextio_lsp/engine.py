"""Contract acquisition: run rextio and parse its JSON into contract shapes.

Two acquisition paths, tried in order:

1. **In-process** -- import the project's ``rextio`` and invoke its CLI ``main``
   with stdout captured, obtaining byte-identical JSON to the CLI. This is the
   production-preferred path (the server ships into the project environment, so
   ``import rextio`` resolves the project's own analyzer + plugins).
2. **Subprocess fallback** -- run a discovered ``rextio`` binary with
   ``--format json``. Used when ``import rextio`` fails in this interpreter.

Both call ``check`` with ``--no-report`` so the project's ``.rextio/reports``
is never written. If neither path is available the acquisition returns ``None``
(silent no-op; the caller logs to LSP trace).

The capabilities manifest is cached keyed by its ``config_fingerprint`` so the
plugin-importing ``capabilities`` command runs at most once per resolved config.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from rextio_lsp.contract import (
    CapabilityManifest,
    ProjectReport,
    parse_capabilities,
    parse_check_report,
)
from rextio_lsp.discovery import find_rextio_binary

logger = logging.getLogger("rextio_lsp.engine")

# ``redirect_stdout`` swaps the process-wide ``sys.stdout`` while the in-process
# CLI runs. Serialize acquisitions so two concurrent analyses cannot interleave
# their capture buffers. (pygls' stdio transport holds the original
# ``stdout.buffer`` captured at startup, so this redirect does not corrupt the
# JSON-RPC stream.)
_INPROCESS_LOCK = threading.Lock()

_SUBPROCESS_TIMEOUT_SECONDS = 120


class AcquisitionError(RuntimeError):
    """Raised internally when a chosen acquisition path fails to produce JSON."""


def _rextio_available_in_process() -> bool:
    import importlib.util

    return importlib.util.find_spec("rextio.cli.main") is not None


def _run_in_process(argv: list[str]) -> dict[str, Any]:
    from rextio.cli.main import main as rextio_main

    out = io.StringIO()
    err = io.StringIO()
    with _INPROCESS_LOCK, redirect_stdout(out), redirect_stderr(err):
        try:
            rextio_main(argv)
        except SystemExit as exc:  # argparse edge cases
            raise AcquisitionError(f"rextio main raised SystemExit({exc.code})") from exc
    payload = out.getvalue().strip()
    if not payload:
        raise AcquisitionError(f"rextio {argv[0]} produced no JSON (stderr: {err.getvalue().strip()})")
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AcquisitionError(f"rextio {argv[0]} JSON parse failed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AcquisitionError(f"rextio {argv[0]} JSON was not an object")
    return parsed


def _run_subprocess(binary: Path, argv: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [str(binary), *argv],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AcquisitionError(f"rextio subprocess failed: {exc}") from exc
    payload = completed.stdout.strip()
    if not payload:
        raise AcquisitionError(
            f"rextio subprocess produced no JSON (rc={completed.returncode}, "
            f"stderr: {completed.stderr.strip()})"
        )
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AcquisitionError(f"rextio subprocess JSON parse failed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AcquisitionError("rextio subprocess JSON was not an object")
    return parsed


def _acquire_json(
    project_root: Path,
    command: str,
    extra: list[str],
    *,
    interpreter_path: str | None = None,
) -> dict[str, Any] | None:
    """Return raw JSON for ``rextio <command> <root> --format json`` or ``None``.

    ``None`` means neither acquisition path was available/usable -- a silent
    no-op condition, not an error to surface to the user. ``interpreter_path``
    (from ``initializationOptions``) is consulted first when locating the
    subprocess-fallback binary.
    """
    argv = [command, str(project_root), "--format", "json", *extra]

    if _rextio_available_in_process():
        try:
            return _run_in_process(argv)
        except AcquisitionError as exc:
            logger.warning("in-process rextio %s failed, trying subprocess: %s", command, exc)
        except Exception as exc:  # noqa: BLE001 -- never let analysis crash the server
            logger.warning("in-process rextio %s raised %s, trying subprocess", command, exc)

    binary = find_rextio_binary(project_root, interpreter_path)
    if binary is None:
        logger.info("rextio unavailable in-process and no binary found; skipping %s", command)
        return None
    try:
        return _run_subprocess(binary, argv)
    except AcquisitionError as exc:
        logger.warning("subprocess rextio %s failed: %s", command, exc)
        return None


class Engine:
    """Stateful acquisition facade with a fingerprint-keyed manifest cache."""

    def __init__(self) -> None:
        self._manifest_by_fingerprint: dict[str, CapabilityManifest] = {}
        self._fingerprint_by_root: dict[str, str] = {}
        self._lock = threading.Lock()
        # Set from ``initializationOptions.interpreter.path``; consulted first
        # when locating the subprocess-fallback rextio binary.
        self.interpreter_path: str | None = None

    def check(self, project_root: Path) -> ProjectReport | None:
        """Run a whole-project ``check`` and parse it, or ``None`` on no-op."""
        data = _acquire_json(
            project_root, "check", ["--no-report"], interpreter_path=self.interpreter_path
        )
        if data is None:
            return None
        return parse_check_report(data)

    def capabilities(self, project_root: Path, *, refresh: bool = False) -> CapabilityManifest | None:
        """Return the capability manifest for ``project_root``.

        Cached per resolved config: the first acquisition records the
        ``config_fingerprint`` for the root and reuses the manifest on later
        calls. When ``rextio.toml`` changes the server calls :meth:`invalidate`
        to drop the stale entry; pass ``refresh=True`` to force re-acquisition.
        """
        key = str(project_root)
        with self._lock:
            if not refresh:
                fp = self._fingerprint_by_root.get(key)
                if fp is not None:
                    cached = self._manifest_by_fingerprint.get(fp)
                    if cached is not None:
                        return cached

        data = _acquire_json(
            project_root, "capabilities", [], interpreter_path=self.interpreter_path
        )
        if data is None:
            return None
        manifest = parse_capabilities(data)

        with self._lock:
            self._fingerprint_by_root[key] = manifest.config_fingerprint
            self._manifest_by_fingerprint.setdefault(manifest.config_fingerprint, manifest)
            return self._manifest_by_fingerprint[manifest.config_fingerprint]

    def invalidate(self, project_root: Path) -> None:
        """Drop the cached capability manifest for ``project_root``.

        Called when the project's ``rextio.toml`` changes. The manifest is only
        evicted when no other root still references its ``config_fingerprint``
        (manifests are shared by fingerprint across roots with identical config).
        """
        key = str(project_root)
        with self._lock:
            fp = self._fingerprint_by_root.pop(key, None)
            if fp is not None and fp not in self._fingerprint_by_root.values():
                self._manifest_by_fingerprint.pop(fp, None)
