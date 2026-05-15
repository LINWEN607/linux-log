"""
事件处理器模块
"""

import logging
import json
from typing import Dict, Any, Callable, Optional, List
from datetime import datetime
from threading import Timer

from .models import JournalEntry
from src.notifier.multi_platform_notifier import normalize_disk_io_payload
from src.notifier.unified_notifier import UnifiedNotifier
from src.utils.log_storage import LogStorage


class EventProcessor:
    """事件处理器"""

    def __init__(self, notifier: UnifiedNotifier, config):
        self.notifier = notifier
        self.config = config
        self.logger = logging.getLogger(__name__)

        log_retention_days = getattr(config, 'log_retention_days', 30)
        self.log_storage = LogStorage(
            storage_dir=getattr(config, 'log_dir', './data/logs'),
            days_to_keep=log_retention_days,
            enable_auto_cleanup=True
        )

        self.handlers = {
            # 登录事件
            'LoginSucc': self._handle_login_success,
            'LoginFail': self._handle_login_fail,
            'Logout': self._handle_logout,
            # SSH 事件
            'SSH_INVALID_USER': self._handle_ssh_invalid_user,
            'SSH_AUTH_FAILED': self._handle_ssh_auth_failed,
            'SSH_LOGIN_SUCCESS': self._handle_ssh_login_success,
            'SSH_DISCONNECTED': self._handle_ssh_disconnected,
            # 磁盘与存储
            'FoundDisk': self._handle_found_disk,
            'InsertDisk': lambda ed, e: self._handle_simple_notification('InsertDisk', ed, e),
            'EjectDisk': lambda ed, e: self._handle_simple_notification('EjectDisk', ed, e),
            'StorageBroken': lambda ed, e: self._handle_simple_notification('StorageBroken', ed, e),
            'STORAGE_DEGRADED': lambda ed, e: self._handle_simple_notification('STORAGE_DEGRADED', ed, e),
            'DiskWakeup': self._handle_disk_wakeup,
            'DiskSpindown': self._handle_disk_spindown,
            'DISK_IO_ERR': self._handle_disk_io_err,
            # Systemd 服务
            'SYSTEMD_SERVICE_STARTED': self._handle_systemd_service_event,
            'SYSTEMD_SERVICE_STOPPED': self._handle_systemd_service_event,
            'SYSTEMD_SERVICE_RESTARTED': self._handle_systemd_service_event,
            'SYSTEMD_SERVICE_FAILED': self._handle_systemd_service_event,
            # Docker 容器
            'DOCKER_CONTAINER_CREATE': lambda ed, e: self._handle_docker_notification('DOCKER_CONTAINER_CREATE', ed, e),
            'DOCKER_CONTAINER_START': lambda ed, e: self._handle_docker_notification('DOCKER_CONTAINER_START', ed, e),
            'DOCKER_CONTAINER_STOP': lambda ed, e: self._handle_docker_notification('DOCKER_CONTAINER_STOP', ed, e),
            'DOCKER_CONTAINER_DIE': lambda ed, e: self._handle_docker_notification('DOCKER_CONTAINER_DIE', ed, e),
            'DOCKER_CONTAINER_OOM': lambda ed, e: self._handle_docker_notification('DOCKER_CONTAINER_OOM', ed, e),
            'DOCKER_CONTAINER_KILL': lambda ed, e: self._handle_docker_notification('DOCKER_CONTAINER_KILL', ed, e),
            'DOCKER_CONTAINER_PAUSE': lambda ed, e: self._handle_docker_notification('DOCKER_CONTAINER_PAUSE', ed, e),
            'DOCKER_CONTAINER_UNPAUSE': lambda ed, e: self._handle_docker_notification('DOCKER_CONTAINER_UNPAUSE', ed, e),
            'DOCKER_CONTAINER_RESTART': lambda ed, e: self._handle_docker_notification('DOCKER_CONTAINER_RESTART', ed, e),
            'DOCKER_CONTAINER_DESTROY': lambda ed, e: self._handle_docker_notification('DOCKER_CONTAINER_DESTROY', ed, e),
            # 自定义规则
            'SYSLOG_PATTERN_MATCH': lambda ed, e: self._handle_simple_notification('SYSLOG_PATTERN_MATCH', ed, e),
            # 系统资源（巡检告警）
            'PATROL_CPU_ALARM': lambda ed, e: self._handle_simple_notification('PATROL_CPU_ALARM', ed, e),
            'PATROL_CPU_RESTORED': lambda ed, e: self._handle_simple_notification('PATROL_CPU_RESTORED', ed, e),
            'PATROL_MEM_ALARM': lambda ed, e: self._handle_simple_notification('PATROL_MEM_ALARM', ed, e),
            'PATROL_MEM_RESTORED': lambda ed, e: self._handle_simple_notification('PATROL_MEM_RESTORED', ed, e),
            'PATROL_DISK_ALARM': lambda ed, e: self._handle_simple_notification('PATROL_DISK_ALARM', ed, e),
            'PATROL_DISK_RESTORED': lambda ed, e: self._handle_simple_notification('PATROL_DISK_RESTORED', ed, e),
        }

        # 磁盘事件合并缓存
        self.disk_wakeup_cache = []
        self.disk_spindown_cache = []
        self.merge_window = 30
        self.wakeup_timer = None
        self.spindown_timer = None

        # SSH认证失败去重
        self.ssh_auth_fail_cache = {}
        self.ssh_auth_fail_window = 5
        self.ssh_auth_fail_cache_max = 10000

        # SSH事件合并
        self.ssh_merge_window = 5
        self.ssh_pending = {}

        self.logger.info("事件处理器初始化完成")

    def _build_batch_event_brief(self, event_type: str, event_data: Dict[str, Any]) -> str:
        data = event_data.get('data') if isinstance(event_data.get('data'), dict) else {}
        parts = []
        if event_data.get('user') or event_data.get('IP'):
            parts.append(f"{event_data.get('user', '')}@{event_data.get('IP', '')}".strip("@"))
        if data.get('DISPLAY_NAME') or data.get('APP_NAME'):
            parts.append(data.get('DISPLAY_NAME') or data.get('APP_NAME'))
        if event_data.get('name'):
            parts.append(str(event_data.get('name')))
        if event_data.get('message'):
            parts.append(str(event_data.get('message'))[:80])
        if event_data.get('unit'):
            parts.append(str(event_data.get('unit'))[:60])
        if not parts:
            parts.append(event_type)
        return " | ".join([str(p).strip() for p in parts if p])[:120]

    def process_batch_events(self, batch_events: List[Dict[str, Any]]) -> bool:
        if not batch_events:
            return False

        latest_entry = batch_events[-1].get("entry")
        timestamp = getattr(latest_entry, "timestamp", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        by_type: Dict[str, int] = {}
        preview_items: List[Dict[str, str]] = []
        grouped_events: Dict[str, List[Dict[str, Any]]] = {}
        raw_for_storage: List[Dict[str, Any]] = []

        for item in batch_events:
            event_type = str(item.get("event_type") or "unknown")
            event_data = item.get("event_data") or {}
            entry = item.get("entry")
            by_type[event_type] = by_type.get(event_type, 0) + 1
            grouped_events.setdefault(event_type, []).append({
                "timestamp": getattr(entry, "timestamp", timestamp),
                "event_data": event_data,
                "raw_log": getattr(entry, "raw_data", "{}"),
            })

            if len(preview_items) < 10:
                preview_items.append({
                    "event_type": event_type,
                    "timestamp": getattr(entry, "timestamp", timestamp),
                    "brief": self._build_batch_event_brief(event_type, event_data),
                })

            raw_for_storage.append({
                "event_type": event_type,
                "event_data": event_data,
                "timestamp": getattr(entry, "timestamp", timestamp),
                "raw_log": getattr(entry, "raw_data", "{}"),
            })

            self._store_notification_log(
                event_type=event_type,
                event_data=event_data,
                raw_log=getattr(entry, "raw_data", "{}"),
                entry=entry,
                source='system'
            )

        summary_event_data = {
            "count": len(batch_events),
            "by_type": by_type,
            "items": preview_items,
            "grouped_events": grouped_events,
        }
        raw_log = json.dumps(raw_for_storage, ensure_ascii=False)[:6000]
        self.logger.info("轮询批量汇总推送：count=%s, types=%s", len(batch_events), len(by_type))
        self.notifier.send_notification(
            event_type='POLL_BATCH_SUMMARY',
            event_data=summary_event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        return True

    def _send_ssh_notification(self, event_type: str, event_data: Dict[str, Any], entry: JournalEntry):
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.notifier.send_notification(
            event_type=event_type,
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log(event_type, event_data, raw_log, entry)

    def _schedule_ssh_event(self, key: str, event_type: str, event_data: Dict[str, Any],
                            entry: JournalEntry, log_message: str):
        existing = self.ssh_pending.pop(key, None)
        if existing:
            existing.get('timer').cancel()

        def _flush():
            pending = self.ssh_pending.pop(key, None)
            if not pending:
                return
            self.logger.info(pending['log_message'])
            self._send_ssh_notification(pending['event_type'], pending['event_data'], pending['entry'])

        timer = Timer(self.ssh_merge_window, _flush)
        timer.daemon = True
        self.ssh_pending[key] = {
            'event_type': event_type,
            'event_data': event_data,
            'entry': entry,
            'log_message': log_message,
            'timer': timer
        }
        timer.start()

    def _store_notification_log(self, event_type: str, event_data: Dict[str, Any],
                                raw_log: str, entry: JournalEntry, source: str = "system"):
        try:
            actual_raw_log = entry.original_line if entry.original_line else raw_log
            success = self.log_storage.store_log(
                event_type=event_type,
                raw_log=actual_raw_log,
                processed_data=event_data,
                source=source
            )
            if success:
                self.logger.debug(f"日志存储成功: {event_type}")
            else:
                self.logger.warning(f"日志存储失败: {event_type}")
        except Exception as e:
            self.logger.error(f"存储日志时发生错误: {e}")

    def get_handler(self, event_type: str) -> Optional[Callable]:
        handler = self.handlers.get(event_type)
        if handler:
            return handler
        return None

    # ---- 登录事件 ----

    def _handle_login_success(self, event_data: Dict[str, Any], entry: JournalEntry):
        user = event_data.get('user', '')
        ip = event_data.get('IP', '')
        self.logger.info(f"登录成功: {user}@{ip}")
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.notifier.send_notification(
            event_type='LoginSucc',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log('LoginSucc', event_data, raw_log, entry)

    def _handle_login_fail(self, event_data: Dict[str, Any], entry: JournalEntry):
        user = event_data.get('user', '')
        ip = event_data.get('IP', '')
        self.logger.warning(f"登录失败: {user}@{ip}")
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        result = self.notifier.send_notification(
            event_type='LoginFail',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log('LoginFail', event_data, raw_log, entry)

    def _handle_logout(self, event_data: Dict[str, Any], entry: JournalEntry):
        user = event_data.get('user', '')
        ip = event_data.get('IP', '')
        self.logger.info(f"退出登录: {user}@{ip}")
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.notifier.send_notification(
            event_type='Logout',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )

    # ---- SSH 事件 ----

    def _handle_ssh_invalid_user(self, event_data: Dict[str, Any], entry: JournalEntry):
        user = event_data.get('user', 'unknown')
        ip = event_data.get('IP', 'unknown')
        self.logger.warning(f"SSH无效用户尝试: {user}@{ip}")
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.notifier.send_notification(
            event_type='SSH_INVALID_USER',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log('SSH_INVALID_USER', event_data, raw_log, entry)

    def _handle_ssh_auth_failed(self, event_data: Dict[str, Any], entry: JournalEntry):
        user = event_data.get('user', 'unknown')
        ip = event_data.get('IP', 'unknown')
        key = f"{ip or 'unknown'}|{user or 'unknown'}"
        now = datetime.now().timestamp()
        last_ts = self.ssh_auth_fail_cache.get(key)
        if last_ts and (now - last_ts) < self.ssh_auth_fail_window:
            self.logger.debug(f"SSH认证失败去重: {user}@{ip}")
            return
        self.ssh_auth_fail_cache[key] = now
        if len(self.ssh_auth_fail_cache) > self.ssh_auth_fail_cache_max:
            cutoff = now - (self.ssh_auth_fail_window * 2)
            self.ssh_auth_fail_cache = {
                k: ts for k, ts in self.ssh_auth_fail_cache.items()
                if ts >= cutoff
            }

        self.logger.warning(f"SSH认证失败: {user}@{ip}")
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.notifier.send_notification(
            event_type='SSH_AUTH_FAILED',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log('SSH_AUTH_FAILED', event_data, raw_log, entry)

    def _handle_ssh_login_success(self, event_data: Dict[str, Any], entry: JournalEntry):
        user = event_data.get('user', 'unknown')
        ip = event_data.get('IP', 'unknown')
        self._schedule_ssh_event(
            key=f"ssh_login_success:{user}",
            event_type='SSH_LOGIN_SUCCESS',
            event_data=event_data,
            entry=entry,
            log_message=f"SSH登录成功: {user}@{ip}"
        )

    def _handle_ssh_disconnected(self, event_data: Dict[str, Any], entry: JournalEntry):
        user = event_data.get('user', 'unknown')
        ip = event_data.get('IP', 'unknown')
        self._schedule_ssh_event(
            key=f"ssh_disconnected:{user}",
            event_type='SSH_DISCONNECTED',
            event_data=event_data,
            entry=entry,
            log_message=f"SSH断开连接: {user}@{ip}"
        )

    # ---- 磁盘事件 ----

    def _handle_found_disk(self, event_data: Dict[str, Any], entry: JournalEntry):
        name = event_data.get('name', '')
        self.logger.info(f"发现新存储设备: {name}")
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.notifier.send_notification(
            event_type='FoundDisk',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )

    def _handle_disk_io_err(self, event_data: Dict[str, Any], entry: JournalEntry):
        dev, _model, _sn, err_cnt = normalize_disk_io_payload(event_data)
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.warning(f"磁盘IO错误: {dev}, 错误次数 {err_cnt}")
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type='DISK_IO_ERR',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log('DISK_IO_ERR', event_data, raw_log, entry, source='system')

    # ---- Systemd 服务事件 ----

    def _handle_systemd_service_event(self, event_data: Dict[str, Any], entry: JournalEntry):
        event_type = getattr(entry, 'syslog_identifier', 'SYSTEMD_SERVICE_STARTED')
        unit = event_data.get('unit', '')
        self.logger.info(f"系统服务事件: {event_type} - {unit}")
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.notifier.send_notification(
            event_type=event_type,
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log(event_type, event_data, raw_log, entry, source='systemd')

    # ---- CPU / 内存事件 ----

    def _handle_cpu_usage_alarm(self, event_data: Dict[str, Any], entry: JournalEntry):
        threshold = event_data.get('threshold', event_data.get('THRESHOLD', 0))
        value = event_data.get('value', 0)
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.warning(f"CPU使用率告警: {value}% (阈值 {threshold}%)")
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type='CPU_USAGE_ALARM',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log('CPU_USAGE_ALARM', event_data, raw_log, entry, source='system')

    def _handle_cpu_usage_restored(self, event_data: Dict[str, Any], entry: JournalEntry):
        threshold = event_data.get('threshold', event_data.get('THRESHOLD', 0))
        value = event_data.get('value', 0)
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.info(f"CPU使用率恢复: {value}% (阈值 {threshold}%)")
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type='CPU_USAGE_RESTORED',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log('CPU_USAGE_RESTORED', event_data, raw_log, entry, source='system')

    def _handle_memory_usage_alarm(self, event_data: Dict[str, Any], entry: JournalEntry):
        threshold = event_data.get('threshold', event_data.get('THRESHOLD', 0))
        value = event_data.get('value', 0)
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.warning(f"内存使用率告警: {value}% (阈值 {threshold}%)")
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type='MEMORY_USAGE_ALARM',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log('MEMORY_USAGE_ALARM', event_data, raw_log, entry, source='system')

    def _handle_memory_usage_restored(self, event_data: Dict[str, Any], entry: JournalEntry):
        threshold = event_data.get('threshold', event_data.get('THRESHOLD', 0))
        value = event_data.get('value', 0)
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.info(f"内存使用率恢复: {value}% (阈值 {threshold}%)")
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type='MEMORY_USAGE_RESTORED',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log('MEMORY_USAGE_RESTORED', event_data, raw_log, entry, source='system')

    def _handle_cpu_temperature_alarm(self, event_data: Dict[str, Any], entry: JournalEntry):
        data = event_data.get('data', {})
        threshold = data.get('THRESHOLD', event_data.get('threshold', 0))
        value = event_data.get('value', 0)
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.warning(f"CPU温度告警: {value}°C (阈值 {threshold}°C)")
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type='CPU_TEMPERATURE_ALARM',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )

    # ---- 磁盘唤醒/休眠合并 ----

    def _add_to_cache_and_schedule_send(self, cache_list, timer_attr, event_data, event_type, entry):
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        disk_details = self._extract_disk_details(event_data)
        if not disk_details:
            disk_details = [{'disk': '', 'model': '', 'serial': ''}]

        for detail in disk_details:
            event_entry = {
                'disk': detail.get('disk', ''),
                'model': detail.get('model', ''),
                'serial': detail.get('serial', ''),
                'timestamp': timestamp,
                'raw_log': getattr(entry, 'raw_data', '{}'),
                'full_event_data': event_data
            }
            cache_list.append(event_entry)
            disk_label = event_entry['disk'] or event_entry['serial'] or event_entry['model'] or "(未知)"
            self.logger.info(f"磁盘事件缓存: {disk_label} ({event_type})")

        old_timer = getattr(self, timer_attr, None)
        if old_timer and old_timer.is_alive():
            old_timer.cancel()

        new_timer = Timer(self.merge_window, lambda: self._send_merged_events(cache_list, event_type))
        new_timer.start()
        setattr(self, timer_attr, new_timer)

    def _handle_disk_wakeup(self, event_data: Dict[str, Any], entry: JournalEntry):
        self._add_to_cache_and_schedule_send(self.disk_wakeup_cache, 'wakeup_timer', event_data, 'DiskWakeup', entry)
        raw_log = getattr(entry, 'raw_data', '{}')
        self._store_notification_log('DiskWakeup', event_data, raw_log, entry)

    def _handle_disk_spindown(self, event_data: Dict[str, Any], entry: JournalEntry):
        self._add_to_cache_and_schedule_send(self.disk_spindown_cache, 'spindown_timer', event_data, 'DiskSpindown', entry)
        raw_log = getattr(entry, 'raw_data', '{}')
        self._store_notification_log('DiskSpindown', event_data, raw_log, entry)

    def _send_merged_events(self, cache_list, event_type):
        if not cache_list:
            return

        merged_data = {
            'merged_disks': cache_list,
            'count': len(cache_list),
            'event_type': event_type
        }
        latest_timestamp = cache_list[-1]['timestamp']
        self.logger.info(f"合并推送磁盘事件: {event_type} x {len(cache_list)}")
        self.notifier.send_notification(
            event_type=event_type,
            event_data=merged_data,
            raw_log=str(cache_list),
            timestamp=latest_timestamp
        )
        cache_list.clear()

    def _extract_disk_details(self, event_data: Dict[str, Any]) -> List[Dict[str, str]]:
        details = []

        def add_candidate(source):
            if isinstance(source, dict):
                details.append(source)

        disk = self._pick_disk_field(event_data)
        model = self._pick_field(event_data, ['model', 'MODEL', 'Model'])
        serial = self._pick_field(event_data, ['serial', 'SERIAL', 'Serial', 'sn', 'SN'])
        if disk or model or serial:
            return [{'disk': disk, 'model': model, 'serial': serial}]

        data_section = event_data.get('data')
        add_candidate(event_data)
        if isinstance(data_section, dict):
            add_candidate(data_section)

        list_keys = ['disks', 'disk_list', 'devices', 'DISKS', 'DEVICES']
        for container in filter(lambda x: isinstance(x, dict), [event_data, data_section]):
            for key in list_keys:
                items = container.get(key)
                if isinstance(items, list):
                    for item in items:
                        add_candidate(item)

        for container in list(details):
            disk_field = container.get('disk')
            if isinstance(disk_field, dict):
                add_candidate(disk_field)

        normalized = []
        for candidate in details:
            normalized_entry = {
                'disk': self._pick_disk_field(candidate),
                'model': self._pick_field(candidate, ['model', 'MODEL', 'Model']),
                'serial': self._pick_field(candidate, ['serial', 'SERIAL', 'sn', 'SN']),
            }
            if any(normalized_entry.values()):
                normalized.append(normalized_entry)

        if not normalized:
            for single in [event_data, event_data.get('data') or {}]:
                if not isinstance(single, dict) or single in details:
                    continue
                disk = self._pick_disk_field(single)
                model = self._pick_field(single, ['model', 'MODEL', 'Model'])
                serial = self._pick_field(single, ['serial', 'SERIAL', 'sn', 'SN'])
                if disk or model or serial:
                    normalized.append({'disk': disk, 'model': model, 'serial': serial})
                    break

        return normalized

    def _pick_disk_field(self, candidate: Dict[str, Any]) -> str:
        disk = self._pick_field(candidate, ['disk', 'device', 'path', 'name', 'dev', 'DEV', 'DEVICE'])
        if disk:
            return disk
        slot = self._pick_field(candidate, ['slot', 'slot_id', 'bay', 'index'])
        if slot:
            return f"槽位 {slot}"
        paths = candidate.get('paths') or candidate.get('PATHS')
        if isinstance(paths, list) and paths:
            return str(paths[0])
        return ''

    def _pick_field(self, candidate: Dict[str, Any], keys: List[str]) -> str:
        for key in keys:
            if key in candidate and candidate[key]:
                return self._coerce_str(candidate[key])
            upper = key.upper()
            if upper in candidate and candidate[upper]:
                return self._coerce_str(candidate[upper])
            camel = key[:1].lower() + key[1:]
            if camel in candidate and candidate[camel]:
                return self._coerce_str(candidate[camel])
        return ''

    def _coerce_str(self, value: Any) -> str:
        if isinstance(value, dict):
            for nested_key in ['path', 'device', 'name', 'disk', 'value']:
                if nested_key in value and value[nested_key]:
                    return str(value[nested_key])
            return ''
        if isinstance(value, list):
            return str(value[0]) if value else ''
        if value is None:
            return ''
        return str(value)

    # ---- 通用通知 ----

    def _handle_simple_notification(self, event_type: str, event_data: Dict[str, Any], entry: JournalEntry):
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type=event_type,
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log(event_type, event_data, raw_log, entry, source='system')

    def _handle_docker_notification(self, event_type: str, event_data: Dict[str, Any], entry: JournalEntry):
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        raw_log = getattr(entry, 'raw_data', '{}')
        label = event_data.get("container_name") or event_data.get("container_id") or ""
        self.logger.info("Docker 容器事件 %s: %s", event_type, label)
        self.notifier.send_notification(
            event_type=event_type,
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp,
        )
        self._store_notification_log(event_type, event_data, raw_log, entry, source="docker")

    def process_event(self, event_type: str, event_data: Dict[str, Any], entry: JournalEntry):
        handler = self.get_handler(event_type)
        if handler:
            try:
                handler(event_data, entry)
                return True
            except Exception as e:
                self.logger.error(f"处理事件失败: {e}")
                return False
        else:
            self.logger.warning(f"未知事件类型: {event_type}")
            return False
