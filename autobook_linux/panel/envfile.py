"""Reading and atomically writing systemd EnvironmentFile-style config."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

HEADER = "# Managed by the linux-autobook admin panel. Mode 0600.\n"


def read_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=value`` lines, honouring single and double quoting."""
    values: dict[str, str] = {}
    try:
        raw_text = Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return values
    for raw in raw_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = value.replace('\\"', '"').replace("\\\\", "\\")
        values[key.strip()] = value
    return values


def quote_env(value: str) -> str:
    clean = str(value).replace("\r", " ").replace("\n", " ")
    return '"' + clean.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_env_file(path: Path, values: dict[str, str], order: list[str] | None = None) -> None:
    """Write ``values`` to ``path`` atomically with 0600 permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    preferred = list(order or [])
    ordered = list(
        dict.fromkeys(
            [key for key in preferred if key in values]
            + sorted(key for key in values if key not in preferred)
        )
    )
    body = HEADER + "".join(f"{key}={quote_env(values.get(key, ''))}\n" for key in ordered)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        Path(tmp).unlink(missing_ok=True)
