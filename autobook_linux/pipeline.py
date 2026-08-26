"""Per-task pipeline: SS code -> group library -> download -> extract/convert -> upload.

Replaces the whole Windows GUI chain (BaiduNetdisk client + 7-Zip GUI +
Pdg2Pic GUI) with pure API/CLI steps, so any number of tasks can run
concurrently on a headless Linux server.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pikepdf

from autobook_linux.archive import (
    ARCHIVE_SUFFIXES,
    extract_archive,
    find_files_by_suffix,
    looks_like_archive,
)
from autobook_linux.baidu_pan import BaiduPanClient
from autobook_linux.config import Config
from autobook_linux.gateway_client import BaiduGatewayClient
from autobook_linux.library_index import LibraryIndex, pick_best_file
from autobook_linux.lookup import Lookup, LookupError
from autobook_linux.lookup import plan_from_task as lookup_plan
from autobook_linux.lookup import queries_for
from autobook_linux.pdg_crypto import pdg2pic_direct_type

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


# Formats a reader can already open. The netdisk holds thousands of these,
# mostly EPUB, and refusing them would make those books undeliverable.
EBOOK_SUFFIXES = {".epub", ".mobi", ".azw3"}
# calibre's converter, when the image has it; see _from_ebook.
EBOOK_CONVERTERS = ("ebook-convert",)
EBOOK_CONVERT_TIMEOUT = 1800
# Each conversion starts a headless Chromium worth several hundred megabytes,
# so running one per task thread will exhaust a small worker. Conversions take
# seconds, so serialising them costs little.
EBOOK_CONVERT_SLOTS = max(1, int(os.environ.get("EBOOK_CONVERT_SLOTS", "1")))
_EBOOK_CONVERT_LOCK = threading.Semaphore(EBOOK_CONVERT_SLOTS)


def find_ebook_converter() -> str | None:
    for name in EBOOK_CONVERTERS:
        path = shutil.which(name)
        if path:
            return path
    return None


def _slug(value: str) -> str:
    """Short, filesystem-safe stand-in for an SS number in directory names."""
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", value or "").strip("-")
    return (cleaned[:40] or "book")


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
    def _search_locally(self, plan, progress_cb):
        """The gateway-free equivalent of GatewayManager._resolve."""
        assert self.index is not None
        for lookup in plan:
            for query in queries_for(lookup):
                try:
                    candidates = self.index.search(self.gid, query)
                except Exception as exc:  # noqa: BLE001 - try the next form
                    LOGGER.warning("检索 %r 失败: %s", query, exc)
                    continue
                item = pick_best_file(candidates, lookup.value, lookup.kind)
                if item is not None:
                    return item
        return None

    def process(self, task: dict, progress_cb=lambda msg: None) -> dict:
        """Run one task end to end. Returns {"share_url":..., "pdf": Path}."""
        task_id = task.get("id")
        book_title = str(task.get("book_title") or task.get("keyword") or "")
        try:
            plan = lookup_plan(task)
        except LookupError as exc:
            raise PipelineError(str(exc)) from exc
        lookup = plan[0]
        # Books imported from the netdisk catalogue often have no SS number;
        # the slug only names the working directory.
        ssno = lookup.value if lookup.kind == "ss" else ""
        slug = lookup.value if lookup.kind == "ss" else _slug(lookup.value)

        job_dir = self.config.work_root / f"task_{task_id}_{slug}"
        dl_dir = self.config.download_root / f"task_{task_id}_{slug}"
        job_dir.mkdir(parents=True, exist_ok=True)
        dl_dir.mkdir(parents=True, exist_ok=True)
        try:
            if self.gateway is not None:
                progress_cb(f"正在通过下载网关检索并下载 {lookup.label()}")
                request_id = f"task-{task_id}-{task.get('lease_id') or task.get('token') or slug}"
                downloaded, source_name = self.gateway.fetch(
                    lookup.value, request_id, dl_dir, lookup.kind,
                    plan=[entry.as_payload() for entry in plan[1:]])
                LOGGER.info("网关下载完成: %s", source_name)
            else:
                progress_cb(f"正在进行非标准文件检索 {lookup.label()}")
                assert self.index is not None and self.baidu is not None
                item = self._search_locally(plan, progress_cb)
                if item is None:
                    tried = "、".join(entry.label() for entry in plan)
                    raise PipelineError(f"非标准文件检索未找到对应的文件（已尝试 {tried}）")
                LOGGER.info("选中群文件: %s (size=%d msg_id=%s)", item.name, item.size, item.msg_id)
                progress_cb(f"转存并下载 {item.name}")
                downloaded = self.baidu.fetch_group_file(
                    item,
                    save_dir=self.config.baidu_save_dir,
                    target_dir=dl_dir,
                )
                source_name = item.name

            progress_cb("生成 PDF")
            pdf_path = self._to_pdf(downloaded, job_dir, slug, book_title)

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
    def _from_ebook(self, downloaded: Path, job_dir: Path, target_pdf: Path) -> Path:
        """Convert EPUB/MOBI/AZW3 to PDF, or deliver the original if we cannot.

        Conversion needs calibre's ebook-convert (in the image) plus CJK fonts,
        without which Chinese renders as empty boxes.  When the converter is
        missing or fails, delivering the original file is a far better outcome
        than failing the task: the reader still gets the book.
        """
        converter = find_ebook_converter()
        if converter:
            LOGGER.info("使用 %s 转换 %s", Path(converter).name, downloaded.name)
            # calibre renders PDF through a headless Chromium, which refuses to
            # start as root without --no-sandbox, and it needs a writable HOME
            # for its config directory.
            calibre_home = job_dir / ".calibre"
            calibre_home.mkdir(parents=True, exist_ok=True)
            environment = dict(os.environ)
            environment.update({
                "HOME": str(calibre_home),
                "CALIBRE_CONFIG_DIRECTORY": str(calibre_home / "config"),
                "QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox --disable-gpu --disable-dev-shm-usage",
                "QT_QPA_PLATFORM": "offscreen",
            })
            with _EBOOK_CONVERT_LOCK:
                result = subprocess.run(
                    [converter, str(downloaded), str(target_pdf)],
                    capture_output=True, text=True, errors="replace",
                    timeout=EBOOK_CONVERT_TIMEOUT, env=environment,
                )
            if result.returncode == 0 and target_pdf.exists() and target_pdf.stat().st_size > 0:
                return target_pdf
            LOGGER.warning(
                "电子书转换失败（退出码 %s），改为直接交付原文件: %s",
                result.returncode, (result.stderr or result.stdout or "")[-300:],
            )
            target_pdf.unlink(missing_ok=True)
        else:
            LOGGER.info("未安装 ebook-convert，直接交付原始 %s 文件", downloaded.suffix)

        delivered = job_dir / sanitize_filename(downloaded.name)
        shutil.copy2(downloaded, delivered)
        return delivered

    def _convert_pdg_with_wine(self, source_dir: Path, target_pdf: Path, expected_pages: int) -> int:
        """Run the gateway's ephemeral Pdg2Pic container and validate its PDF."""
        if self.gateway is None:
            raise PipelineError("当前 Worker 未配置中心网关，无法调用 Pdg2Pic Wine 兜底")
        self.gateway.convert_pdg_fallback(source_dir, target_pdf)
        if not target_pdf.is_file() or target_pdf.stat().st_size <= 8:
            raise PipelineError("Pdg2Pic 网关兜底未生成有效 PDF")
        with pikepdf.open(target_pdf) as document:
            pages = len(document.pages)
        if pages <= 0:
            raise PipelineError("Pdg2Pic 网关兜底生成的 PDF 没有页面")
        if pages != expected_pages:
            raise PipelineError(f"Pdg2Pic 网关兜底页数不完整: 期望 {expected_pages}，实际 {pages}")
        return pages

    def _to_pdf(self, downloaded: Path, job_dir: Path, ssno: str, book_title: str) -> Path:
        suffix = downloaded.suffix.lower()
        target_name = preferred_pdf_name(book_title, ssno, downloaded.stem)
        target_pdf = job_dir / target_name

        if suffix == ".pdf":
            shutil.copy2(downloaded, target_pdf)
            return target_pdf

        if suffix in EBOOK_SUFFIXES:
            return self._from_ebook(downloaded, job_dir, target_pdf)

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
        pdg_files = find_files_by_suffix(extract_dir, {".pdg"})
        pdg_count = len(pdg_files)
        if not pdg_files:
            detail = f"; 嵌套归档错误: {' | '.join(nested_errors[:3])}" if nested_errors else ""
            raise PipelineError(f"压缩包内未发现 PDF 或 PDG 文件: {downloaded.name}{detail}")

        LOGGER.info("开始 PDG 转换 (%d 页): %s", pdg_count, downloaded.name)
        direct_types: set[int] = set()
        for page in pdg_files:
            with page.open("rb") as source:
                marker = pdg2pic_direct_type(source.read(16))
            if marker is not None:
                direct_types.add(marker)
        common_parent = Path(os.path.commonpath([str(page.resolve().parent) for page in pdg_files]))
        if direct_types:
            markers = "/".join(f"{value:02X}H" for value in sorted(direct_types))
            LOGGER.warning("检测到已知专有 PDG 类型 %s，直接转交网关按需 Wine 兜底", markers)
            try:
                pages = self._convert_pdg_with_wine(common_parent, target_pdf, pdg_count)
            except Exception as exc:
                target_pdf.unlink(missing_ok=True)
                raise PipelineError(f"专有 PDG {markers} 的 Pdg2Pic 网关兜底失败: {exc}") from exc
            LOGGER.info("Pdg2Pic 网关兜底完成 (%d 页): %s", pages, target_pdf.name)
            return target_pdf

        try:
            converter = PdgConverter(str(extract_dir), output_path=str(target_pdf), dpi=float(self.config.pdg_dpi))
            converter.convert()
            if not target_pdf.is_file() or target_pdf.stat().st_size <= 8:
                raise PipelineError("开放 PDG 转换器未生成有效 PDF")
            with pikepdf.open(target_pdf) as document:
                pages = len(document.pages)
            if pages != pdg_count:
                raise PipelineError(f"开放 PDG 转换器页数不完整: 期望 {pdg_count}，实际 {pages}")
            return target_pdf
        except Exception as primary_exc:
            target_pdf.unlink(missing_ok=True)
            LOGGER.warning("开放 PDG 转换失败，转交网关按需 Wine 兜底: %s", primary_exc)
            try:
                pages = self._convert_pdg_with_wine(common_parent, target_pdf, pdg_count)
            except Exception as fallback_exc:
                target_pdf.unlink(missing_ok=True)
                raise PipelineError(
                    f"PDG 开放转换失败: {primary_exc}; Pdg2Pic Wine 兜底也失败: {fallback_exc}"
                ) from fallback_exc
            LOGGER.info("Pdg2Pic 网关兜底完成 (%d 页): %s", pages, target_pdf.name)
            return target_pdf
