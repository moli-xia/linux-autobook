#!/usr/bin/env python3
"""Entry point for the Linux document-delivery worker.

Usage:
  python run_worker.py              # run forever (claim + process concurrently)
  python run_worker.py --once       # claim at most one task, process, exit
  python run_worker.py --sync-index # legacy/manual index diagnostic only
  python run_worker.py --check      # config/login/group preflight, then exit
  python run_worker.py --baidu-login # scan QR and persist Baidu web cookies
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autobook_linux.baidu_auth import (  # noqa: E402
    BaiduCredentialStore,
    BaiduQrLogin,
    BaiduQrLoginError,
)
from autobook_linux.baidu_pan import BaiduPanClient  # noqa: E402
from autobook_linux.config import Config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="autobook linux worker")
    parser.add_argument("--once", action="store_true", help="只处理一个任务后退出")
    parser.add_argument("--sync-index", action="store_true", help="遗留诊断：手工同步群文件库索引")
    parser.add_argument("--full", action="store_true", help="配合 --sync-index 做完整同步")
    parser.add_argument("--check", action="store_true", help="预检配置/登录/群组后退出")
    parser.add_argument("--baidu-login", action="store_true", help="使用百度网盘 App 扫码登录并保存 Cookie")
    parser.add_argument("--qr-output", type=Path, help="扫码二维码 PNG 的保存路径")
    parser.add_argument("--login-timeout", type=int, help="等待扫码确认的秒数")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = Config.load()
    if args.baidu_login:
        return baidu_login(config, args.qr_output, args.login_timeout)
    try:
        config.validate()
    except RuntimeError as exc:
        logging.getLogger(__name__).error("配置未就绪，请先在管理面板完成快速设置：%s", exc)
        return 78
    from autobook_linux.worker import Worker

    worker = Worker(config)
    worker.preflight()

    if args.check:
        print("预检通过：配置完整，百度下载来源可用。")
        return 0
    if args.sync_index:
        if worker.index is None:
            parser.error("网关模式不在 Worker 上维护百度群文件索引；请在网关节点使用直连模式执行")
        count = worker.index.sync(worker.pipeline.gid, incremental=not args.full)
        print(f"索引同步完成，写入 {count} 条。")
        return 0

    def request_stop(signum, _frame) -> None:
        logging.getLogger(__name__).info("收到信号 %s，停止领取新任务并等待活动任务结束", signum)
        worker.stop()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    return worker.run(once=args.once)


def baidu_login(config: Config, qr_output: Path | None, login_timeout: int | None) -> int:
    image_path = qr_output or config.baidu_qr_path
    timeout = login_timeout or config.baidu_qr_timeout_seconds
    login = BaiduQrLogin(proxy=config.baidu_proxy)
    try:
        challenge = login.generate(image_path)
        print(f"二维码已保存: {challenge.image_path}")
        print(f"也可在浏览器打开: {challenge.image_url}")
        print("请使用百度网盘 App 扫码并在手机上确认登录。")

        status_text = {
            "waiting": "等待扫码...",
            "scanned": "已扫码，等待手机确认...",
            "confirmed": "已确认，正在建立网盘会话...",
        }
        credentials = login.wait_for_login(
            challenge,
            timeout=max(30, timeout),
            status_callback=lambda status: print(status_text.get(status, status)),
        )
        BaiduCredentialStore(config.baidu_auth_file).save(credentials)

        client = BaiduPanClient(
            bduss=credentials.bduss,
            stoken=credentials.stoken,
            baiduid=credentials.baiduid,
            ptoken=credentials.ptoken,
            cookies=credentials.cookies,
        )
        client.check_login()
        groups = client.list_groups()
        print(f"扫码登录成功，凭据已安全保存；检测到 {len(groups)} 个百度网盘群组。")
        if not config.baidu_group_gid:
            try:
                client.resolve_gid(config.baidu_group_name)
            except RuntimeError as exc:
                print(
                    f"登录账号不可用于当前 Worker: {exc}。"
                    "请用已加入目标群的百度账号重新扫码，或配置 BAIDU_GROUP_GID。",
                    file=sys.stderr,
                )
                return 3
        return 0
    except Exception as exc:
        print(f"扫码登录失败: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
