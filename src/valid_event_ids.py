"""
用户可配置的监控事件 ID 白名单。
"""

from __future__ import annotations

from typing import Any, Iterable, List

# 白名单：通用 Linux 系统事件
MONITOR_EVENT_IDS = frozenset({
    # 登录与认证
    "LoginSucc",
    "LoginFail",
    "Logout",
    # SSH
    "SSH_INVALID_USER",
    "SSH_AUTH_FAILED",
    "SSH_LOGIN_SUCCESS",
    "SSH_DISCONNECTED",
    # 磁盘与存储
    "FoundDisk",
    "InsertDisk",
    "EjectDisk",
    "StorageBroken",
    "STORAGE_DEGRADED",
    "DiskWakeup",
    "DiskSpindown",
    "DISK_IO_ERR",
    # Systemd 服务
    "SYSTEMD_SERVICE_STARTED",
    "SYSTEMD_SERVICE_STOPPED",
    "SYSTEMD_SERVICE_RESTARTED",
    "SYSTEMD_SERVICE_FAILED",
    # Docker 容器
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
    # 自定义规则
    "SYSLOG_PATTERN_MATCH",
    # 系统资源（巡检告警）
    "PATROL_CPU_ALARM",
    "PATROL_CPU_RESTORED",
    "PATROL_MEM_ALARM",
    "PATROL_MEM_RESTORED",
    "PATROL_DISK_ALARM",
    "PATROL_DISK_RESTORED",
})


def is_allowed_monitor_event(event_id: Any) -> bool:
    s = str(event_id).strip()
    if not s:
        return False
    return s in MONITOR_EVENT_IDS


def filter_monitor_events(events: Iterable[Any]) -> List[Any]:
    return [e for e in events if is_allowed_monitor_event(e)]
