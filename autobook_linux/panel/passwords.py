"""The archive password dictionary, editable from the panel.

The project ships a curated default list.  A fresh install starts from it, and
the panel can add, edit, delete or restore entries without touching the shell.
"""
from __future__ import annotations

import locale
import os
import secrets
import shutil
from pathlib import Path

from autobook_linux.panel.envfile import read_env_file
from autobook_linux.panel.settings import PanelSettings, service_user

DEFAULTS_FILE = Path(__file__).resolve().parent / "data" / "default_passwords.txt"
MAX_ENTRIES = 20000
MAX_LENGTH = 200


def _decode(path: Path) -> str:
    """Read a dictionary written by any of the encodings we have seen."""
    encodings = dict.fromkeys(["utf-8-sig", "utf-8", locale.getpreferredencoding(False), "gb18030"])
    for encoding in encodings:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
        except OSError:
            return ""
    return ""


def _parse(text: str) -> list[str]:
    seen: set[str] = set()
    entries: list[str] = []
    for line in text.splitlines():
        value = line.strip()
        if value and not value.startswith("#") and value not in seen:
            seen.add(value)
            entries.append(value)
    return entries


def default_entries() -> list[str]:
    return _parse(_decode(DEFAULTS_FILE))


def dictionary_path(settings: PanelSettings) -> Path:
    values = read_env_file(settings.worker_env)
    raw = values.get("PASSWORD_DICT") or str(settings.install_dir / "password.txt")
    resolved = Path(raw).resolve()
    allowed = settings.install_dir.resolve()
    if not str(resolved).startswith(str(allowed)):
        raise ValueError("密码字典必须位于程序安装目录内")
    return resolved


def load(settings: PanelSettings) -> list[str]:
    return _parse(_decode(dictionary_path(settings)))


def save(settings: PanelSettings, entries: list[str]) -> int:
    """Atomically replace the dictionary, keeping order and dropping duplicates."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in entries:
        value = str(raw).strip()
        if not value or value in seen:
            continue
        if len(value) > MAX_LENGTH:
            raise ValueError(f"单个密码不能超过 {MAX_LENGTH} 个字符")
        if any(char in value for char in "\r\n\x00"):
            raise ValueError("密码不能包含换行或空字符")
        seen.add(value)
        cleaned.append(value)
    if len(cleaned) > MAX_ENTRIES:
        raise ValueError(f"密码字典最多 {MAX_ENTRIES} 条")
    path = dictionary_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write("\n".join(cleaned) + ("\n" if cleaned else ""))
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        owner = service_user()
        if owner and os.name != "nt":
            try:
                shutil.chown(path, user=owner, group=owner)
            except (LookupError, PermissionError, OSError):
                pass
    finally:
        Path(tmp).unlink(missing_ok=True)
    return len(cleaned)


def ensure_seeded(settings: PanelSettings) -> bool:
    """Create the dictionary from the built-in defaults when it is empty."""
    try:
        path = dictionary_path(settings)
    except ValueError:
        return False
    if path.is_file() and _parse(_decode(path)):
        return False
    save(settings, default_entries())
    return True


# ----------------------------------------------------------------- mutations


def add(settings: PanelSettings, value: str) -> tuple[int, str]:
    entries = load(settings)
    value = value.strip()
    if not value:
        raise ValueError("密码不能为空")
    if value in entries:
        raise ValueError("该密码已存在")
    entries.append(value)
    return save(settings, entries), f"已添加密码，共 {len(entries)} 条"


def update(settings: PanelSettings, old: str, new: str) -> tuple[int, str]:
    entries = load(settings)
    new = new.strip()
    if not new:
        raise ValueError("密码不能为空")
    if old not in entries:
        raise ValueError("要修改的密码已不存在，请刷新后重试")
    if new != old and new in entries:
        raise ValueError("修改后的密码与已有条目重复")
    entries[entries.index(old)] = new
    return save(settings, entries), "已保存修改"


def remove(settings: PanelSettings, value: str) -> tuple[int, str]:
    entries = load(settings)
    if value not in entries:
        raise ValueError("该密码已不存在")
    entries.remove(value)
    return save(settings, entries), f"已删除，剩余 {len(entries)} 条"


def restore_defaults(settings: PanelSettings) -> tuple[int, str]:
    count = save(settings, default_entries())
    return count, f"已恢复为内置字典，共 {count} 条"


def merge_defaults(settings: PanelSettings) -> tuple[int, str]:
    entries = load(settings)
    existing = set(entries)
    added = [value for value in default_entries() if value not in existing]
    count = save(settings, entries + added)
    if not added:
        return count, "内置字典中的密码都已存在，无需补充"
    return count, f"已补充 {len(added)} 条内置密码，共 {count} 条"


def snapshot(settings: PanelSettings) -> dict[str, object]:
    entries = load(settings)
    defaults = default_entries()
    missing = [value for value in defaults if value not in set(entries)]
    try:
        path = str(dictionary_path(settings))
    except ValueError:
        path = ""
    return {
        "path": path,
        "count": len(entries),
        "entries": entries,
        "content": "\n".join(entries),
        "defaults_count": len(defaults),
        "missing_defaults": len(missing),
        "custom_count": len([value for value in entries if value not in set(defaults)]),
    }
