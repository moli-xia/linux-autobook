#!/usr/bin/env python3
"""Run the linux-autobook HTTPS administration panel."""
from __future__ import annotations

import logging

from autobook_linux.admin_panel import AdminSettings, serve_admin


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    serve_admin(AdminSettings.load())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
