"""Package version, isolated from ``__init__`` side effects.

Mirrors rextio core's pattern: setuptools reads
``[tool.setuptools.dynamic] version = {attr = "rextio_lsp.__about__.__version__"}``
by parsing this module's AST, so keeping it a single literal assignment avoids
importing the full package at build time.
"""

__version__ = "0.1.2"
