"""
系统巡检：按可配置间隔采集 CPU/内存，周期结束时计算平均值，
若超过阈值则推送告警，恢复后推送恢复通知。
磁盘直接检查当前使用率，超阈值即告警。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import psutil
except ImportError:
    psutil = None

_STATE_FILENAME = "system_patrol_state.json"


def _state_path(cursor_dir: str) -> Path:
    p = Path(cursor_dir or "./data/cursor")
    p.mkdir(parents=True, exist_ok=True)
    return p / _STATE_FILENAME


def _load_state(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            obj = json.loads(path.read_text(encoding="utf-8") or "{}")
            if isinstance(obj, dict):
                return obj
    except Exception:
        pass
    return {}


def _save_state(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logging.getLogger(__name__).warning("写入巡检状态失败: %s", e)


def _push(app: Any, event_type: str, event_data: dict) -> bool:
    try:
        from .models import JournalEntry
        entry = JournalEntry(
            cursor=f"patrol:{event_type}:{time.time():.0f}",
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            hostname="",
            syslog_identifier=event_type,
            message=event_data.get("message", ""),
            priority=5 if "ALARM" in event_type else 6,
            pid=0,
            raw_data=json.dumps(event_data, ensure_ascii=False),
            original_line="",
        )
        return bool(app.notifier.send_notification(
            event_type=event_type,
            event_data=event_data,
            raw_log=entry.raw_data,
            timestamp=entry.timestamp,
        ))
    except Exception as e:
        logging.getLogger(__name__).error("巡检推送异常: %s", e)
        return False


def _sample_cpu() -> float:
    try:
        return psutil.cpu_percent(interval=1.0)
    except Exception:
        return -1


def _sample_mem() -> float:
    try:
        return psutil.virtual_memory().percent
    except Exception:
        return -1


def _sample_disk() -> float:
    try:
        du = psutil.disk_usage("/")
        return 100.0 * (du.used / du.total) if du.total > 0 else -1
    except Exception:
        return -1


def patrol_worker_loop(app: Any) -> None:
    log = logging.getLogger(__name__)
    log.info("系统巡检（阈值告警）线程已启动")

    while app.running:
        try:
            cfg = app.config
            if not cfg or not getattr(cfg, "system_patrol_enabled", False) or not app.notifier:
                time.sleep(30)
                continue

            sp = _state_path(getattr(cfg, "cursor_dir", "./data/cursor") or "./data/cursor")
            state = _load_state(sp)

            # ---- CPU ----
            cpu_th = getattr(cfg, "patrol_cpu_threshold", 90) or 90
            cpu_int = getattr(cfg, "patrol_cpu_interval", 1) or 1
            cpu_prd = getattr(cfg, "patrol_cpu_period", 60) or 60
            cpu_points = state.setdefault("cpu_points", [])
            cpu_last_alarm = state.setdefault("cpu_last_alarm", False)

            # ---- MEM ----
            mem_th = getattr(cfg, "patrol_mem_threshold", 90) or 90
            mem_int = getattr(cfg, "patrol_mem_interval", 1) or 1
            mem_prd = getattr(cfg, "patrol_mem_period", 60) or 60
            mem_points = state.setdefault("mem_points", [])
            mem_last_alarm = state.setdefault("mem_last_alarm", False)

            # ---- DISK ----
            disk_th = getattr(cfg, "patrol_disk_threshold", 90) or 90
            disk_last_alarm = state.setdefault("disk_last_alarm", False)

            now = time.time()

            # CPU 采样
            cpu_next = state.get("cpu_next_sample", 0.0)
            if now >= cpu_next:
                val = _sample_cpu()
                if val >= 0:
                    cpu_points.append((now, val))
                    state["cpu_next_sample"] = now + max(10, cpu_int * 60)
                else:
                    state["cpu_next_sample"] = now + 60

            # MEM 采样
            mem_next = state.get("mem_next_sample", 0.0)
            if now >= mem_next:
                val = _sample_mem()
                if val >= 0:
                    mem_points.append((now, val))
                    state["mem_next_sample"] = now + max(10, mem_int * 60)
                else:
                    state["mem_next_sample"] = now + 60

            # CPU 周期评估
            cpu_eval_next = state.get("cpu_eval_next", 0.0)
            if cpu_points and now >= cpu_eval_next:
                cutoff = now - max(60, cpu_prd * 60)
                valid = [v for t, v in cpu_points if t >= cutoff]
                cpu_points[:] = [(t, v) for t, v in cpu_points if t >= cutoff]
                if valid:
                    avg = sum(valid) / len(valid)
                    if avg > cpu_th and not cpu_last_alarm:
                        _push(app, "PATROL_CPU_ALARM", {
                            "threshold": cpu_th,
                            "value": f"{avg:.1f}",
                            "message": f"CPU 使用率 {avg:.1f}%（阈值 {cpu_th}%）"
                        })
                        state["cpu_last_alarm"] = True
                    elif avg <= cpu_th and cpu_last_alarm:
                        _push(app, "PATROL_CPU_RESTORED", {
                            "threshold": cpu_th,
                            "value": f"{avg:.1f}",
                            "message": f"CPU 使用率已恢复至 {avg:.1f}%（阈值 {cpu_th}%）"
                        })
                        state["cpu_last_alarm"] = False
                state["cpu_eval_next"] = now + max(10, cpu_prd * 60)

            # MEM 周期评估
            mem_eval_next = state.get("mem_eval_next", 0.0)
            if mem_points and now >= mem_eval_next:
                cutoff = now - max(60, mem_prd * 60)
                valid = [v for t, v in mem_points if t >= cutoff]
                mem_points[:] = [(t, v) for t, v in mem_points if t >= cutoff]
                if valid:
                    avg = sum(valid) / len(valid)
                    if avg > mem_th and not mem_last_alarm:
                        _push(app, "PATROL_MEM_ALARM", {
                            "threshold": mem_th,
                            "value": f"{avg:.1f}",
                            "message": f"内存使用率 {avg:.1f}%（阈值 {mem_th}%）"
                        })
                        state["mem_last_alarm"] = True
                    elif avg <= mem_th and mem_last_alarm:
                        _push(app, "PATROL_MEM_RESTORED", {
                            "threshold": mem_th,
                            "value": f"{avg:.1f}",
                            "message": f"内存使用率已恢复至 {avg:.1f}%（阈值 {mem_th}%）"
                        })
                        state["mem_last_alarm"] = False
                state["mem_eval_next"] = now + max(10, mem_prd * 60)

            # 磁盘直接检查
            disk_next = state.get("disk_next_check", 0.0)
            if now >= disk_next:
                val = _sample_disk()
                if val >= 0:
                    if val > disk_th and not disk_last_alarm:
                        _push(app, "PATROL_DISK_ALARM", {
                            "threshold": disk_th,
                            "value": f"{val:.1f}",
                            "message": f"磁盘使用率 {val:.1f}%（阈值 {disk_th}%）"
                        })
                        state["disk_last_alarm"] = True
                    elif val <= disk_th and disk_last_alarm:
                        _push(app, "PATROL_DISK_RESTORED", {
                            "threshold": disk_th,
                            "value": f"{val:.1f}",
                            "message": f"磁盘使用率已恢复至 {val:.1f}%（阈值 {disk_th}%）"
                        })
                        state["disk_last_alarm"] = False
                state["disk_next_check"] = now + 300

            _save_state(sp, state)

        except Exception as e:
            log.error("巡检异常: %s", e, exc_info=True)

        time.sleep(30)


def start_system_patrol_thread(app: Any) -> Optional[threading.Thread]:
    t = threading.Thread(target=patrol_worker_loop, args=(app,), name="SystemPatrol", daemon=True)
    t.start()
    return t
