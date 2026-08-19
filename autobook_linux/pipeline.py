"""Per-task pipeline: SS code -> group library -> download -> extract/convert -> upload.

Replaces the whole Windows GUI chain (BaiduNetdisk client + 7-Zip GUI +
Pdg2Pic GUI) with pure API/CLI steps, so any number of tasks can run
concurrently on a headless Linux server.
"""
from __future__ import annotations

import logging
import re
import shutil
import sys
import time
from pathlib import Path

from autobook_linux.archive import (
    ARCHIVE_SUFFIXES,
    extract_archive,
    find_files_by_suffix,
    looks_like_archive,
)
from autobook_linux.baidu_pan import BaiduPanClient
from autobook_linux.config import Config
from autobook_linux.gateway_client import BaiduGatewayClient
from autobook_linux.library_index import LibraryIndex

sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))
from pdg2pdf import PdgConverter  # noqa: E402  (vendored, MIT, bj5/pdg2pdf_open)
from upload_to_drive import upload_file as drive_upload_file  # noqa: E402

LOGGER = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    pass


def extract_ssno(task: dict) -> str:
    if task.get("ssno"):
        return str(task["ssno"]).strip()
    for value in (task.get("book_title"), task.get("keyword")):
        if not value:
            continue
        match = re.search(r"_(\d{8})_", str(value))
        if match:
            return match.group(1)
        match = re.search(r"(?:^|\D)(\d{8})(?:\D|$)", str(value))
        if match:
            return match.group(1)
    return ""


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.rstrip(". ")


def preferred_pdf_name(book_title: str, ssno: str, fallback_stem: str) -> str:
    title = sanitize_filename(book_title or "")
    stem = f"{title}_{ssno}" if title else (fallback_stem or ssno)
    return f"{stem}.pdf"


class TaskPipeline:
    def __init__(
        self,
        config: Config,
        baidu: BaiduPanClient | None = None,
        index: LibraryIndex | None = None,
        gateway: BaiduGatewayClient | None = None,
    ) -> None:
        self.config = config
        self.baidu = baidu
        self.index = index
        self.gateway = gateway
        if gateway is None and (baidu is None or index is None):
            raise ValueError("直连模式需要 baidu 和 index；网关模式需要 gateway")
        self._gid: str | None = None

    @property
    def gid(self) -> str:
        if self._gid is None:
            if self.config.baidu_group_gid:
                self._gid = self.config.baidu_group_gid
            else:
                if self.baidu is None:
                    raise PipelineError("网关模式不能在 Worker 端解析群组 gid")
                self._gid = self.baidu.resolve_gid(self.config.baidu_group_name)
            LOGGER.info("群组 gid=%s", self._gid)
        return self._gid

    # ------------------------------------------------------------------
    def process(self, task: dict, progress_cb=lambda msg: None) -> dict:
        """Run one task end to end. Returns {"share_url":..., "pdf": Path}."""
        task_id = task.get("id")
        ssno = extract_ssno(task)
        book_title = str(task.get("book_title") or task.get("keyword") or "")
        if not ssno:
            raise PipelineError("任务缺少 SS 号，无法处理")

        job_dir = self.config.work_root / f"task_{task_id}_{ssno}"
        dl_dir = self.config.download_root / f"task_{task_id}_{ssno}"
        job_dir.mkdir(parents=True, exist_ok=True)
        dl_dir.mkdir(parents=True, exist_ok=True)
        try:
            if self.gateway is not None:
                progress_cb(f"正在通过百度下载网关检索并下载 SS={ssno}")
                request_id = f"task-{task_id}-{task.get('lease_id') or task.get('token') or ssno}"
                downloaded, source_name = self.gateway.fetch(ssno, request_id, dl_dir)
                LOGGER.info("网关下载完成: %s", source_name)
            else:
                progress_cb(f"正在群文件库检索 SS={ssno}")
                assert self.index is not None and self.baidu is not None
                item = self.index.pick_best(self.gid, ssno)
                if item is None:
                    raise PipelineError(f"群文件库中未找到 SS={ssno} 对应的文件")
                LOGGER.info("选中群文件: %s (size=%d msg_id=%s)", item.name, item.size, item.msg_id)
                progress_cb(f"转存并下载 {item.name}")
                downloaded = self.baidu.fetch_group_file(
                    item,
                    save_dir=self.config.baidu_save_dir,
                    target_dir=dl_dir,
                )
                source_name = item.name

            progress_cb("生成 PDF")
            pdf_path = self._to_pdf(downloaded, job_dir, ssno, book_title)

            progress_cb("上传到网盘并创建分享链接")
            result = drive_upload_file(
                pdf_path,
                book_title or pdf_path.stem,
                self.config.drive_target_dir,
                self.config.drive_expire_days,
            )
            share_url = result["share_url"]
            LOGGER.info("任务 %s 完成: %s", task_id, share_url)
            return {"share_url": share_url, "pdf": pdf_path, "source": source_name}
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)
            shutil.rmtree(dl_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    def _to_pdf(self, downloaded: Path, job_dir: Path, ssno: str, book_title: str) -> Path:
        suffix = downloaded.suffix.lower()
        target_name = preferred_pdf_name(book_title, ssno, downloaded.stem)
        target_pdf = job_dir / target_name

        if suffix == ".pdf":
            shutil.copy2(downloaded, target_pdf)
            return target_pdf

        if suffix not in ARCHIVE_SUFFIXES and not looks_like_archive(downloaded):
            raise PipelineError(f"暂不支持的文件类型: {downloaded.name}")

        extract_dir = job_dir / "extracted"
        extract_archive(
            downloaded,
            seven_zip=self.config.seven_zip_bin,
            password_dict=self.config.password_dict,
            target_dir=extract_dir,
            timeout=self.config.download_timeout_seconds,
        )

        # Some group uploads wrap a UVZ/ZIP inside RAR/7z. Expand a bounded
        # number of nested containers until PDF/PDG content becomes visible.
        nested_seen: set[Path] = set()
        nested_errors: list[str] = []
        for depth in range(3):
            if find_files_by_suffix(extract_dir, {".pdf", ".pdg"}):
                break
            nested = [
                path
                for path in extract_dir.rglob("*")
                if path.is_file() and path not in nested_seen and looks_like_archive(path)
            ]
            if not nested:
                break
            for index, nested_archive in enumerate(nested[:32], start=1):
                nested_seen.add(nested_archive)
                nested_target = extract_dir / "__nested__" / f"{depth + 1}_{index}_{nested_archive.stem[:60]}"
                try:
                    LOGGER.info("解压嵌套归档 depth=%d: %s", depth + 1, nested_archive.name)
                    extract_archive(
                        nested_archive,
                        seven_zip=self.config.seven_zip_bin,
                        password_dict=self.config.password_dict,
                        target_dir=nested_target,
                        timeout=self.config.download_timeout_seconds,
                    )
                except RuntimeError as exc:
                    nested_errors.append(f"{nested_archive.name}: {exc}")
                    LOGGER.warning("嵌套归档解压失败，继续检查其他文件: %s", exc)

        # 1) archive already contains a PDF -> use the largest one
        pdfs = sorted(
            find_files_by_suffix(extract_dir, {".pdf"}),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        if pdfs:
            shutil.copy2(pdfs[0], target_pdf)
            return target_pdf

        # 2) PDG folder -> convert
        pdg_count = len(find_files_by_suffix(extract_dir, {".pdg"}))
        if pdg_count == 0:
            detail = f"; 嵌套归档错误: {' | '.join(nested_errors[:3])}" if nested_errors else ""
            raise PipelineError(f"压缩包内未发现 PDF 或 PDG 文件: {downloaded.name}{detail}")

        LOGGER.info("开始 PDG 转换 (%d 页): %s", pdg_count, downloaded.name)
        converter = PdgConverter(str(extract_dir), output_path=str(target_pdf), dpi=float(self.config.pdg_dpi))
        converter.convert()
        if not target_pdf.exists() or target_pdf.stat().st_size == 0:
            raise PipelineError("PDG 转换后未生成有效 PDF")
        return target_pdf
