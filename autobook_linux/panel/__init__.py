"""linux-autobook web administration panel (version 2).

The panel is a small JSON API plus a single-page front-end.  It depends only
on the Python standard library so it can run before the application virtual
environment is fully provisioned.
"""
from __future__ import annotations

PANEL_VERSION = "2.0.0"

__all__ = ["PANEL_VERSION"]
