"""Runtime settings for the administration panel."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from autobook_linux.panel.envfile import read_env_file

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VALID_ROLES = {"all", "gateway", "worker"}


@dataclass
class PanelSettings:
    bind: str
    port: int
    tls_cert: Path
    tls_key: Path
    state_file: Path
    config_dir: Path
    install_dir: Path
    gateway_env: Path
    worker_env: Path
    install_env: Path
    session_seconds: int
    public_host: str
    role: str = "all"

    @classmethod
    def load(cls) -> "PanelSettings":
        config_dir = Path(os.environ.get("ADMIN_CONFIG_DIR", "/etc/linux-autobook"))
        install_dir = Path(os.environ.get("ADMIN_INSTALL_DIR", str(PROJECT_ROOT)))
        install_env = config_dir / "install.env"
        state = read_env_file(install_env)
        role = (os.environ.get("ADMIN_ROLE") or state.get("INSTALL_ROLE") or "all").strip().lower()
        if role not in VALID_ROLES:
            role = "all"
        public_host = (os.environ.get("ADMIN_PUBLIC_HOST") or state.get("PUBLIC_HOST") or "").strip()
        return cls(
            bind=os.environ.get("ADMIN_BIND", "0.0.0.0"),
            port=int(os.environ.get("ADMIN_PORT", "8766")),
            tls_cert=Path(os.environ.get("ADMIN_TLS_CERT", str(config_dir / "admin.crt"))),
            tls_key=Path(os.environ.get("ADMIN_TLS_KEY", str(config_dir / "admin.key"))),
            state_file=Path(os.environ.get("ADMIN_STATE_FILE", str(config_dir / "admin-state.json"))),
            config_dir=config_dir,
            install_dir=install_dir,
            gateway_env=Path(os.environ.get("ADMIN_GATEWAY_ENV", str(config_dir / "gateway.env"))),
            worker_env=Path(os.environ.get("ADMIN_WORKER_ENV", str(config_dir / "worker.env"))),
            install_env=install_env,
            session_seconds=max(300, int(os.environ.get("ADMIN_SESSION_SECONDS", "28800"))),
            public_host=public_host,
            role=role,
        )

    # ------------------------------------------------------------------
    def current_role(self) -> str:
        """Re-read the role from install.env so role changes apply live."""
        state = read_env_file(self.install_env)
        role = (state.get("INSTALL_ROLE") or self.role).strip().lower()
        return role if role in VALID_ROLES else self.role

    def roles(self) -> list[str]:
        role = self.current_role()
        return ["gateway", "worker"] if role == "all" else [role]

    def has_role(self, role: str) -> bool:
        return role in self.roles()

    def env_path(self, target: str) -> Path:
        return self.gateway_env if target == "gateway" else self.worker_env

    def venv_python(self) -> Path:
        return self.install_dir / ".venv" / "bin" / "python"
