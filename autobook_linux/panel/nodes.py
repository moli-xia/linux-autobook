"""Fleet management: one panel talking to the panels of other nodes.

Nodes authenticate to each other with a per-panel bearer token rather than the
administrator password, and every call pins the peer's certificate by SHA-256
fingerprint, so self-signed certificates stay safe without shipping CA files
between machines.  A node is added by pasting a single "join code" that carries
the address, the fingerprint and the token together.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import socket
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.client import HTTPSConnection
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

JOIN_PREFIX = "AUTOBOOK1:"
REQUEST_TIMEOUT = 20
POLL_INTERVAL = 30
NODE_TOKEN_FILE = "node-token"
NODES_FILE = "nodes.json"

# Endpoints another panel may reach with a node token.  Deliberately excludes
# the account, session and join-code routes: a peer can observe and operate the
# services, never take the node over.
NODE_TOKEN_GET = {
    "/api/overview",
    "/api/activity",
    "/api/logs",
    "/api/config",
    "/api/passwords",
}
NODE_TOKEN_POST = {
    "/api/service",
    "/api/check",
    "/api/config",
    "/api/passwords",
    "/api/gateway-cert",
}


class NodeError(RuntimeError):
    """A remote node could not be reached or refused the request."""


# --------------------------------------------------------------- local token


class NodeTokenStore:
    """This panel's own token, handed to whoever manages it."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def get(self) -> str:
        try:
            token = self.path.read_text(encoding="utf-8").strip()
            if token:
                return token
        except OSError:
            pass
        return self.rotate()

    def rotate(self) -> str:
        token = secrets.token_urlsafe(36)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    output.write(token + "\n")
                os.replace(tmp, self.path)
                os.chmod(self.path, 0o600)
            finally:
                Path(tmp).unlink(missing_ok=True)
        return token

    def matches(self, supplied: str) -> bool:
        current = self.get()
        return bool(supplied) and secrets.compare_digest(supplied, current)


def certificate_fingerprint(cert_path: Path) -> str:
    """SHA-256 of the DER form of a PEM certificate, lowercase hex."""
    pem = Path(cert_path).read_text(encoding="utf-8")
    der = ssl.PEM_cert_to_DER_cert(pem)
    return hashlib.sha256(der).hexdigest()


def make_join_code(name: str, url: str, fingerprint: str, token: str) -> str:
    payload = {"name": name, "url": url.rstrip("/"), "fp": fingerprint, "token": token}
    blob = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return JOIN_PREFIX + blob.decode("ascii")


def parse_join_code(code: str) -> dict[str, str]:
    code = (code or "").strip()
    if not code.startswith(JOIN_PREFIX):
        raise ValueError("接入码格式不正确，请从目标节点面板重新复制")
    try:
        raw = base64.urlsafe_b64decode(code[len(JOIN_PREFIX):].encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("接入码已损坏，请重新复制完整内容") from exc
    for key in ("url", "fp", "token"):
        if not payload.get(key):
            raise ValueError("接入码缺少必要信息，请重新生成")
    if not str(payload["url"]).startswith("https://"):
        raise ValueError("接入码中的地址必须是 https://")
    return {
        "name": str(payload.get("name") or payload["url"]),
        "url": str(payload["url"]).rstrip("/"),
        "fingerprint": str(payload["fp"]).lower(),
        "token": str(payload["token"]),
    }


# ------------------------------------------------------------ remote client


class NodeClient:
    """Minimal HTTPS client that pins the peer certificate by fingerprint."""

    def __init__(self, url: str, fingerprint: str, token: str, timeout: int = REQUEST_TIMEOUT) -> None:
        self.url = url.rstrip("/")
        self.fingerprint = fingerprint.lower()
        self.token = token
        self.timeout = timeout
        host = self.url.split("://", 1)[1]
        if host.startswith("["):                     # bracketed IPv6 literal
            self.host, _, port = host.partition("]")
            self.host = self.host[1:]
            self.port = int(port.lstrip(":") or 443)
        else:
            self.host, _, port = host.partition(":")
            self.port = int(port or 443)

    def _connect(self) -> HTTPSConnection:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE     # replaced by the fingerprint pin below
        connection = HTTPSConnection(self.host, self.port, timeout=self.timeout, context=context)
        connection.connect()
        der = connection.sock.getpeercert(binary_form=True)
        actual = hashlib.sha256(der).hexdigest()
        if not secrets.compare_digest(actual, self.fingerprint):
            connection.close()
            raise NodeError(
                "节点证书与登记的指纹不一致，可能是节点重新生成了证书，"
                "或连接被中间人劫持。请在节点面板重新复制接入码。"
            )
        return connection

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = self._connect()
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read(4 * 1024 * 1024)
            status = response.status
        except (OSError, socket.timeout) as exc:
            raise NodeError(f"连接节点失败: {exc}") from exc
        finally:
            connection.close()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NodeError(f"节点返回了无法解析的内容（HTTP {status}）") from exc
        if status == 401:
            raise NodeError("节点拒绝了接入令牌，请在节点面板重新生成接入码")
        if status >= 400:
            raise NodeError(str(parsed.get("error") or f"节点返回 HTTP {status}"))
        return parsed


# --------------------------------------------------------------- registry


@dataclass
class Node:
    id: str
    name: str
    url: str
    fingerprint: str
    token: str
    added_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "url": self.url, "added_at": self.added_at}


class NodeRegistry:
    """Registered peers plus a cache of their most recent status."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._nodes: dict[str, Node] = {}
        self._status: dict[str, dict[str, Any]] = {}
        self.load()

    # ------------------------------------------------------------------
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = []
        with self._lock:
            self._nodes = {}
            for item in raw if isinstance(raw, list) else []:
                try:
                    node = Node(
                        id=str(item["id"]), name=str(item["name"]), url=str(item["url"]),
                        fingerprint=str(item["fingerprint"]), token=str(item["token"]),
                        added_at=float(item.get("added_at") or time.time()),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                self._nodes[node.id] = node

    def save(self) -> None:
        with self._lock:
            payload = [
                {"id": n.id, "name": n.name, "url": n.url, "fingerprint": n.fingerprint,
                 "token": n.token, "added_at": n.added_at}
                for n in self._nodes.values()
            ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        finally:
            Path(tmp).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    def list(self) -> list[Node]:
        with self._lock:
            return sorted(self._nodes.values(), key=lambda n: n.added_at)

    def get(self, node_id: str) -> Node:
        with self._lock:
            node = self._nodes.get(node_id)
        if not node:
            raise NodeError("节点不存在，可能已被移除")
        return node

    def add(self, code: str, name_override: str = "") -> Node:
        parsed = parse_join_code(code)
        with self._lock:
            for existing in self._nodes.values():
                if existing.url == parsed["url"]:
                    # Re-adding the same address refreshes its credentials.
                    existing.fingerprint = parsed["fingerprint"]
                    existing.token = parsed["token"]
                    existing.name = name_override or parsed["name"]
                    self.save()
                    return existing
            node = Node(
                id=uuid.uuid4().hex[:12],
                name=name_override or parsed["name"],
                url=parsed["url"],
                fingerprint=parsed["fingerprint"],
                token=parsed["token"],
            )
            self._nodes[node.id] = node
        self.save()
        return node

    def rename(self, node_id: str, name: str) -> None:
        name = name.strip()
        if not name:
            raise NodeError("节点名称不能为空")
        with self._lock:
            self.get(node_id).name = name[:60]
        self.save()

    def remove(self, node_id: str) -> None:
        with self._lock:
            self._nodes.pop(node_id, None)
            self._status.pop(node_id, None)
        self.save()

    def client(self, node_id: str) -> NodeClient:
        node = self.get(node_id)
        return NodeClient(node.url, node.fingerprint, node.token)

    # ------------------------------------------------------------------
    def poll(self, node_id: str) -> dict[str, Any]:
        """Fetch one node's overview and cache the result."""
        node = self.get(node_id)
        entry: dict[str, Any] = {"id": node.id, "name": node.name, "url": node.url, "checked_at": time.time()}
        try:
            overview = self.client(node_id).request("GET", "/api/overview")
        except NodeError as exc:
            entry.update({"online": False, "error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive
            entry.update({"online": False, "error": f"{type(exc).__name__}: {exc}"})
        else:
            issues = overview.get("issues", {})
            entry.update({
                "online": True,
                "error": "",
                "role": overview.get("role", ""),
                "version": overview.get("version", ""),
                "services": overview.get("services", []),
                "issues": issues,
                "issue_count": sum(len(items) for items in issues.values()),
                "system": overview.get("system", {}),
                "container": bool(overview.get("container")),
            })
        with self._lock:
            self._status[node_id] = entry
        return entry

    def poll_all(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        threads: list[threading.Thread] = []
        for node in self.list():
            thread = threading.Thread(target=self._poll_quietly, args=(node.id,), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join(timeout=REQUEST_TIMEOUT + 5)
        for node in self.list():
            results.append(self.status(node.id))
        return results

    def _poll_quietly(self, node_id: str) -> None:
        try:
            self.poll(node_id)
        except Exception:
            LOGGER.debug("轮询节点失败 id=%s", node_id, exc_info=True)

    def status(self, node_id: str) -> dict[str, Any]:
        with self._lock:
            cached = self._status.get(node_id)
            node = self._nodes.get(node_id)
        if not node:
            raise NodeError("节点不存在")
        if cached:
            # Name and address come from the registry, not the poll snapshot,
            # so a rename shows up immediately instead of after the next poll.
            entry = dict(cached)
            entry.update({"name": node.name, "url": node.url})
            return entry
        return {"id": node.id, "name": node.name, "url": node.url, "online": None,
                "error": "", "checked_at": 0.0}

    def all_status(self) -> list[dict[str, Any]]:
        return [self.status(node.id) for node in self.list()]


class NodePoller:
    """Background refresh so the fleet page is instant to open."""

    def __init__(self, registry: NodeRegistry, interval: int = POLL_INTERVAL) -> None:
        self.registry = registry
        self.interval = interval
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="node-poller", daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.wait(5):
            try:
                self.registry.poll_all()
            except Exception:
                LOGGER.debug("节点轮询循环异常", exc_info=True)
            if self._stop.wait(self.interval):
                return

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------- aggregate


def summarise(local: dict[str, Any], remote: list[dict[str, Any]]) -> dict[str, Any]:
    """Fleet-wide counters shown at the top of the page."""
    entries = [local] + [item for item in remote if item.get("online")]
    workers_running = 0
    workers_total = 0
    gateways_running = 0
    issues = 0
    offline = sum(1 for item in remote if item.get("online") is False)
    for entry in entries:
        for service in entry.get("services", []):
            if not service.get("installed"):
                continue
            if service["name"] == "worker":
                workers_total += 1
                workers_running += 1 if service.get("running") else 0
            elif service["name"] == "gateway" and service.get("running"):
                gateways_running += 1
        issues += entry.get("issue_count", 0)
    return {
        "nodes_total": len(remote) + 1,
        "nodes_offline": offline,
        "workers_running": workers_running,
        "workers_total": workers_total,
        "gateways_running": gateways_running,
        "issues": issues,
    }
