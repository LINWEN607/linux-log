"""
配置页事件目录：分类、默认勾选、UI 展示构建。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from valid_event_ids import MONITOR_EVENT_IDS

# 事件分类（顺序即展示顺序）
EVENT_CATEGORIES = [
    ("login", "登录与认证", [
        "LoginSucc", "LoginFail", "Logout",
    ]),
    ("ssh", "SSH", [
        "SSH_INVALID_USER", "SSH_AUTH_FAILED", "SSH_LOGIN_SUCCESS", "SSH_DISCONNECTED",
    ]),
    ("disk", "磁盘与存储", [
        "FoundDisk", "InsertDisk", "EjectDisk",
        "StorageBroken", "STORAGE_DEGRADED",
        "DiskWakeup", "DiskSpindown", "DISK_IO_ERR",
    ]),
    ("systemd", "Systemd 服务", [
        "SYSTEMD_SERVICE_STARTED", "SYSTEMD_SERVICE_STOPPED",
        "SYSTEMD_SERVICE_RESTARTED", "SYSTEMD_SERVICE_FAILED",
    ]),
    ("docker", "Docker 容器", [
        "DOCKER_CONTAINER_CREATE",
        "DOCKER_CONTAINER_START",
        "DOCKER_CONTAINER_STOP",
        "DOCKER_CONTAINER_DIE",
        "DOCKER_CONTAINER_OOM",
        "DOCKER_CONTAINER_KILL",
        "DOCKER_CONTAINER_PAUSE",
        "DOCKER_CONTAINER_UNPAUSE",
        "DOCKER_CONTAINER_RESTART",
        "DOCKER_CONTAINER_DESTROY",
    ]),
]

# 不在 UI 中提供选择（内部使用的系统事件）
EVENT_IDS_HIDDEN_IN_UI = {"APP_START", "APP_STOP", "SYSLOG_PATTERN_MATCH",
    "PATROL_CPU_ALARM", "PATROL_CPU_RESTORED",
    "PATROL_MEM_ALARM", "PATROL_MEM_RESTORED",
    "PATROL_DISK_ALARM", "PATROL_DISK_RESTORED"}

# 后端认可的静态事件 ID
VALID_EVENT_IDS = MONITOR_EVENT_IDS

# 默认勾选的事件
DEFAULT_SELECTED_EVENTS = [
    "LoginSucc",
    "LoginFail",
    "Logout",
    "SSH_INVALID_USER",
    "SSH_AUTH_FAILED",
    "SSH_LOGIN_SUCCESS",
    "SSH_DISCONNECTED",
    "FoundDisk",
    "InsertDisk",
    "EjectDisk",
    "StorageBroken",
    "STORAGE_DEGRADED",
    "DiskWakeup",
    "DiskSpindown",
    "DISK_IO_ERR",
    "SYSTEMD_SERVICE_STARTED",
    "SYSTEMD_SERVICE_STOPPED",
    "SYSTEMD_SERVICE_FAILED",
]


def build_events_for_ui(
    titles: Dict[str, str] = None,
    notes: Dict[str, str] = None,
) -> Tuple[List[Dict[str, Any]], set]:
    """构建配置页事件分类与可选事件集合。"""
    if titles is None:
        titles = {}
    if notes is None:
        notes = {}

    valid_event_ids = set(VALID_EVENT_IDS)
    events_by_category: List[Dict[str, Any]] = []

    for cat_id, cat_name, event_ids in EVENT_CATEGORIES:
        events = []
        for key in event_ids:
            if key in EVENT_IDS_HIDDEN_IN_UI:
                continue
            raw_title = titles.get(key)
            if raw_title:
                display_title = re.sub(r"\s+", " ", raw_title).strip()
            else:
                display_title = key
            base_note = notes.get(key, "")
            events.append({
                "id": key,
                "title": display_title,
                "note": base_note,
            })
        if events:
            events_by_category.append({"id": cat_id, "name": cat_name, "events": events})
    return events_by_category, valid_event_ids
