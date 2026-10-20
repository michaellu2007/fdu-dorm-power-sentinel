# -*- coding: utf-8 -*-
"""state.json 的读写：历史电量记录（全量保留）+ 告警/关机状态。"""

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta

log = logging.getLogger("fdu")

HISTORY_CAP = 20000          # 防止无限膨胀的上限（30 分钟一次也够记 10 年）
RETENTION_DAYS = 120         # 超过 120 天的样本才滚动清理


class StateStore:
    def __init__(self, path):
        self.path = path
        self.data = {
            "history": [],            # [{ts: iso, surplus: float, update_time: str}, ...]
            "alerting": False,        # 「半天内用完」告警中
            "last_alert_email_ts": None,
            "consecutive_failures": 0,
            "last_outage_email_ts": None,
            "shutdown_issued_date": None,   # 已发起过关机的日期 (YYYY-MM-DD)
        }
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except Exception as e:
                log.warning("state.json 读取失败，重新开始: %s", e)

    def append_sample(self, surplus, meter_update_time, now=None):
        """记录一次电量读数（同一次电表更新期内不重复堆叠，只更新时间戳？——否，
        每次查询都记录，速率估算时按需取窗口）。"""
        now = now or datetime.now()
        self.data.setdefault("history", []).append({
            "ts": now.replace(microsecond=0).isoformat(),
            "surplus": round(float(surplus), 3),
            "update_time": meter_update_time,
        })
        hist = self.data["history"]
        cutoff = (now - timedelta(days=RETENTION_DAYS)).isoformat()
        if len(hist) > HISTORY_CAP or (hist and hist[0]["ts"] < cutoff):
            self.data["history"] = [s for s in hist if s["ts"] >= cutoff][-HISTORY_CAP:]

    # --- 逐人退订（每人可自助选择不收） ---

    @staticmethod
    def _norm(addr):
        return str(addr).strip().lower()

    def opt_out(self, addr):
        lst = self.data.setdefault("optout", [])
        a = self._norm(addr)
        if a not in lst:
            lst.append(a)

    def opt_in(self, addr):
        a = self._norm(addr)
        self.data["optout"] = [x for x in self.data.get("optout", []) if x != a]

    def is_opted_out(self, addr):
        return self._norm(addr) in self.data.get("optout", [])

    def active_recipients(self, roster):
        """名册里过滤掉已退订的人。"""
        return [a for a in roster if not self.is_opted_out(a)]

    def optout_list(self):
        return list(self.data.get("optout", []))

    # --- 充值静默（snooze）与告警节流 ---

    def set_snooze(self, hours, now=None):
        self.data["snooze_until"] = ((now or datetime.now())
                                     + timedelta(hours=hours)).replace(
                                         microsecond=0).isoformat()

    def snoozed(self, now=None):
        ts = self.data.get("snooze_until")
        if not ts:
            return False
        try:
            until = datetime.fromisoformat(ts)
        except ValueError:
            return False
        return (now or datetime.now()) < until

    def snooze_until_str(self):
        return str(self.data.get("snooze_until") or "")

    def mark_alert_email(self, now=None):
        self.data["last_alert_email_ts"] = (now or datetime.now()).replace(
            microsecond=0).isoformat()

    def should_send_alert(self, now=None, min_gap_hours=12):
        """告警重发节流：首封之后的重发至少间隔 min_gap_hours 小时。"""
        ts = self.data.get("last_alert_email_ts")
        if not ts:
            return True
        try:
            last = datetime.fromisoformat(ts)
        except ValueError:
            return True
        return (now or datetime.now()) - last >= timedelta(hours=min_gap_hours)

    def history(self):
        return self.data.get("history", [])

    # --- 日用电历史（数据中心 xlsx 导入，供估算器做种子速率） ---

    def set_daily_usage(self, items):
        self.data["daily_usage"] = items

    def daily_usage(self):
        return self.data.get("daily_usage", [])

    # --- 告警状态 ---

    @property
    def alerting(self):
        return bool(self.data.get("alerting"))

    @alerting.setter
    def alerting(self, v):
        self.data["alerting"] = bool(v)

    # --- 连续失败 / 停摆通知 ---

    def note_failure(self):
        self.data["consecutive_failures"] = int(self.data.get("consecutive_failures", 0)) + 1
        return self.data["consecutive_failures"]

    def note_success(self):
        self.data["consecutive_failures"] = 0

    def mark_outage_email(self, now=None):
        self.data["last_outage_email_ts"] = (now or datetime.now()).replace(
            microsecond=0).isoformat()

    def should_send_outage_email(self, now=None, min_gap_hours=24):
        ts = self.data.get("last_outage_email_ts")
        if not ts:
            return True
        try:
            last = datetime.fromisoformat(ts)
        except ValueError:
            return True
        return (now or datetime.now()) - last >= timedelta(hours=min_gap_hours)

    def consecutive_failures(self):
        return int(self.data.get("consecutive_failures", 0))

    # --- 关机标记 ---

    def shutdown_issued_today(self, today_str):
        return self.data.get("shutdown_issued_date") == today_str

    def mark_shutdown_issued(self, today_str):
        self.data["shutdown_issued_date"] = today_str

    def clear_shutdown_marks_if_recovered(self, pred_minutes, day_trigger, night_trigger,
                                          threshold_minutes):
        """预测可用时长已远超触发阈值（一般=充值了）时，清掉当日关机标记。"""
        if pred_minutes is None:
            return
        if pred_minutes > max(day_trigger, night_trigger) * 3:
            if self.data.get("shutdown_issued_date"):
                self.data["shutdown_issued_date"] = None

    def save(self):
        d = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
