"""Console entry point: start the Rextio language server over stdio."""

from __future__ import annotations

import logging

from rextio_lsp.server import create_server


def main() -> None:
    """Start the server on stdio (blocking)."""
    logging.basicConfig(level=logging.WARNING)
    create_server().start_io()


if __name__ == "__main__":
    main()
