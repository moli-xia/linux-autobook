"""Periodic storage cleanup shared by the worker and the gateway.

Two places grow without bound if nothing prunes them:

* the result drive, where every delivered PDF outlives the share that pointed
  at it (``drive_cleanup``);
* the Baidu inbox, where a transfer whose task timed out or crashed is left
  behind (``BaiduPanClient.sweep_inbox``).

A worker cleans the result drive it uploads to; the gateway — the only role
holding Baidu cookies — cleans the inbox.  Both run the same scheduler, which
sleeps in short slices so shutdown stays responsive.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

# Long enough that a restart loop cannot hammer the APIs, short enough that the
# first sweep after a deploy happens while someone is still watching.
STARTUP_DELAY_SECONDS = 120
TICK_SECONDS = 5


class Janitor:
    """Runs cleanup callables on a fixed interval in a daemon thread."""

    def __init__(
        self,
        tasks: dict[str, Callable[[], Any]],
        interval_hours: int,
        startup_delay: int = STARTUP_DELAY_SECONDS,
    ) -> None:
        self.tasks = tasks
        self.interval = max(1, int(interval_hours)) * 3600
        self.startup_delay = max(0, int(startup_delay))
        self.last_results: dict[str, Any] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> dict[str, Any]:
        """Run every task, isolating failures so one cannot skip the others."""
        results: dict[str, Any] = {}
        for name, task in self.tasks.items():
            try:
                results[name] = task()
            except Exception as exc:  # noqa: BLE001 - cleanup must never crash a service
                LOGGER.warning("清理任务 %s 失败: %s", name, exc)
                results[name] = {"error": str(exc)[:200]}
        self.last_results = results
        return results

    def _loop(self) -> None:
        if self._sleep(self.startup_delay):
            return
        while not self._stop.is_set():
            self.run_once()
            if self._sleep(self.interval):
                return

    def _sleep(self, seconds: float) -> bool:
        """Sleep in slices; True if we were asked to stop."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stop.wait(min(TICK_SECONDS, deadline - time.monotonic())):
                return True
        return self._stop.is_set()

    def start(self) -> None:
        if not self.tasks or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="janitor", daemon=True)
        self._thread.start()
        LOGGER.info("存储清理已启用: 每 %d 小时执行 %s", self.interval // 3600,
                    "、".join(self.tasks))

    def stop(self) -> None:
        self._stop.set()


def drive_task(config) -> Callable[[], Any]:
    from .drive_cleanup import from_config

    def run() -> Any:
        cleaner = from_config(config, grace_days=config.drive_cleanup_grace_days)
        return vars(cleaner.run(dry_run=False))

    return run


def inbox_task(config, client_factory: Callable[[], Any]) -> Callable[[], Any]:
    def run() -> Any:
        return client_factory().sweep_inbox(
            config.baidu_save_dir, config.baidu_inbox_orphan_hours)

    return run


def for_worker(config) -> Janitor:
    """A worker prunes the result drive it delivers to."""
    tasks: dict[str, Callable[[], Any]] = {}
    if config.cleanup_enabled and config.drive_base_url and config.drive_email:
        tasks["结果网盘"] = drive_task(config)
    return Janitor(tasks, config.cleanup_interval_hours)


def for_gateway(config, client_factory: Callable[[], Any]) -> Janitor:
    """The gateway holds the Baidu session, so it prunes the transfer inbox."""
    tasks: dict[str, Callable[[], Any]] = {}
    if config.cleanup_enabled and config.baidu_save_dir:
        tasks["百度转存目录"] = inbox_task(config, client_factory)
    return Janitor(tasks, config.cleanup_interval_hours)
