"""Delete files that no running task and no live share still needs.

    python3 tools/storage_sweep.py                # preview only, both targets
    python3 tools/storage_sweep.py --execute      # actually delete
    python3 tools/storage_sweep.py --only drive   # result drive only
    python3 tools/storage_sweep.py --only inbox   # Baidu transfer inbox only

The services do this on a timer by themselves (see ``autobook_linux.janitor``);
this is the manual equivalent, and the way the panel's cleanup button runs it.
Each target reads the env file of the role that owns it, so a target whose role
is not installed here is simply skipped.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autobook_linux.panel.envfile import read_env_file  # noqa: E402

CONFIG_DIR = Path(os.environ.get("ADMIN_CONFIG_DIR", "/etc/linux-autobook"))


def load_env(path: Path) -> bool:
    """Put an env file into the environment; False if it is not there."""
    if not path.is_file():
        return False
    for key, value in read_env_file(path).items():
        if value != "":
            os.environ[key] = value
    return True


def sweep_drive(execute: bool, grace_days: int | None, limit: int) -> None:
    from autobook_linux.config import Config
    from autobook_linux.drive_cleanup import from_config

    config = Config.load()
    if not (config.drive_base_url and config.drive_email):
        print("结果网盘未配置，跳过")
        return
    cleaner = from_config(
        config,
        grace_days=config.drive_cleanup_grace_days if grace_days is None else grace_days,
    )
    print(f"\n=== 结果网盘 {cleaner.base} 目录 {cleaner.uri} ===")
    print(f"分享有效期 {cleaner.expire_days} 天 + 宽限 {cleaner.grace_days} 天")
    result = cleaner.run(dry_run=not execute, limit=limit)
    print(result.summary())
    for sample in result.samples:
        print(f"  {sample}")
    if result.errors:
        print(f"错误 {len(result.errors)} 条，首条: {result.errors[0]}")


def sweep_inbox(execute: bool, hours: int | None) -> None:
    from autobook_linux.baidu_auth import BaiduCredentialStore, resolve_baidu_credentials
    from autobook_linux.baidu_pan import BaiduPanClient
    from autobook_linux.config import Config

    config = Config.load()
    credentials = resolve_baidu_credentials(
        config.bduss, config.stoken, config.baiduid,
        BaiduCredentialStore(config.baidu_auth_file),
    )
    client = BaiduPanClient(
        bduss=credentials.bduss, stoken=credentials.stoken, baiduid=credentials.baiduid,
        ptoken=credentials.ptoken, cookies=credentials.cookies,
        panweb=config.panweb, download_ua=config.download_ua,
    )
    window = config.baidu_inbox_orphan_hours if hours is None else hours
    print(f"\n=== 百度转存目录 {config.baidu_save_dir} ===")
    print(f"清除 {window} 小时前仍未被删除的残留文件")
    report = client.sweep_inbox(config.baidu_save_dir, window, dry_run=not execute)
    action = "已删除" if execute else "可清理"
    print(f"扫描 {report['scanned']} 个，{action} {report['deleted']} 个，"
          f"释放 {report['freed_bytes'] / 1024 / 1024:.1f} MB")
    for sample in report["samples"]:
        print(f"  {sample}")
    if report.get("error"):
        print(f"错误: {report['error']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="清理网盘中已过期或残留的文件")
    parser.add_argument("--execute", action="store_true", help="真正删除；缺省只预览")
    parser.add_argument("--only", choices=("drive", "inbox"), help="只清理其中一处")
    parser.add_argument("--grace-days", type=int, help="覆盖结果网盘的过期宽限天数")
    parser.add_argument("--orphan-hours", type=int, help="覆盖百度中转目录的残留时限")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if not args.execute:
        print("（预览模式，不会删除任何文件；加 --execute 才会真正删除）")

    if args.only != "inbox":
        if load_env(CONFIG_DIR / "worker.env"):
            sweep_drive(args.execute, args.grace_days, args.limit)
        elif args.only == "drive":
            print(f"未找到 {CONFIG_DIR / 'worker.env'}，本机没有 worker 角色")

    if args.only != "drive":
        if load_env(CONFIG_DIR / "gateway.env"):
            sweep_inbox(args.execute, args.orphan_hours)
        elif args.only == "inbox":
            print(f"未找到 {CONFIG_DIR / 'gateway.env'}，本机没有网关角色")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
