"""
配置管理模块
"""

import copy
import os
import json
from typing import List, Dict, Any
from dataclasses import dataclass, field, fields
from pathlib import Path
from src.utils.value_parser import as_bool
from src.valid_event_ids import filter_monitor_events

TITLE_PREFIX_DEFAULT = "Linux"


@dataclass
class Config:
    """应用配置"""

    # Webhook配置
    wechat_webhook_url: str = ""
    dingtalk_webhook_url: str = ""
    feishu_webhook_url: str = ""
    bark_url: str = ""
    bark_icon: str = ""
    pushplus_params: str = ""
    magic_push_params: str = ""
    smtp_params: str = ""

    # 通知标题配置
    title_prefix: str = field(default=TITLE_PREFIX_DEFAULT)
    minimal_push_enabled: bool = False

    # 监控配置
    monitor_events: List[str] = field(default_factory=lambda: [
        "LoginSucc", "LoginFail", "Logout",
        "SSH_INVALID_USER", "SSH_AUTH_FAILED", "SSH_LOGIN_SUCCESS", "SSH_DISCONNECTED",
        "FoundDisk", "InsertDisk", "EjectDisk", "StorageBroken", "STORAGE_DEGRADED",
        "DiskWakeup", "DiskSpindown", "DISK_IO_ERR",
        "SYSTEMD_SERVICE_STARTED", "SYSTEMD_SERVICE_STOPPED", "SYSTEMD_SERVICE_FAILED",
    ])

    # 日志配置
    log_level: str = "INFO"
    log_dir: str = "./data/logs"
    log_retention_days: int = 30

    # 连接池配置
    http_pool_size: int = 10
    http_retry_count: int = 3
    http_timeout: int = 10
    dedup_window: int = 300

    # 数据源配置
    cursor_dir: str = "./data/cursor"
    logger_poll_interval: int = 2  # 秒，轮询间隔

    # 数据源模式：journald / syslog-tail / both
    data_source: str = "syslog-tail"
    syslog_paths: List[str] = field(default_factory=lambda: [
        "/var/log/auth.log",
        "/var/log/syslog",
        "/var/log/kern.log",
    ])
    custom_patterns: List[Dict] = field(default_factory=list)

    # Systemd 服务监控
    systemd_service_monitor: bool = True
    systemd_service_exclude: List[str] = field(default_factory=lambda: [
        "dbus*.service",
        "systemd-*.service",
        "user@*.service",
        "session-*.scope",
        "user-*.slice",
    ])

    # 资源监控（阈值告警，基于 psutil）
    resource_monitor_enabled: bool = True
    cpu_alarm_threshold: int = 90
    memory_alarm_threshold: int = 90
    cpu_temp_alarm_threshold: int = 80
    resource_monitor_interval: int = 60

    # 轮询汇总模式
    poll_batch_summary_enabled: bool = False

    # 勿扰模式
    dnd_enabled: bool = False
    dnd_start_time: str = "22:00"
    dnd_end_time: str = "07:00"

    # 系统巡检
    system_patrol_enabled: bool = False
    patrol_cpu_threshold: int = 90
    patrol_cpu_interval: int = 1
    patrol_cpu_period: int = 60
    patrol_mem_threshold: int = 90
    patrol_mem_interval: int = 1
    patrol_mem_period: int = 60
    patrol_disk_threshold: int = 90

    # 高级配置
    max_log_age: int = 7
    notification_restart_enabled: bool = True
    notification_restart_consecutive_failures: int = 10
    notification_restart_window: int = 1800
    notification_restart_cooldown: int = 3600

    def __post_init__(self):
        """初始化后处理"""
        self._env_set_keys = set()
        self._load_from_env()
        self._load_from_file_skip_if_set()
        self.title_prefix = (self.title_prefix or "").strip()
        self._validate()
        self._ensure_directories()

    def _get_config_file_path(self) -> Path:
        app_home = os.getenv("APP_HOME")
        if app_home:
            return Path(app_home) / "config" / "config.json"
        if Path("/app/config/config.json").exists():
            return Path("/app/config/config.json")
        candidate = Path(__file__).resolve().parent.parent / "config" / "config.json"
        if candidate.exists():
            return candidate
        return Path("/app/config/config.json")

    def _load_from_file_skip_if_set(self):
        config_file = self._get_config_file_path()
        if config_file.exists():
            try:
                with open(config_file, 'r') as f:
                    data = json.load(f)
                for key, value in data.items():
                    if hasattr(self, key):
                        if key in self._env_set_keys:
                            continue
                        if isinstance(value, str) and value.startswith('${') and value.endswith('}'):
                            env_var_name = value[2:-1]
                            env_value = os.getenv(env_var_name, '')
                            setattr(self, key, env_value)
                        else:
                            setattr(self, key, value)
            except Exception as e:
                print(f"警告: 配置文件读取失败 - {e}")

    def reload_from_file(self, config_path: Path) -> bool:
        if not config_path.exists():
            return False
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return False

        _backup = {f.name: copy.deepcopy(getattr(self, f.name)) for f in fields(self)}

        def env_skip(field: str) -> bool:
            return field in self._env_set_keys

        if not env_skip("monitor_events") and "monitor_events" in data and isinstance(data["monitor_events"], list):
            self.monitor_events = data["monitor_events"]
        if not env_skip("wechat_webhook_url") and "wechat_webhook_url" in data and isinstance(data["wechat_webhook_url"], str):
            self.wechat_webhook_url = data["wechat_webhook_url"]
        if not env_skip("dingtalk_webhook_url") and "dingtalk_webhook_url" in data and isinstance(data["dingtalk_webhook_url"], str):
            self.dingtalk_webhook_url = data["dingtalk_webhook_url"]
        if not env_skip("feishu_webhook_url") and "feishu_webhook_url" in data and isinstance(data["feishu_webhook_url"], str):
            self.feishu_webhook_url = data["feishu_webhook_url"]
        if not env_skip("bark_url") and "bark_url" in data and isinstance(data["bark_url"], str):
            self.bark_url = data["bark_url"]
        if not env_skip("bark_icon") and "bark_icon" in data and isinstance(data["bark_icon"], str):
            self.bark_icon = data["bark_icon"]
        if not env_skip("minimal_push_enabled") and "minimal_push_enabled" in data:
            self.minimal_push_enabled = as_bool(data["minimal_push_enabled"], False)
        if not env_skip("smtp_params") and "smtp_params" in data and isinstance(data["smtp_params"], str):
            self.smtp_params = data["smtp_params"]
        if "pushplus_params" in data and isinstance(data["pushplus_params"], str):
            self.pushplus_params = data["pushplus_params"]
        if "magic_push_params" in data and isinstance(data["magic_push_params"], str):
            self.magic_push_params = data["magic_push_params"]
        if "title_prefix" in data and isinstance(data["title_prefix"], str):
            self.title_prefix = (data["title_prefix"] or "").strip()
        if not env_skip("log_retention_days") and "log_retention_days" in data and data["log_retention_days"] is not None:
            try:
                self.log_retention_days = int(data["log_retention_days"])
            except (TypeError, ValueError):
                pass
        if not env_skip("logger_poll_interval") and "logger_poll_interval" in data and data["logger_poll_interval"] is not None:
            try:
                self.logger_poll_interval = int(data["logger_poll_interval"])
            except (TypeError, ValueError):
                pass
        if "dnd_enabled" in data:
            self.dnd_enabled = as_bool(data["dnd_enabled"], False)
        if "dnd_start_time" in data and isinstance(data["dnd_start_time"], str):
            self.dnd_start_time = data["dnd_start_time"].strip()
        if "dnd_end_time" in data and isinstance(data["dnd_end_time"], str):
            self.dnd_end_time = data["dnd_end_time"].strip()
        if "system_patrol_enabled" in data:
            self.system_patrol_enabled = as_bool(data["system_patrol_enabled"], False)
        if "system_patrol_interval_minutes" in data and data["system_patrol_interval_minutes"] is not None:
            try:
                self.system_patrol_interval_minutes = int(data["system_patrol_interval_minutes"])
            except (TypeError, ValueError):
                pass
        if "poll_batch_summary_enabled" in data:
            self.poll_batch_summary_enabled = as_bool(data["poll_batch_summary_enabled"], False)
        if not env_skip("data_source") and "data_source" in data and isinstance(data["data_source"], str):
            self.data_source = data["data_source"].strip()
        if "syslog_paths" in data and isinstance(data["syslog_paths"], list):
            self.syslog_paths = [str(x).strip() for x in data["syslog_paths"] if str(x).strip()]
        if "custom_patterns" in data and isinstance(data["custom_patterns"], list):
            self.custom_patterns = data["custom_patterns"]
        if "system_patrol_enabled" in data:
            self.system_patrol_enabled = as_bool(data["system_patrol_enabled"], False)
        if "patrol_cpu_threshold" in data and data["patrol_cpu_threshold"] is not None:
            try:
                self.patrol_cpu_threshold = int(data["patrol_cpu_threshold"])
            except (TypeError, ValueError):
                pass
        if "patrol_cpu_interval" in data and data["patrol_cpu_interval"] is not None:
            try:
                self.patrol_cpu_interval = int(data["patrol_cpu_interval"])
            except (TypeError, ValueError):
                pass
        if "patrol_cpu_period" in data and data["patrol_cpu_period"] is not None:
            try:
                self.patrol_cpu_period = int(data["patrol_cpu_period"])
            except (TypeError, ValueError):
                pass
        if "patrol_mem_threshold" in data and data["patrol_mem_threshold"] is not None:
            try:
                self.patrol_mem_threshold = int(data["patrol_mem_threshold"])
            except (TypeError, ValueError):
                pass
        if "patrol_mem_interval" in data and data["patrol_mem_interval"] is not None:
            try:
                self.patrol_mem_interval = int(data["patrol_mem_interval"])
            except (TypeError, ValueError):
                pass
        if "patrol_mem_period" in data and data["patrol_mem_period"] is not None:
            try:
                self.patrol_mem_period = int(data["patrol_mem_period"])
            except (TypeError, ValueError):
                pass
        if "patrol_disk_threshold" in data and data["patrol_disk_threshold"] is not None:
            try:
                self.patrol_disk_threshold = int(data["patrol_disk_threshold"])
            except (TypeError, ValueError):
                pass
        self.title_prefix = (self.title_prefix or "").strip()
        try:
            self._validate()
        except ValueError as e:
            for f in fields(self):
                setattr(self, f.name, _backup[f.name])
            print(f"警告: 热加载配置未通过校验，已保持热加载前内存中的配置: {e}")
            return False
        return True

    def _load_from_env(self):
        # Webhook URLs
        if wechat_webhook := os.getenv('WECHAT_WEBHOOK_URL'):
            self.wechat_webhook_url = wechat_webhook
            self._env_set_keys.add('wechat_webhook_url')
        elif webhook := os.getenv('WEBHOOK_URL'):
            self.wechat_webhook_url = webhook
            self._env_set_keys.add('wechat_webhook_url')
        if dingtalk_webhook := os.getenv('DINGTALK_WEBHOOK_URL'):
            self.dingtalk_webhook_url = dingtalk_webhook
            self._env_set_keys.add('dingtalk_webhook_url')
        if feishu_webhook := os.getenv('FEISHU_WEBHOOK_URL'):
            self.feishu_webhook_url = feishu_webhook
            self._env_set_keys.add('feishu_webhook_url')
        if bark_url := os.getenv('BARK_URL'):
            self.bark_url = bark_url
            self._env_set_keys.add('bark_url')
        if bark_icon := os.getenv('BARK_ICON'):
            self.bark_icon = bark_icon
            self._env_set_keys.add('bark_icon')
        if minimal_push_enabled := os.getenv('MINIMAL_PUSH_ENABLED'):
            self.minimal_push_enabled = minimal_push_enabled.lower() in ['1', 'true', 'yes', 'on']
            self._env_set_keys.add('minimal_push_enabled')
        if smtp_params := os.getenv('SMTP_PARAMS'):
            self.smtp_params = smtp_params
            self._env_set_keys.add('smtp_params')

        if events := os.getenv('MONITOR_EVENTS'):
            self.monitor_events = [e.strip() for e in events.split(',')]
            self._env_set_keys.add('monitor_events')

        if log_level := os.getenv('LOG_LEVEL'):
            self.log_level = log_level.upper()
            self._env_set_keys.add('log_level')

        if pool_size := os.getenv('HTTP_POOL_SIZE'):
            try:
                self.http_pool_size = int(pool_size)
                self._env_set_keys.add('http_pool_size')
            except (TypeError, ValueError):
                print(f"警告: HTTP_POOL_SIZE 不是整数，已忽略: {pool_size}")

        if retry_count := os.getenv('HTTP_RETRY_COUNT'):
            try:
                self.http_retry_count = int(retry_count)
                self._env_set_keys.add('http_retry_count')
            except (TypeError, ValueError):
                print(f"警告: HTTP_RETRY_COUNT 不是整数，已忽略: {retry_count}")

        if timeout := os.getenv('HTTP_TIMEOUT'):
            try:
                self.http_timeout = int(timeout)
                self._env_set_keys.add('http_timeout')
            except (TypeError, ValueError):
                print(f"警告: HTTP_TIMEOUT 不是整数，已忽略: {timeout}")

        if dedup_window := os.getenv('DEDUP_WINDOW'):
            try:
                self.dedup_window = int(dedup_window)
                self._env_set_keys.add('dedup_window')
            except (TypeError, ValueError):
                print(f"警告: DEDUP_WINDOW 不是整数，已忽略: {dedup_window}")

        if data_source := os.getenv('DATA_SOURCE'):
            self.data_source = data_source.strip().lower()
            self._env_set_keys.add('data_source')

        if poll_interval := os.getenv('LOGGER_POLL_INTERVAL'):
            try:
                self.logger_poll_interval = int(poll_interval)
                self._env_set_keys.add('logger_poll_interval')
            except (TypeError, ValueError):
                print(f"警告: LOGGER_POLL_INTERVAL 不是整数，已忽略: {poll_interval}")

        if systemd_exclude := os.getenv('SYSTEMD_SERVICE_EXCLUDE'):
            self.systemd_service_exclude = [x.strip() for x in systemd_exclude.split(',') if x.strip()]
            self._env_set_keys.add('systemd_service_exclude')

        if cpu_threshold := os.getenv('CPU_ALARM_THRESHOLD'):
            try:
                self.cpu_alarm_threshold = int(cpu_threshold)
                self._env_set_keys.add('cpu_alarm_threshold')
            except (TypeError, ValueError):
                pass

        if mem_threshold := os.getenv('MEMORY_ALARM_THRESHOLD'):
            try:
                self.memory_alarm_threshold = int(mem_threshold)
                self._env_set_keys.add('memory_alarm_threshold')
            except (TypeError, ValueError):
                pass

        if max_age := os.getenv('MAX_LOG_AGE'):
            try:
                self.max_log_age = int(max_age)
                self._env_set_keys.add('max_log_age')
            except (TypeError, ValueError):
                print(f"警告: MAX_LOG_AGE 不是整数，已忽略: {max_age}")

        if log_retention := os.getenv('LOG_RETENTION_DAYS'):
            try:
                self.log_retention_days = int(log_retention)
                self._env_set_keys.add('log_retention_days')
            except (TypeError, ValueError):
                print(f"警告: LOG_RETENTION_DAYS 不是整数，已忽略: {log_retention}")

        if notify_restart_enabled := os.getenv('NOTIFY_RESTART_ENABLED'):
            self.notification_restart_enabled = notify_restart_enabled.lower() in ['1', 'true', 'yes', 'on']
            self._env_set_keys.add('notification_restart_enabled')

        if notify_restart_failures := os.getenv('NOTIFY_RESTART_CONSECUTIVE'):
            try:
                self.notification_restart_consecutive_failures = int(notify_restart_failures)
                self._env_set_keys.add('notification_restart_consecutive_failures')
            except (TypeError, ValueError):
                print(f"警告: NOTIFY_RESTART_CONSECUTIVE 不是整数，已忽略: {notify_restart_failures}")

        if notify_restart_window := os.getenv('NOTIFY_RESTART_WINDOW'):
            try:
                self.notification_restart_window = int(notify_restart_window)
                self._env_set_keys.add('notification_restart_window')
            except (TypeError, ValueError):
                print(f"警告: NOTIFY_RESTART_WINDOW 不是整数，已忽略: {notify_restart_window}")

        if notify_restart_cooldown := os.getenv('NOTIFY_RESTART_COOLDOWN'):
            try:
                self.notification_restart_cooldown = int(notify_restart_cooldown)
                self._env_set_keys.add('notification_restart_cooldown')
            except (TypeError, ValueError):
                print(f"警告: NOTIFY_RESTART_COOLDOWN 不是整数，已忽略: {notify_restart_cooldown}")

    def _validate(self):
        """验证配置"""
        if self.wechat_webhook_url and not self.wechat_webhook_url.startswith('http'):
            raise ValueError("WECHAT_WEBHOOK_URL 必须是有效的URL")
        if self.dingtalk_webhook_url and not self.dingtalk_webhook_url.startswith('http'):
            raise ValueError("DINGTALK_WEBHOOK_URL 必须是有效的URL")
        if self.feishu_webhook_url and not self.feishu_webhook_url.startswith('http'):
            raise ValueError("FEISHU_WEBHOOK_URL 必须是有效的URL")
        if self.bark_url and not self.bark_url.startswith('http'):
            raise ValueError("BARK_URL 必须是有效的URL")

        if self.pushplus_params:
            for part in (p.strip() for p in self.pushplus_params.split('|') if p.strip()):
                try:
                    obj = json.loads(part)
                    if not isinstance(obj, dict) or 'token' not in obj:
                        raise ValueError("PushPlus 参数必须为包含 token 的 JSON 对象")
                except json.JSONDecodeError as e:
                    raise ValueError(f"PushPlus 参数不是合法 JSON: {e}")

        if self.magic_push_params:
            for part in (p.strip() for p in self.magic_push_params.split("|") if p.strip()):
                try:
                    obj = json.loads(part)
                    if not isinstance(obj, dict):
                        raise ValueError("魔法推送参数必须为 JSON 对象")
                    base = (obj.get("base_url") or "").strip()
                    token = (obj.get("token") or "").strip()
                    if not base or not token:
                        raise ValueError("魔法推送须包含 base_url 与 token")
                    if not base.startswith("http"):
                        raise ValueError("魔法推送 base_url 须为有效 http(s) 地址")
                except json.JSONDecodeError as e:
                    raise ValueError(f"魔法推送参数不是合法 JSON: {e}")

        if self.smtp_params:
            for part in (p.strip() for p in self.smtp_params.split("|") if p.strip()):
                try:
                    obj = json.loads(part)
                    if not isinstance(obj, dict):
                        raise ValueError("SMTP 参数必须为 JSON 对象")
                    server = (obj.get("server") or "").strip()
                    username = (obj.get("username") or "").strip()
                    password = obj.get("password") or ""
                    to_raw = (obj.get("to") or "").strip()
                    if not server or not username or not password or not to_raw:
                        raise ValueError("SMTP 参数须包含 server、username、password、to")
                    try:
                        int(obj.get("port", 465))
                    except (TypeError, ValueError):
                        raise ValueError("SMTP port 必须为整数")
                except json.JSONDecodeError as e:
                    raise ValueError(f"SMTP 参数不是合法 JSON: {e}")

        if not self.monitor_events:
            raise ValueError("必须配置至少一个监控事件")

        self.monitor_events = filter_monitor_events(self.monitor_events)
        if not self.monitor_events:
            raise ValueError("必须配置至少一个监控事件")

        # 巡检阈值钳位
        self.patrol_cpu_threshold = max(1, min(100, self.patrol_cpu_threshold))
        self.patrol_cpu_interval = max(1, min(60, self.patrol_cpu_interval))
        self.patrol_cpu_period = max(1, min(1440, self.patrol_cpu_period))
        self.patrol_mem_threshold = max(1, min(100, self.patrol_mem_threshold))
        self.patrol_mem_interval = max(1, min(60, self.patrol_mem_interval))
        self.patrol_mem_period = max(1, min(1440, self.patrol_mem_period))
        self.patrol_disk_threshold = max(1, min(100, self.patrol_disk_threshold))

        if self.data_source not in ("syslog-tail", "journald", "both"):
            self.data_source = "syslog-tail"

    def _ensure_directories(self):
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)
        Path(self.cursor_dir).mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'monitor_events': self.monitor_events,
            'log_level': self.log_level,
            'http_pool_size': self.http_pool_size,
            'dedup_window': self.dedup_window,
            'wechat_webhook_url': self.wechat_webhook_url[:50] + '...'
                if len(self.wechat_webhook_url) > 50 else self.wechat_webhook_url
        }
