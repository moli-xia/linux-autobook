#!/usr/bin/env python3
"""Run the linux-autobook HTTPS administration panel."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autobook_linux.panel.server import serve  # noqa: E402
from autobook_linux.panel.settings import PanelSettings  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    serve(PanelSettings.load())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
