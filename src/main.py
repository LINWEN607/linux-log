
import sys
import signal
import socket
import os
import traceback
from datetime import datetime
import time
from pathlib import Path
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from utils.logger import setup_logging
from utils.push_stats import init as init_push_stats
from monitor.event_processor import EventProcessor
from monitor.system_patrol import start_system_patrol_thread
from monitor.docker_events_poller import DockerEventsPoller, DOCKER_POLL_EVENTS
from monitor.syslog_tail_poller import SyslogTailPoller
from notifier.unified_notifier import UnifiedNotifier
from web.ui_app import start_ui_server_in_background


class Application:
    """主应用程序"""

    def __init__(self):
        self.config = None
        self.notifier = None
        self.event_processor = None
        self.syslog_poller = None
        self.docker_events_poller = None
        self.logger = None
        self.running = False
        self.notification_health_thread = None
        self._system_patrol_thread = None
        self._exit_code = 0

    def _print_banner(self):
        banner = f"""
        启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        监控模式: {self._data_source_label()}
        通知方式: 企业微信/钉钉/飞书机器人/Bark/PushPlus/魔法推送/SMTP邮件

        """
        print(banner)

    def _data_source_label(self) -> str:
        ds = getattr(self.config, "data_source", "syslog-tail")
        if ds == "journald":
            return "Journald 日志"
        return "Syslog 文件尾随"

    def _dispatch_batch_events(self, batch_events):
        if self.event_processor:
            self.event_processor.process_batch_events(batch_events)

    def _build_data_source_pollers(self):
        """按 data_source 配置创建日志轮询器。"""
        if not self.config:
            return
        ds = getattr(self.config, "data_source", "syslog-tail")
        interval = self.config.logger_poll_interval
        me = self.config.monitor_events
        cdir = self.config.cursor_dir

        syslog_paths = getattr(self.config, "syslog_paths", None)
        custom_patterns = getattr(self.config, "custom_patterns", None)
        poller = SyslogTailPoller(
            cursor_dir=cdir,
            syslog_paths=syslog_paths,
            custom_patterns=custom_patterns,
            poll_interval=interval,
            monitor_events=me,
        )
        if getattr(self.config, "poll_batch_summary_enabled", False):
            poller.set_batch_handler(self._dispatch_batch_events)
        self.syslog_poller = poller
        print("Syslog 尾随轮询器已创建")

    def _build_docker_poller(self):
        if not self.config:
            return
        me = self.config.monitor_events
        if not (set(me) & set(DOCKER_POLL_EVENTS)):
            print("未勾选 Docker 容器事件，跳过 Docker 监听")
            return
        sock = (getattr(self.config, "docker_socket_path", "") or "").strip() or "/var/run/docker.sock"
        if not Path(sock).exists():
            print(f"已勾选 Docker 容器事件，但 socket 不存在: {sock}")
            return

        self.docker_events_poller = DockerEventsPoller(
            socket_path=sock,
            cursor_dir=self.config.cursor_dir,
            monitor_events=me,
        )
        print(f"Docker 容器事件监听已启用（socket: {sock}）")

    def initialize(self) -> bool:
        try:
            print("开始初始化应用组件...")

            self.config = Config()
            init_push_stats(self.config.cursor_dir)
            has_webhook = any([
                self.config.wechat_webhook_url,
                self.config.dingtalk_webhook_url,
                self.config.feishu_webhook_url,
                self.config.bark_url,
                self.config.pushplus_params,
                getattr(self.config, "magic_push_params", "") or "",
                getattr(self.config, "smtp_params", "") or "",
            ])
            if has_webhook:
                print("配置加载完成（已配置推送渠道）")
            else:
                print("配置加载完成（未配置推送渠道，可在 Web 配置页面添加）")

            self.logger = setup_logging(self.config)
            print("日志设置完成")

            self._print_banner()

            print(f"监控事件: {', '.join(self.config.monitor_events)}")
            print(f"日志级别: {self.config.log_level}")
            print(f"去重窗口: {self.config.dedup_window}秒")
            print(f"连接池大小: {self.config.http_pool_size}")

            if self.config.wechat_webhook_url:
                print(f"企业微信Webhook: 已配置")
            if self.config.dingtalk_webhook_url:
                print(f"钉钉Webhook: 已配置")
            if self.config.feishu_webhook_url:
                print(f"飞书Webhook: 已配置")
            if self.config.bark_url:
                print(f"Bark: 已配置")
            if self.config.pushplus_params:
                print(f"PushPlus: 已配置")
            if getattr(self.config, "magic_push_params", ""):
                print(f"魔法推送: 已配置")
            if getattr(self.config, "smtp_params", ""):
                print("SMTP邮件: 已配置")
            if not has_webhook:
                print("未配置推送渠道：不监听事件、不推送消息，仅提供 Web 配置页面。")
                print("初始化完成（待配置）。")
                return True

            print("初始化多平台通知器...")
            self.notifier = UnifiedNotifier(self.config)
            print("多平台通知器初始化完成")

            print("正在初始化事件处理器...")
            self.event_processor = EventProcessor(self.notifier, self.config)
            print("事件处理器初始化完成")

            print("正在初始化数据源轮询器...")
            self._build_data_source_pollers()

            print("正在初始化 Docker 事件监听...")
            self._build_docker_poller()

            print("开始注册事件处理器...")
            self._register_handlers()

            print(f"\n初始化完成，开始监控...")
            return True
        except Exception as e:
            print(f"初始化失败: {e}")
            traceback.print_exc()
            return False

    def _register_handlers(self):
        if not self.event_processor or not self.config:
            return
        if self.syslog_poller:
            self.syslog_poller.clear_handlers()
        if self.docker_events_poller:
            self.docker_events_poller.clear_handlers()

        for event_type in self.config.monitor_events:
            handler = self.event_processor.get_handler(event_type)
            if not handler:
                print(f"✗ 未知事件类型: {event_type}")
                continue
            if self.syslog_poller:
                self.syslog_poller.add_handler(event_type, handler)
            if self.docker_events_poller:
                self.docker_events_poller.add_handler(event_type, handler)
            print(f"✓ 注册事件处理器: {event_type}")

    def reload_config(self) -> None:
        from web.ui_app import CONFIG_FILE
        if not self.config:
            return
        ok = self.config.reload_from_file(CONFIG_FILE)
        if not ok:
            return
        has_webhook = any([
            self.config.wechat_webhook_url,
            self.config.dingtalk_webhook_url,
            self.config.feishu_webhook_url,
            self.config.bark_url,
            self.config.pushplus_params,
            getattr(self.config, "magic_push_params", "") or "",
            getattr(self.config, "smtp_params", "") or "",
        ])
        if self.notifier is None and has_webhook:
            print("配置已保存并热加载：检测到新配置的推送渠道，正在启动监控...")
            self.notifier = UnifiedNotifier(self.config)
            self.event_processor = EventProcessor(self.notifier, self.config)
            self._build_data_source_pollers()
            self._build_docker_poller()
            self._register_handlers()
            self._start_all_pollers()
            if self.logger:
                self.logger.info("热加载完成：监控已启动")
        elif self.notifier is not None:
            self.notifier.reload_config()
            self._stop_all_pollers()
            self._build_data_source_pollers()
            self._build_docker_poller()
            self._register_handlers()
            self._start_all_pollers()
            if self.logger:
                self.logger.info("热加载完成：监控配置已更新")

        if self.event_processor and getattr(self.event_processor, "log_storage", None):
            try:
                d = int(getattr(self.config, "log_retention_days", 30))
                self.event_processor.log_storage.days_to_keep = max(1, d)
            except (TypeError, ValueError):
                pass

    def _signal_handler(self, signum, frame):
        print(f"\n接收到信号 {signum}，准备关闭应用...")
        self.running = False

    def _start_all_pollers(self):
        if self.syslog_poller:
            self.syslog_poller.start()
        if self.docker_events_poller:
            self.docker_events_poller.start()


    def _stop_all_pollers(self):
        if self.syslog_poller:
            self.syslog_poller.stop()
            self.syslog_poller = None
        if self.docker_events_poller:
            self.docker_events_poller.stop()
            self.docker_events_poller = None


    def run(self):
        try:
            if not self.initialize():
                if self.notifier:
                    self.notifier.send_system_notification(
                        'APP_ERROR',
                        '应用初始化失败: 未知错误',
                        {'hostname': socket.gethostname(), 'version': '1.0.0'}
                    )
                sys.exit(1)

            self.running = True

            try:
                ui_thread = start_ui_server_in_background(on_config_saved=self.reload_config)
                print(f"配置 UI 已启动，线程: {ui_thread.name}")
            except Exception as e:
                print(f"配置 UI 启动失败: {e}")

            if not self.notifier:
                print("")
                print("  >>> 请访问 Web 配置页面完成推送渠道配置 （保存后自动生效，无需重启）  <<<")
                print("")
            else:
                self._start_notification_health_monitor()
                self._system_patrol_thread = start_system_patrol_thread(self)
                self.notifier.send_system_notification(
                    'APP_START',
                    'LinuxMessageBot 已启动，开始监控系统事件',
                    {'hostname': socket.gethostname(), 'version': '1.0.0'}
                )
                self._start_all_pollers()

            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)

            loop_count = 0
            while self.running:
                loop_count += 1
                if loop_count % 60 == 0 and self.notifier:
                    try:
                        self.notifier.flush_dnd_buffer_if_needed()
                    except Exception as e:
                        if self.logger:
                            self.logger.warning("勿扰汇总检查异常: %s", e)
                time.sleep(1)

        except KeyboardInterrupt:
            print("\n接收到中断信号...")
        except Exception as e:
            print(f"运行时错误: {e}")
            traceback.print_exc()
        finally:
            self.shutdown()

    def _start_notification_health_monitor(self):
        if not self.notifier or not self.config:
            return
        if not self.config.notification_restart_enabled:
            return

        self.notification_health_thread = threading.Thread(
            target=self._notification_health_loop,
            name="NotificationHealthMonitor",
            daemon=True
        )
        self.notification_health_thread.start()

    def _notification_health_loop(self):
        check_interval = 60
        while self.running:
            try:
                if not self.notifier or not self.config:
                    time.sleep(check_interval)
                    continue
                health = self.notifier.get_delivery_health()
                active_platforms = health.get('active_platforms', {})
                if not any(active_platforms.values()):
                    time.sleep(check_interval)
                    continue

                last_attempt = health.get('last_attempt_time')
                if last_attempt is None:
                    time.sleep(check_interval)
                    continue

                consecutive_failures = health.get('consecutive_failures', 0)
                first_failure_time = health.get('first_failure_time')

                if consecutive_failures >= self.config.notification_restart_consecutive_failures and first_failure_time:
                    failure_duration = time.time() - first_failure_time
                    if failure_duration >= self.config.notification_restart_window:
                        if self._should_throttle_notification_restart():
                            time.sleep(check_interval)
                            continue

                        reason = (
                            f"通知连续失败 {consecutive_failures} 次，持续 {failure_duration:.0f} 秒"
                        )
                        self._trigger_app_restart(reason)
                        return
            except Exception as e:
                if self.logger:
                    self.logger.error(f"通知健康监控出错: {e}", exc_info=True)
            time.sleep(check_interval)

    def _should_throttle_notification_restart(self) -> bool:
        if not self.config:
            return False
        cooldown = self.config.notification_restart_cooldown
        if cooldown <= 0:
            return False

        marker = Path("/tmp/notification_restart.lock")
        now = time.time()
        try:
            if marker.exists():
                last_ts = float(marker.read_text().strip() or "0")
                if now - last_ts < cooldown:
                    if self.logger:
                        self.logger.warning(f"通知重启冷却中，距离上次 {now - last_ts:.0f} 秒")
                    return True
            marker.write_text(str(now))
        except Exception as e:
            if self.logger:
                self.logger.error(f"写入通知重启标记失败: {e}")
        return False

    def _trigger_app_restart(self, reason: str):
        if self.logger:
            self.logger.critical(f"触发应用重启，原因: {reason}")
        else:
            print(f"触发应用重启，原因: {reason}")

        try:
            restart_log = Path("/tmp/restart_reason.log")
            with open(restart_log, "a") as f:
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                f.write(f"{timestamp} - {reason}\n")
        except Exception:
            pass

        try:
            if self.notifier:
                self.notifier.send_system_notification(
                    'APP_ERROR',
                    f'触发自动重启: {reason}',
                    {'hostname': socket.gethostname(), 'version': '1.0.0'}
                )
        except Exception:
            pass

        time.sleep(2)
        self._exit_code = 1
        self.running = False

    def shutdown(self):
        print("\n正在关闭应用...")

        if self.notifier:
            self.notifier.send_system_notification(
                'APP_STOP',
                'LinuxMessageBot 已停止，监控服务暂停',
                {'hostname': socket.gethostname(), 'version': '1.0.0'}
            )

        self._stop_all_pollers()

        cleanup_flag = getattr(self.logger, 'cleanup_stop_flag', None) if self.logger else None
        if cleanup_flag is not None:
            print("正在停止运行日志清理线程...")
            cleanup_flag.set()

        if self.event_processor and hasattr(self.event_processor, 'log_storage'):
            print("正在停止原始推送日志清理线程...")
            self.event_processor.log_storage.stop_cleanup_thread()

        if self.notifier:
            stats = self.notifier.get_stats()
            print("\n运行统计:")
            print(f"  发送请求: {stats.get('request_count', 0)}")
            print(f"  成功通知: {stats.get('success_count', 0)}")
            print(f"  失败通知: {stats.get('error_count', 0)}")

            success_rate = stats.get('success_rate', '0.0%')
            print(f"  成功率: {success_rate}")

            self.notifier.close()

        print(f"应用已关闭 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def main():
    app = Application()
    app.run()
    raise SystemExit(getattr(app, "_exit_code", 0))

if __name__ == "__main__":
    main()
