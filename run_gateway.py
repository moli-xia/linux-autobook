#!/usr/bin/env python3
"""Run the central, TLS-protected Baidu download gateway."""
from __future__ import annotations

import argparse
import logging

from autobook_linux.config import Config
from autobook_linux.gateway_server import GatewayManager, serve_gateway


def main() -> int:
    parser = argparse.ArgumentParser(description="autobook Baidu download gateway")
    parser.add_argument("--check", action="store_true", help="验证配置、百度登录和群文件库后退出")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    config = Config.load()
    try:
        config.validate_gateway()
    except RuntimeError as exc:
        logging.getLogger(__name__).error("配置未就绪，请先在管理面板完成快速设置：%s", exc)
        return 78
    manager = GatewayManager(config)
    manager.preflight()
    if args.check:
        print("网关预检通过：TLS 文件、百度登录和群文件库均可用。")
        return 0
    serve_gateway(config, manager)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
