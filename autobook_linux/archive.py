"""Archive extraction with a password dictionary (port of the Windows logic).

Most archives go through the ``7z`` CLI (p7zip-full on Debian/Ubuntu): for each
candidate password it runs ``7z t`` until one matches, then extracts with
``7z x``.

RAR is handled by RARLAB ``unrar`` instead, because the p7zip build ships an
incomplete RAR decoder that fails on modern RAR5 compression with "Unsupported
Method" - the password is correct but the data cannot be decompressed, which
the old loop misreported as "no working password". ``unrar`` decompresses RAR5
and, crucially, can verify a password against a single member in one PBKDF2
pass (~50 ms), so a few hundred candidates cost seconds rather than the minutes
a whole-archive test per guess would take.
"""
from __future__ import annotations

import logging
import locale
import re
import shutil
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


RAR_MAGIC = b"Rar!"
# unrar's exit code for a bad password (RARLAB reserves 11 for RAR_BAD_PASSWORD).
UNRAR_BAD_PASSWORD = 11


def is_rar(archive: Path) -> bool:
    """True when the file is a RAR (either RAR4 or RAR5) by header."""
    try:
        with archive.open("rb") as fh:
            return fh.read(7).startswith(RAR_MAGIC)
    except OSError:
        return False


def find_unrar() -> str | None:
    return shutil.which("unrar")


def _unrar_test_member(archive: Path) -> tuple[str | None, bool]:
    """Pick one encrypted member to test passwords against.

    RAR5 salts every file separately, so testing one member costs a single
    PBKDF2 pass instead of one per file.  Returns the largest encrypted regular
    file and whether the archive has any encrypted member at all; an archive
    with none needs no password.
    """
    unrar = find_unrar()
    if not unrar:
        return None, False
    try:
        listing = subprocess.run(
            [unrar, "lt", "-p-", str(archive)],
            capture_output=True, text=True, errors="replace", timeout=120,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None, False
    name: str | None = None
    size = 0
    best_name: str | None = None
    best_size = -1
    is_file = False
    encrypted = False
    any_encrypted = False
    for raw in listing.splitlines():
        line = raw.strip()
        if line.startswith("Name:"):
            # Flush the previous block before starting a new one.
            if name and is_file and encrypted and size > best_size:
                best_name, best_size = name, size
            name = line[5:].strip()
            size = 0
            is_file = False
            encrypted = False
        elif line.startswith("Size:"):
            try:
                size = int(line[5:].strip())
            except ValueError:
                size = 0
        elif line.startswith("Type:"):
            is_file = line[5:].strip().lower() == "file"
        elif line.startswith("Flags:"):
            if "encrypted" in line.lower():
                encrypted = True
                any_encrypted = True
    if name and is_file and encrypted and size > best_size:
        best_name, best_size = name, size
    return best_name, any_encrypted


def _extract_rar(
    archive: Path, unrar: str, candidates: list[str], target_dir: Path, timeout: int
) -> Path:
    """Extract a RAR, finding the password by testing one member per guess."""
    member, encrypted = _unrar_test_member(archive)

    def do_extract(password: str) -> None:
        args = [unrar, "x", "-y", "-inul", "-o+"]
        args.append("-p-" if password == "" else f"-p{password}")
        args += [str(archive), f"{target_dir}/"]
        result = subprocess.run(
            args, capture_output=True, text=True, errors="replace", timeout=timeout
        )
        if result.returncode != 0:
            raise RuntimeError(f"unrar 解压失败 rc={result.returncode}: {_compact_output(result)}")

    if not encrypted:
        LOGGER.info("RAR 无加密，直接解压: %s", archive.name)
        do_extract("")
        return target_dir

    if member is None:
        # Encrypted but we could not identify a member (unusual); fall back to
        # testing the whole archive, which is slower but still correct.
        member_args: list[str] = []
    else:
        member_args = [member]

    LOGGER.info("尝试 %d 个密码解压 RAR: %s", len(candidates), archive.name)
    last_output = ""
    for index, password in enumerate(candidates, start=1):
        if password == "":
            continue  # an encrypted archive cannot open with an empty password
        test = subprocess.run(
            [unrar, "t", "-inul", f"-p{password}", str(archive), *member_args],
            capture_output=True, text=True, errors="replace", timeout=timeout,
        )
        if test.returncode == 0:
            LOGGER.info("RAR 密码命中（第 %d 个候选），开始解压", index)
            do_extract(password)
            return target_dir
        if test.returncode != UNRAR_BAD_PASSWORD:
            last_output = _compact_output(test)
        if index == 1 or index % 50 == 0:
            LOGGER.info("已尝试 %d/%d 个密码", index, len(candidates))
    raise RuntimeError(
        f"密码字典未找到可用密码: {archive.name}"
        + (f"; 最后输出: {last_output}" if last_output else "")
    )


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

    # RAR needs unrar: the bundled p7zip cannot decompress RAR5 and would report
    # every password as wrong.  Fall back to 7z only when unrar is absent (it
    # still handles the older RAR4 that some uploads use).
    unrar = find_unrar()
    if is_rar(archive):
        if unrar:
            return _extract_rar(archive, unrar, candidates, target_dir, timeout)
        LOGGER.warning("未安装 unrar，RAR 将回退到 7z（无法解压 RAR5）: %s", archive.name)

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
            if "Unsupported Method" in last_output:
                # The password may be right; 7z simply cannot decode this codec.
                raise RuntimeError(
                    f"7z 不支持该压缩格式（非密码问题）: {archive.name}; "
                    f"请安装 unrar 后重试; 输出: {last_output}"
                )
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
