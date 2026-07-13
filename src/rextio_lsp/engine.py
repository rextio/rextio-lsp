"""Contract acquisition: run rextio and parse its JSON into contract shapes.

Two acquisition paths exist:

1. **In-process** -- import the project's ``rextio`` and invoke its CLI ``main``
   with stdout captured, obtaining byte-identical JSON to the CLI. This is the
   production-preferred path for the common single-project case (the server
   ships into the project environment, so ``import rextio`` resolves the
   project's own analyzer + plugins).
2. **Subprocess** -- run a discovered ``rextio`` binary with ``--format json``.

Precedence between them (see :func:`_prefer_subprocess`):

* An explicit ``initializationOptions.interpreter.path`` always wins: the
  subprocess via that interpreter's neighbouring ``rextio`` takes precedence
  over in-process, which is only a fallback if that binary is missing.
* Otherwise, when the project root has a discoverable venv ``rextio`` that is
  NOT the server's own environment (a multi-root workspace where the server is
  not installed in every project's venv), that subprocess is preferred for that
  root. When the server IS in the project venv (same environment), in-process is
  used -- it is equivalent and avoids spawning a subprocess.

Both call ``check`` with ``--no-report`` so the project's ``.rextio/reports``
is never written. If neither path is available the acquisition returns ``None``
(silent no-op; the caller logs to LSP trace).

The capabilities manifest is cached keyed by its composite cache key (see
:meth:`CapabilityManifest.cache_key`) so the plugin-importing ``capabilities``
command runs at most once per resolved config + rextio + plugin versions.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from rextio_lsp.contract import (
    CapabilityManifest,
    ProjectReport,
    parse_capabilities,
    parse_check_report,
)
from rextio_lsp.discovery import _exe, find_project_venv_binary, find_rextio_binary

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
        raise AcquisitionError(
            f"rextio {argv[0]} produced no JSON (stderr: {err.getvalue().strip()})"
        )
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


def _is_server_environment(binary: Path) -> bool:
    """Whether ``binary`` lives in the LSP server's own environment.

    Compared against the server's environment via ``sys.prefix``, NOT by
    resolving ``sys.executable``: in the production layout ``.venv/bin/python``
    is a symlink whose ``resolve()`` escapes to the pyenv base, while
    ``.venv/bin/rextio`` resolves in place -- so resolving the interpreter would
    misclassify the server's OWN venv as a foreign environment and never take
    the in-process fast path. The primary check compares the binary's UNRESOLVED
    parent to ``<sys.prefix>/bin`` (``Scripts`` on Windows); a resolved-parent
    comparison is a secondary fallback for indirect layouts.
    """
    bindir = "Scripts" if sys.platform == "win32" else "bin"
    server_bindir = Path(sys.prefix) / bindir
    if binary.parent == server_bindir:
        return True
    try:
        return binary.resolve().parent == server_bindir.resolve()
    except OSError:  # pragma: no cover -- resolve() on a vanished path
        return False


def _prefer_subprocess(
    project_root: Path, binary: Path | None, *, interpreter_path: str | None
) -> bool:
    """Whether the discovered ``binary`` should take precedence over in-process.

    * An explicit ``interpreter_path`` wins, but ONLY when the discovered binary
      is that interpreter's own neighbour (``<interpreter dir>/rextio``): the
      client asked for that specific environment's rextio, so a bare ``PATH``
      fallback discovered *under* an explicit interpreter_path must not displace
      in-process.
    * Otherwise only a *project-venv* binary in a different environment than the
      server's own displaces in-process; a bare ``PATH`` hit or the server's own
      neighbour does not.

    ``binary is None`` returns False either way -- there is nothing to prefer;
    in-process runs first and the (absent) subprocess is a no-op fallback.
    """
    if binary is None:
        return False
    if interpreter_path:
        neighbour = Path(interpreter_path).parent / _exe("rextio")
        try:
            return neighbour.resolve() == binary.resolve()
        except OSError:  # pragma: no cover -- resolve() on a vanished path
            return False
    if _is_server_environment(binary):
        return False
    venv_binary = find_project_venv_binary(project_root)
    return venv_binary is not None and venv_binary.resolve() == binary.resolve()


def _try_in_process(command: str, argv: list[str]) -> dict[str, Any] | None:
    if not _rextio_available_in_process():
        return None
    try:
        return _run_in_process(argv)
    except AcquisitionError as exc:
        logger.warning("in-process rextio %s failed: %s", command, exc)
    except Exception as exc:  # noqa: BLE001 -- never let analysis crash the server
        logger.warning("in-process rextio %s raised %s", command, exc)
    return None


def _try_subprocess(command: str, binary: Path | None, argv: list[str]) -> dict[str, Any] | None:
    if binary is None:
        return None
    try:
        return _run_subprocess(binary, argv)
    except AcquisitionError as exc:
        logger.warning("subprocess rextio %s failed: %s", command, exc)
    except Exception as exc:  # noqa: BLE001 -- never let analysis crash the server
        # Mirrors _try_in_process: the specific OSError/SubprocessError cases are
        # wrapped inside _run_subprocess; this catches any unforeseen escape so
        # the other acquisition path still gets tried.
        logger.warning("subprocess rextio %s raised %s", command, exc)
    return None


def _acquire_json(
    project_root: Path,
    command: str,
    extra: list[str],
    *,
    interpreter_path: str | None = None,
) -> dict[str, Any] | None:
    """Return raw JSON for ``rextio <command> <root> --format json`` or ``None``.

    ``None`` means neither acquisition path was available/usable -- a silent
    no-op condition, not an error to surface to the user. Precedence between
    in-process and subprocess is decided by :func:`_prefer_subprocess`; whichever
    is not preferred is still tried as a fallback.
    """
    argv = [command, str(project_root), "--format", "json", *extra]
    binary = find_rextio_binary(project_root, interpreter_path)

    def subprocess_attempt() -> dict[str, Any] | None:
        return _try_subprocess(command, binary, argv)

    def in_process_attempt() -> dict[str, Any] | None:
        return _try_in_process(command, argv)

    order: tuple[Callable[[], dict[str, Any] | None], ...]
    if _prefer_subprocess(project_root, binary, interpreter_path=interpreter_path):
        order = (subprocess_attempt, in_process_attempt)
    else:
        order = (in_process_attempt, subprocess_attempt)

    for attempt in order:
        result = attempt()
        if result is not None:
            return result
    logger.info("rextio unavailable (in-process and subprocess); skipping %s", command)
    return None


class Engine:
    """Stateful acquisition facade with a fingerprint-keyed manifest cache."""

    def __init__(self) -> None:
        # Keyed by the manifest's composite cache key (config_fingerprint +
        # rextio_version + sorted plugin id@version); see cache_key().
        self._manifest_by_key: dict[str, CapabilityManifest] = {}
        self._key_by_root: dict[str, str] = {}
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

    def capabilities(
        self, project_root: Path, *, refresh: bool = False
    ) -> CapabilityManifest | None:
        """Return the capability manifest for ``project_root``.

        Cached per composite cache key (config fingerprint + rextio version +
        plugin id/version list): the first acquisition records the key for the
        root and reuses the manifest on later calls. When ``rextio.toml`` changes
        the server calls :meth:`invalidate` to drop the stale entry; pass
        ``refresh=True`` to force re-acquisition. A manifest that is not
        cache-safe (a plugin with a null version) is never cached and is
        re-acquired every call.
        """
        key = str(project_root)
        with self._lock:
            if not refresh:
                cache_key = self._key_by_root.get(key)
                if cache_key is not None:
                    cached = self._manifest_by_key.get(cache_key)
                    if cached is not None:
                        return cached

        data = _acquire_json(
            project_root, "capabilities", [], interpreter_path=self.interpreter_path
        )
        if data is None:
            return None
        manifest = parse_capabilities(data)

        cache_key = manifest.cache_key()
        if cache_key is None:
            # A plugin with a null version -> not cache-safe; re-acquire always.
            with self._lock:
                self._key_by_root.pop(key, None)
            return manifest

        with self._lock:
            self._key_by_root[key] = cache_key
            self._manifest_by_key.setdefault(cache_key, manifest)
            return self._manifest_by_key[cache_key]

    def invalidate(self, project_root: Path) -> None:
        """Drop the cached capability manifest for ``project_root``.

        Called when the project's ``rextio.toml`` changes. The manifest is only
        evicted when no other root still references its composite cache key
        (manifests are shared by key across roots with identical config).
        """
        key = str(project_root)
        with self._lock:
            cache_key = self._key_by_root.pop(key, None)
            if cache_key is not None and cache_key not in self._key_by_root.values():
                self._manifest_by_key.pop(cache_key, None)
