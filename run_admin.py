#!/usr/bin/env python3
"""Run the linux-autobook HTTPS administration panel."""
from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autobook_linux.panel import passwords, services  # noqa: E402
from autobook_linux.panel.server import serve  # noqa: E402
from autobook_linux.panel.settings import PanelSettings, supervisor_backend  # noqa: E402
from autobook_linux.panel.supervisor import Supervisor  # noqa: E402

LOGGER = logging.getLogger("autobook.panel")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    settings = PanelSettings.load()

    supervisor = None
    if supervisor_backend() == "internal":
        supervisor = Supervisor(settings.install_dir, settings.config_dir, settings.install_dir / "runtime")
        services.bind_supervisor(supervisor)
        supervisor.run()
        supervisor.autostart(settings.roles())
        LOGGER.info("内置进程管理器已启动，角色: %s", "、".join(settings.roles()))

        def shutdown(signum, _frame) -> None:
            LOGGER.info("收到信号 %s，正在停止托管进程", signum)
            supervisor.shutdown()
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

    try:
        if passwords.ensure_seeded(settings):
            LOGGER.info("已使用内置字典初始化解压密码文件")
    except Exception:
        LOGGER.exception("初始化解压密码字典失败")

    try:
        serve(settings)
    finally:
        if supervisor is not None:
            supervisor.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
