"""
Syslog 文件尾随轮询器
通过 polling 方式监控系统日志文件变更，解析新增行并映射为内部事件。
支持自定义正则规则。
"""

import fnmatch
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any, Set, Tuple
from zoneinfo import ZoneInfo

from .models import JournalEntry

# 不关注的 systemd 服务（fnmatch 模式）
SYSTEMD_EXCLUDE_SERVICES = [
    "anacron*",
    "NetworkManager-dispatcher*",
    "cups*",
    "cups-browsed*",
    "packagekit*",
    "dbus*",
    "systemd-*",
    "user@*",
    "session-*",
    "user-*",
]

# 默认监控的日志文件
DEFAULT_SYSLOG_PATHS = [
    "/var/log/auth.log",
    "/var/log/syslog",
    "/var/log/kern.log",
]

# 内置正则规则：(编译正则, 事件类型, 提取字段映射)
# groups 中 $1, $2 对应正则分组
BUILTIN_PATTERNS: List[dict] = [
    # SSH 事件
    {"pattern": r"sshd\[\d+\].*Accepted (publickey|password) for (\S+)", "event": "SSH_LOGIN_SUCCESS", "groups": {"method": "$1", "user": "$2"}},
    {"pattern": r"sshd\[\d+\].*Failed password for (\S+)", "event": "SSH_AUTH_FAILED", "groups": {"user": "$1"}},
    {"pattern": r"sshd\[\d+\].*Invalid user (\S+)", "event": "SSH_INVALID_USER", "groups": {"user": "$1"}},
    {"pattern": r"sshd\[\d+\].*Disconnected from(?: authenticating)? user (\S+)", "event": "SSH_DISCONNECTED", "groups": {"user": "$1"}},
    {"pattern": r"sshd\[\d+\].*Received disconnect from", "event": "SSH_DISCONNECTED", "groups": {}},
    # 登录事件
    {"pattern": r"login\[\d+\].*session opened for user (\S+)", "event": "LoginSucc", "groups": {"user": "$1"}},
    {"pattern": r"login\[\d+\].*session closed for user (\S+)", "event": "Logout", "groups": {"user": "$1"}},
    {"pattern": r"gdm-password.*authentication failure", "event": "LoginFail", "groups": {}},
    {"pattern": r"(sudo|su):.*session opened for user (\S+)", "event": "LoginSucc", "groups": {"user": "$2"}},
    {"pattern": r"gdm-password\[\d+\].*session opened for user (\S+)", "event": "LoginSucc", "groups": {"user": "$1"}},
    {"pattern": r"pam_unix\(login:auth\).*authentication failure.*user=(\S+)", "event": "LoginFail", "groups": {"user": "$1"}},
    # Systemd 服务事件
    {"pattern": r"systemd\[\d+\].*Started (.+\.service)", "event": "SYSTEMD_SERVICE_STARTED", "groups": {"unit": "$1"}},
    {"pattern": r"systemd\[\d+\].*Stopped (.+\.service)", "event": "SYSTEMD_SERVICE_STOPPED", "groups": {"unit": "$1"}},
    {"pattern": r"systemd\[\d+\].*Restarting (.+\.service)", "event": "SYSTEMD_SERVICE_RESTARTED", "groups": {"unit": "$1"}},
    {"pattern": r"systemd\[\d+\].*Failed to start (.+\.service)", "event": "SYSTEMD_SERVICE_FAILED", "groups": {"unit": "$1"}},
    # 内核磁盘事件
    {"pattern": r"kernel:.*New USB storage", "event": "FoundDisk", "groups": {}},
    {"pattern": r"kernel:.*new high-speed USB", "event": "FoundDisk", "groups": {}},
    {"pattern": r"kernel:.*I/O error", "event": "DISK_IO_ERR", "groups": {}},
    {"pattern": r"kernel:.*Buffer I/O error on device (sd\w+)", "event": "DISK_IO_ERR", "groups": {"disk": "$1"}},
    {"pattern": r"kernel:.*(sd\w+).*SPINUP", "event": "DiskWakeup", "groups": {"disk": "$1"}},
    {"pattern": r"kernel:.*(sd\w+).*START", "event": "DiskWakeup", "groups": {"disk": "$1"}},
    {"pattern": r"kernel:.*(sd\w+).*STOP", "event": "DiskSpindown", "groups": {"disk": "$1"}},
    {"pattern": r"kernel:.*(sd\w+).*SLEEP", "event": "DiskSpindown", "groups": {"disk": "$1"}},
    # udev 设备
    {"pattern": r"udev\[\d+\].*add.*(/dev/sd\w+)", "event": "InsertDisk", "groups": {"name": "$1"}},
    {"pattern": r"udev\[\d+\].*remove.*(/dev/sd\w+)", "event": "EjectDisk", "groups": {"name": "$1"}},
    # md/RAID
    {"pattern": r"kernel:.*md\d+.*degraded", "event": "STORAGE_DEGRADED", "groups": {}},
    {"pattern": r"kernel:.*md\d+.*FAILED", "event": "StorageBroken", "groups": {}},
]


class SyslogTailPoller:
    """尾随 syslog 文件，按行解析事件。"""

    def __init__(
        self,
        cursor_dir: str,
        syslog_paths: Optional[List[str]] = None,
        custom_patterns: Optional[List[Dict]] = None,
        poll_interval: int = 2,
        monitor_events: Optional[List[str]] = None,
    ):
        self.cursor_dir = Path(cursor_dir)
        self.poll_interval = max(1, poll_interval)
        self.monitor_events = set(monitor_events or [])
        self.event_handlers: Dict[str, Callable] = {}
        self.batch_handler: Optional[Callable[[List[Dict[str, Any]]], None]] = None
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self.logger = logging.getLogger(__name__)
        self.cursor_dir.mkdir(parents=True, exist_ok=True)

        # 待监控的文件路径
        self._paths: List[Path] = []
        for p in (syslog_paths or DEFAULT_SYSLOG_PATHS):
            pp = Path(p)
            if pp.parent.exists():
                self._paths.append(pp)

        # 当前文件位置（inode -> position）
        self._positions: Dict[int, int] = {}
        self._inode_cache: Dict[str, int] = {}

        # 编译内置正则
        self._patterns: List[dict] = []
        for rule in BUILTIN_PATTERNS:
            rule["_compiled"] = re.compile(rule["pattern"])
            self._patterns.append(rule)

        # 用户自定义正则
        self._custom_patterns: List[dict] = []
        if custom_patterns:
            for rule in custom_patterns:
                try:
                    rule["_compiled"] = re.compile(rule.get("pattern", ""))
                    self._custom_patterns.append(rule)
                except re.error as e:
                    self.logger.warning("自定义规则正则编译失败: %s - %s", rule.get("name", ""), e)

        # 收集自定义规则中的文件路径
        for rule in self._custom_patterns:
            fp = rule.get("file_path", "").strip()
            if fp:
                pp = Path(fp)
                if pp.parent.exists() and pp not in self._paths:
                    self._paths.append(pp)

    def add_handler(self, event_type: str, handler: Callable):
        self.event_handlers[event_type] = handler

    def clear_handlers(self) -> None:
        self.event_handlers.clear()

    def set_batch_handler(self, handler: Optional[Callable[[List[Dict[str, Any]]], None]]) -> None:
        self.batch_handler = handler

    def update_config(
        self,
        monitor_events: Optional[List[str]] = None,
        poll_interval: Optional[int] = None,
        custom_patterns: Optional[List[Dict]] = None,
    ) -> None:
        if monitor_events is not None:
            self.monitor_events = set(monitor_events)
        if poll_interval is not None:
            self.poll_interval = max(1, poll_interval)
        if custom_patterns is not None:
            self._custom_patterns = []
            for rule in custom_patterns:
                try:
                    rule["_compiled"] = re.compile(rule.get("pattern", ""))
                    self._custom_patterns.append(rule)
                except re.error as e:
                    self.logger.warning("自定义规则正则编译失败: %s", e)
            # 重新收集自定义文件路径
            for rule in self._custom_patterns:
                fp = rule.get("file_path", "").strip()
                if fp:
                    pp = Path(fp)
                    if pp.parent.exists() and pp not in self._paths:
                        self._paths.append(pp)

    def _get_inode(self, path: Path) -> int:
        try:
            st = path.stat()
            return st.st_ino
        except OSError:
            return 0

    def _read_new_lines(self, path: Path) -> List[str]:
        """读取文件新增行。处理日志轮转（inode 变化时从头读）。"""
        inode = self._get_inode(path)
        if not inode:
            return []

        pos_key = path.name
        cached_inode = self._inode_cache.get(pos_key)
        if cached_inode and cached_inode != inode:
            # 日志轮转，重置位置
            self._positions[inode] = 0

        self._inode_cache[pos_key] = inode
        offset = self._positions.get(inode, 0)

        try:
            with open(path, "r", errors="replace") as f:
                if offset > 0:
                    f.seek(offset)
                else:
                    # 首次读取或轮转后：跳到末尾，不处理历史行
                    f.seek(0, 2)
                lines = f.readlines()
                self._positions[inode] = f.tell()
        except (FileNotFoundError, PermissionError):
            return []

        return [l.rstrip("\n\r") for l in lines if l.strip()]

    def _is_excluded_service(self, unit_name: str) -> bool:
        for pat in SYSTEMD_EXCLUDE_SERVICES:
            if fnmatch.fnmatch(unit_name, pat):
                return True
        return False

    def _parse_line(self, line: str, source_file: str, source_abs: str = "") -> Optional[Dict[str, Any]]:
        """解析一行日志，返回事件或 None。"""
        # 先匹配内置规则
        for rule in self._patterns:
            m = rule["_compiled"].search(line)
            if m:
                event_type = rule["event"]
                if self.monitor_events and event_type not in self.monitor_events:
                    return None
                event_data = self._extract_groups(m, rule.get("groups", {}))
                event_data["message"] = line
                # Systemd 服务排除
                if event_type.startswith("SYSTEMD_SERVICE_") and self._is_excluded_service(event_data.get("unit", "")):
                    return None
                return self._build_event(event_type, event_data, line, source_file)

        # 再匹配自定义规则
        for rule in self._custom_patterns:
            rp = rule.get("file_path", "").strip()
            if rp and rp != source_abs:
                continue
            m = rule["_compiled"].search(line)
            if m:
                event_type = rule.get("event_type", "SYSLOG_PATTERN_MATCH")
                if self.monitor_events and event_type not in self.monitor_events:
                    return None
                event_data = self._extract_groups(m, rule.get("groups", {}))
                event_data["message"] = line
                event_data["rule_name"] = rule.get("name", "")
                return self._build_event(event_type, event_data, line, source_file)

        return None

    def _extract_groups(self, m: re.Match, group_map: dict) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        for key, value in group_map.items():
            if value.startswith("$"):
                try:
                    idx = int(value[1:])
                    data[key] = m.group(idx) or ""
                except (ValueError, IndexError):
                    data[key] = value
            else:
                data[key] = value
        # 提取 IP
        ip_m = re.search(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", m.string)
        if ip_m:
            data["IP"] = ip_m.group(1)
        return data

    def _build_event(self, event_type: str, event_data: Dict, line: str, source_file: str) -> Dict:
        import hashlib
        cursor = hashlib.md5(line.encode()).hexdigest()[:16]
        ts = _syslog_ts(line, source_file) or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        return {
            "event_type": event_type,
            "event_data": event_data,
            "entry": JournalEntry(
                cursor=f"{source_file}:{cursor}",
                timestamp=ts,
                hostname=source_file,
                syslog_identifier=event_type,
                message=line,
                priority=6,
                pid=0,
                raw_data=line,
                original_line=line,
            ),
        }

    def _poll_once(self) -> None:
        """轮询所有被监控文件。"""
        batch_events: List[Dict[str, Any]] = []

        for path in self._paths:
            lines = self._read_new_lines(path)
            if not lines:
                continue
            source = path.name
            source_abs = str(path)
            for line in lines:
                event = self._parse_line(line, source, source_abs)
                if event:
                    if self.batch_handler:
                        batch_events.append(event)
                    else:
                        handler = self.event_handlers.get(event["event_type"])
                        if handler:
                            try:
                                handler(event["event_data"], event["entry"])
                            except Exception as e:
                                self.logger.error("处理事件失败 %s: %s", event["event_type"], e)

        if self.batch_handler and batch_events:
            try:
                self.batch_handler(batch_events)
            except Exception as e:
                self.logger.error("批量处理事件失败: %s", e)

    def _run_loop(self) -> None:
        self.logger.info("SyslogTailPoller 启动，监控文件: %s", [str(p) for p in self._paths])
        while self.running:
            try:
                self._poll_once()
            except Exception as e:
                self.logger.error("syslog 轮询异常: %s", e, exc_info=True)
            for _ in range(self.poll_interval):
                if not self.running:
                    return
                time.sleep(1)

    def start(self) -> None:
        if self.running:
            return
        if not self._paths:
            self.logger.warning("未配置可用的 syslog 文件路径，跳过")
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, name="SyslogTailPoller", daemon=False)
        self._thread.start()
        self.logger.info("SyslogTailPoller 已启动")

    def stop(self) -> None:
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.poll_interval + 2)
        self.logger.info("SyslogTailPoller 已停止")


def _syslog_ts(line: str, source: str) -> Optional[str]:
    """从 syslog 行首提取时间戳。"""
    try:
        import re
        m = re.match(r"^(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})", line)
        if m:
            raw = m.group(1)
            year = datetime.now().year
            return datetime.strptime(f"{raw} {year}", "%b %d %H:%M:%S %Y").strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return None
