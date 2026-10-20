# -*- coding: utf-8 -*-
"""低电量自动关机。

触发条件（由 main.py 判定后调用）：
- 白天：预计剩余电量只够 10 分钟 -> shutdown /s /t 60
- 凌晨（默认 00:00-07:00，可配）：提前 30 分钟，即剩 40 分钟时就关 -> shutdown /s /t 600
- 双击 cancel_shutdown.bat 可取消当次关机，并写入标记文件，脚本当天不再重复发起。
"""

import logging
import os
import subprocess
from datetime import datetime

log = logging.getLogger("fdu")

CANCEL_MARKER = "shutdown_cancelled.txt"


def parse_hhmm(s, default_minutes):
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return default_minutes


def is_night(now, night_start, night_end):
    """凌晨时段判断（支持跨零点，如 00:00-07:00）。"""
    cur = now.hour * 60 + now.minute
    start = parse_hhmm(night_start, 0)
    end = parse_hhmm(night_end, 7 * 60)
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def cancelled_today(base_dir, now=None):
    """cancel_shutdown.bat 是否在今天取消过关机。"""
    path = os.path.join(base_dir, CANCEL_MARKER)
    if not os.path.exists(path):
        return False
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(path))
        return mtime.date() == (now or datetime.now()).date()
    except OSError:
        return False


def issue_shutdown(countdown_seconds, comment, dry_run=False):
    cmd = ["shutdown", "/s", "/t", str(int(countdown_seconds)), "/c", comment]
    if dry_run:
        log.info("[dry-run] 跳过关机: %s", " ".join(cmd))
        return True
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            log.warning("已发起关机：%d 秒后关机（运行 cancel_shutdown.bat 可取消）",
                        countdown_seconds)
            return True
        log.error("关机命令失败: %s %s", r.stdout, r.stderr)
        return False
    except Exception as e:
        log.error("关机命令异常: %s", e)
        return False


def decide(now, pred_minutes, cfg):
    """根据当前时间和预测可用分钟数，返回 (应否关机, 倒计时秒数, 阈值分钟, 是否夜间)。"""
    if pred_minutes is None:
        return False, 0, 0, False
    night = is_night(now, cfg.get("night_start", "00:00"),
                     cfg.get("night_end", "07:00"))
    if night:
        threshold = float(cfg.get("night_trigger_minutes", 40))
        countdown = int(cfg.get("night_countdown_seconds", 600))
    else:
        threshold = float(cfg.get("day_trigger_minutes", 10))
        countdown = int(cfg.get("day_countdown_seconds", 60))
    return pred_minutes <= threshold, countdown, threshold, night


# ---------------- 自测 ----------------

def _self_test():
    from datetime import datetime
    cfg = {"night_start": "00:00", "night_end": "07:00",
           "day_trigger_minutes": 10, "night_trigger_minutes": 40,
           "day_countdown_seconds": 60, "night_countdown_seconds": 600}

    assert is_night(datetime(2026, 10, 19, 3, 0), "00:00", "07:00")
    assert not is_night(datetime(2026, 10, 19, 12, 0), "00:00", "07:00")
    assert is_night(datetime(2026, 10, 19, 23, 30), "22:00", "07:00")  # 跨零点
    assert not is_night(datetime(2026, 10, 19, 8, 0), "22:00", "07:00")

    go, cd, th, night = decide(datetime(2026, 10, 19, 14, 0), 9.9, cfg)
    assert go and cd == 60 and th == 10 and not night
    go, cd, th, night = decide(datetime(2026, 10, 19, 14, 0), 10.5, cfg)
    assert not go
    go, cd, th, night = decide(datetime(2026, 10, 19, 3, 0), 39.9, cfg)
    assert go and cd == 600 and th == 40 and night
    go, _, _, _ = decide(datetime(2026, 10, 19, 3, 0), 40.5, cfg)
    assert not go
    go, _, _, _ = decide(datetime(2026, 10, 19, 3, 0), None, cfg)
    assert not go

    print("shutdowner self-test OK")


if __name__ == "__main__":
    _self_test()
