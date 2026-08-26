"""Site-wide task management for the panel.

The panel's existing task view reads this machine's own log.  This one asks the
site instead, so one screen shows every task in the fleet and which worker is
holding each of them — and lets an operator delete tasks that are stuck or no
longer wanted.

Deleting is offered only on a node that runs the gateway: that is the machine
an operator administers the fleet from, and the caller checks the role before
anything is removed.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from autobook_linux.panel.envfile import read_env_file
from autobook_linux.panel.settings import PanelSettings

LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30
DEFAULT_LIMIT = 60
MAX_DELETE = 500

# The site's own numbering; kept here so the panel can label a task without a
# round trip when the site does not send a label.
STATUS_LABELS = {
    1: "排队中",
    2: "PDF处理中",
    3: "已完成",
    4: "失败",
    5: "已取消",
    6: "等待格式转换",
    7: "格式转换中",
}
ACTIVE_STATUSES = (1, 2, 6, 7)
FINISHED_STATUSES = (3, 4, 5)


class TaskAdminError(RuntimeError):
    pass


def site_credentials(settings: PanelSettings) -> tuple[str, str]:
    """The site address and worker token, from whichever env file has them."""
    for path in (settings.worker_env, settings.gateway_env):
        values = read_env_file(path)
        base = (values.get("SITE_BASE_URL") or "").strip().rstrip("/")
        token = (values.get("WORKER_TOKEN") or "").strip()
        if base and token:
            return base, token
    raise TaskAdminError("尚未配置任务网站地址或 Worker 令牌，请先在「配置」页填写")


def _call(settings: PanelSettings, action: str, body: dict[str, Any]) -> dict[str, Any]:
    base, token = site_credentials(settings)
    payload = dict(body)
    payload["worker_token"] = token
    try:
        response = requests.post(
            f"{base}/index.php?doc_delivery-{action}-ajax-1",
            data=payload, timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise TaskAdminError(f"连接任务网站失败: {exc}") from exc
    except ValueError as exc:
        raise TaskAdminError("任务网站返回了无效 JSON（可能是站点插件版本过旧）") from exc
    if not data.get("kong_status"):
        raise TaskAdminError(str(data.get("message") or "任务网站拒绝了请求"))
    return data


def describe(task: dict[str, Any], now: int) -> dict[str, Any]:
    """Add the fields the front-end shows: who runs it, and for how long."""
    status = int(task.get("status") or 0)
    started = int(task.get("started_at") or 0)
    finished = int(task.get("finished_at") or 0)
    heartbeat = int(task.get("heartbeat_at") or 0)
    running = status in (2, 7)
    if running and started:
        elapsed = max(0, now - started)
    elif finished and started:
        elapsed = max(0, finished - started)
    else:
        elapsed = 0
    return {
        "id": int(task.get("id") or 0),
        "title": str(task.get("book_title") or task.get("keyword") or "").strip(),
        "ssno": str(task.get("ssno") or ""),
        "status": status,
        "status_label": str(task.get("status_label") or STATUS_LABELS.get(status, "未知")),
        "worker_id": str(task.get("worker_id") or ""),
        "message": str(task.get("message") or ""),
        "result_url": str(task.get("result_url") or ""),
        "retry_count": int(task.get("retry_count") or 0),
        "elapsed": elapsed,
        # A running task whose worker stopped reporting is the one an operator
        # is usually looking for.
        "stale": running and bool(heartbeat) and (now - heartbeat) > 300,
        "created": int(task.get("dateline") or 0),
    }


def list_tasks(settings: PanelSettings, status: str = "", limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    body: dict[str, Any] = {"limit": max(1, min(200, int(limit)))}
    if status:
        body["status"] = status
    data = _call(settings, "tasklist", body)
    now = int(data.get("now") or time.time())
    tasks = [describe(row, now) for row in data.get("tasks") or []]
    counts = {int(key): int(value) for key, value in (data.get("counts") or {}).items()}
    return {
        "tasks": tasks,
        "counts": counts,
        "running": sum(counts.get(one, 0) for one in (2, 7)),
        "queued": sum(counts.get(one, 0) for one in (1, 6)),
        "workers": sorted({row["worker_id"] for row in tasks if row["worker_id"]}),
    }


def delete_tasks(settings: PanelSettings, ids: list[int]) -> int:
    wanted = [int(one) for one in ids if int(one) > 0]
    if not wanted:
        raise TaskAdminError("没有选中任何任务")
    if len(wanted) > MAX_DELETE:
        raise TaskAdminError(f"一次最多删除 {MAX_DELETE} 个任务")
    data = _call(settings, "taskadmin", {"op": "delete", "ids": ",".join(str(one) for one in wanted)})
    LOGGER.info("面板删除任务: %s", wanted[:20])
    return int(data.get("removed") or 0)


def clear_tasks(settings: PanelSettings, statuses: list[int]) -> int:
    wanted = [int(one) for one in statuses if 1 <= int(one) <= 7]
    if not wanted:
        raise TaskAdminError("没有指定要清空的状态")
    data = _call(settings, "taskadmin", {"op": "clear", "status": ",".join(str(one) for one in wanted)})
    LOGGER.info("面板清空任务: 状态 %s", wanted)
    return int(data.get("removed") or 0)
