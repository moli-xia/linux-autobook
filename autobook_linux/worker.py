"""Concurrent worker main loop.

One serialized claim loop feeds a thread pool, so multiple books are
downloaded / converted / uploaded in parallel while task claiming stays
race-free against the site's next_pending() selection.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from autobook_linux.baidu_auth import BaiduCredentialStore, resolve_baidu_credentials
from autobook_linux.baidu_pan import BaiduPanClient
from autobook_linux.config import Config
from autobook_linux.library_index import LibraryIndex
from autobook_linux.pipeline import TaskPipeline, extract_ssno
from autobook_linux.site_client import SiteClient

LOGGER = logging.getLogger(__name__)


class Worker:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.site = SiteClient(config.site_base_url, config.worker_token, config.worker_id)
        credentials = resolve_baidu_credentials(
            config.bduss,
            config.stoken,
            config.baiduid,
            BaiduCredentialStore(config.baidu_auth_file),
        )
        self.baidu = BaiduPanClient(
            bduss=credentials.bduss,
            stoken=credentials.stoken,
            baiduid=credentials.baiduid,
            ptoken=credentials.ptoken,
            cookies=credentials.cookies,
            panweb=config.panweb,
            download_ua=config.download_ua,
            aria2c_bin=config.aria2c_bin,
            aria2_split=config.aria2_split,
            aria2_max_connection=config.aria2_max_connection,
            download_timeout_seconds=config.download_timeout_seconds,
        )
        self.index = LibraryIndex(config.index_db, self.baidu, config.full_sync_max_pages)
        self.pipeline = TaskPipeline(config, self.baidu, self.index)
        self.executor = ThreadPoolExecutor(max_workers=config.concurrency, thread_name_prefix="task")
        self._active: set[Future] = set()
        self._active_lock = threading.Lock()
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    def preflight(self) -> None:
        self.baidu.check_login()
        LOGGER.info("百度网盘登录态正常")
        gid = self.pipeline.gid  # resolve group once at startup
        first_page = next(self.baidu.iter_group_shares(gid), None)
        if first_page is None:
            LOGGER.warning("目标群文件库当前为空 gid=%s", gid)
        else:
            page, records = first_page
            LOGGER.info("目标群文件库访问正常 gid=%s page=%d records=%d", gid, page, len(records))
        self.baidu.ensure_remote_dir(self.config.baidu_save_dir)
        self.baidu.clear_dir(self.config.baidu_save_dir)  # drop stale transfers
        self.config.work_root.mkdir(parents=True, exist_ok=True)
        self.config.download_root.mkdir(parents=True, exist_ok=True)
        # warm up the WASM decoder once so worker threads never race on init
        sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))
        from pdg2pdf import get_wasm_module  # noqa: E402

        get_wasm_module()
        LOGGER.info("PDG WASM 解码器预热完成")

    def run(self, once: bool = False) -> int:
        LOGGER.info(
            "Worker 启动: site=%s worker_id=%s queue=%s concurrency=%d",
            self.config.site_base_url, self.config.worker_id,
            self.config.worker_queue, self.config.concurrency,
        )
        while not self._stop.is_set():
            self._reap()
            if len(self._active) >= self.config.concurrency:
                time.sleep(1)
                continue
            try:
                task = self.site.claim(self.config.worker_queue)
            except Exception as exc:
                LOGGER.warning("领取任务失败: %s", exc)
                time.sleep(self.config.poll_seconds)
                continue
            if not task:
                if once:
                    break
                time.sleep(self.config.poll_seconds)
                continue
            LOGGER.info("领取任务 #%s: %s", task.get("id"), task.get("book_title") or task.get("keyword"))
            future = self.executor.submit(self._run_task, task)
            with self._active_lock:
                self._active.add(future)
            if once:
                break

        if once or self._stop.is_set():
            while self._active:
                self._reap()
                time.sleep(1)
            self.executor.shutdown(wait=True, cancel_futures=False)
        return 0

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def _run_task(self, task: dict) -> None:
        token = str(task.get("token") or "")

        def progress(message: str) -> None:
            LOGGER.info("[#%s] %s", task.get("id"), message)
            if token:
                self.site.progress(token, message)

        try:
            # redirect_stdout mutates process-global state and is unsafe when
            # multiple conversion threads overlap. Keep converter output in
            # the shared service journal instead.
            result = self.pipeline.process(task, progress_cb=progress)
            self.site.complete(
                token,
                worker_status="completed",
                result_url=result["share_url"],
                result_file=str(result["pdf"]),
                message="文献传递完成。",
                raw_output="",
            )
            LOGGER.info("任务 #%s 完成: %s", task.get("id"), result["share_url"])
        except Exception as exc:
            LOGGER.exception("任务 #%s 失败: %s", task.get("id"), exc)
            if token:
                try:
                    self.site.complete(
                        token,
                        worker_status="failed",
                        message=str(exc)[:600],
                        raw_output=str(exc)[-60000:],
                    )
                except Exception:
                    LOGGER.exception("任务 #%s 失败回报也未成功", task.get("id"))

    def _reap(self) -> None:
        with self._active_lock:
            done = {f for f in self._active if f.done()}
            self._active -= done
        for future in done:
            exc = future.exception()
            if exc:
                LOGGER.error("任务线程异常退出: %s", exc)


def build_config() -> Config:
    config = Config.load()
    config.validate()
    return config
