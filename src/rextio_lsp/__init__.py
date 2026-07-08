"""rextio-lsp: Language Server for the Rextio native-promotion tooling contract.

The server is a *consumer* of Rextio's machine-readable tooling contract
(``rextio check --format json`` and ``rextio capabilities --format json``). It
never imports rextio's internal analyzer models as its data model; the contract
JSON shapes, parsed in :mod:`rextio_lsp.contract`, are the model.
"""

from rextio_lsp.__about__ import __version__

__all__ = ["__version__"]
