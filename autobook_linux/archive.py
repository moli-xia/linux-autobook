"""Archive extraction with a password dictionary (port of the Windows logic).

Uses the ``7z`` CLI (p7zip-full on Debian/Ubuntu). For each candidate password
it runs ``7z t`` until one matches, then extracts with ``7z x``.
"""
from __future__ import annotations

import logging
import locale
import subprocess
from pathlib import Path
from typing import Iterable

LOGGER = logging.getLogger(__name__)

ARCHIVE_SUFFIXES = frozenset(
    {
        ".zip",
        ".uvz",  # Chaoxing's UVZ files are ZIP containers with a custom suffix.
        ".rar",
        ".7z",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".tgz",
        ".tbz2",
        ".txz",
        ".cbz",
    }
)

_ARCHIVE_MAGICS = (
    b"PK\x03\x04",       # ZIP / UVZ / CBZ
    b"PK\x05\x06",       # empty ZIP
    b"PK\x07\x08",       # spanned ZIP
    b"Rar!\x1a\x07",     # RAR4 / RAR5
    b"7z\xbc\xaf\x27\x1c",
    b"\x1f\x8b",         # gzip
    b"BZh",               # bzip2
    b"\xfd7zXZ\x00",     # xz
)


def looks_like_archive(path: Path) -> bool:
    """Return true for a known archive suffix or a recognised archive header.

    Header sniffing lets the worker handle custom/missing extensions while the
    suffix list keeps encrypted archives discoverable even when their headers
    cannot be inspected without a password.
    """
    if path.suffix.lower() in ARCHIVE_SUFFIXES:
        return True
    if not path.is_file():
        return False
    try:
        with path.open("rb") as fh:
            header = fh.read(512)
            if any(header.startswith(magic) for magic in _ARCHIVE_MAGICS):
                return True
            if len(header) >= 262 and header[257:262] == b"ustar":
                return True
    except OSError:
        return False
    return False


def find_files_by_suffix(root: Path, suffixes: Iterable[str]) -> list[Path]:
    """Recursively find files using case-insensitive suffix matching."""
    wanted = {suffix.lower() for suffix in suffixes}
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in wanted
    ]


def read_password_dictionary(path: Path) -> list[str]:
    if not path.exists():
        return []
    text: str | None = None
    for encoding in dict.fromkeys(["utf-8-sig", "utf-8", locale.getpreferredencoding(False), "gb18030"]):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise RuntimeError(f"读取密码字典失败: {path}")
    seen: set[str] = set()
    passwords: list[str] = []
    for raw_line in text.splitlines():
        password = raw_line.strip()
        if password and password not in seen:
            seen.add(password)
            passwords.append(password)
    return passwords


def _compact_output(result: subprocess.CompletedProcess[str], limit: int = 400) -> str:
    text = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
    return text[:limit]


def extract_archive(
    archive: Path,
    seven_zip: str,
    password_dict: Path,
    target_dir: Path | None = None,
    timeout: int = 600,
) -> Path:
    """Extract archive into target_dir (default: archive's parent). Returns the dir."""
    if not archive.exists():
        raise RuntimeError(f"待解压文件不存在: {archive}")
    target_dir = target_dir or archive.parent
    target_dir.mkdir(parents=True, exist_ok=True)

    candidates = [""] + read_password_dictionary(password_dict)
    if not candidates:
        raise RuntimeError(f"密码字典为空或不存在: {password_dict}")

    LOGGER.info("尝试 %d 个密码解压: %s", len(candidates), archive.name)
    last_output = ""
    for index, password in enumerate(candidates, start=1):
        password_arg = f"-p{password}"
        test = subprocess.run(
            [seven_zip, "t", "-y", password_arg, str(archive)],
            capture_output=True, text=True, errors="replace", timeout=timeout,
        )
        if test.returncode != 0:
            last_output = _compact_output(test)
            if index == 1 or index % 25 == 0:
                LOGGER.info("已尝试 %d/%d 个密码", index, len(candidates))
            continue

        LOGGER.info("密码命中（第 %d 个候选），开始解压", index)
        result = subprocess.run(
            [seven_zip, "x", "-y", "-aoa", password_arg, f"-o{target_dir}", str(archive)],
            capture_output=True, text=True, errors="replace", timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"7z 解压失败 rc={result.returncode}: {_compact_output(result)}")
        return target_dir

    raise RuntimeError(f"密码字典未找到可用密码: {archive.name}; 最后输出: {last_output}")
