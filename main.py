# -*- coding: utf-8 -*-
"""复旦寝室电量监控：半天告警邮件 + 低电量夜间自动关机。

用法：
  python main.py --once            # 查一次（Windows 计划任务用）
  python main.py --loop            # 常驻循环（调试用）
  python main.py --dry-run --once  # 查一次，只打日志：不发邮件、不关机
  python main.py --test-email      # 发一封测试邮件
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime

import estimator
import feedback
import notifier
import shutdowner
from fdu_client import (BadCredentialsError, CaptchaRequiredError,
                        FduClient, FduError, TwoFactorRequiredError)
from storage import StateStore

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
COOKIE_PATH = os.path.join(BASE_DIR, "cookies.txt")
LOG_PATH = os.path.join(BASE_DIR, "monitor.log")

log = logging.getLogger("fdu")

# 告警期间在单次 --once 内转为快速复查，保证 10 分钟级关机来得及触发
FAST_CHECK_MINUTES = 10
FAST_CHECK_WINDOW_MINUTES = 25


_logging_ready = False


def setup_logging():
    global _logging_ready
    if _logging_ready:
        return
    _logging_ready = True
    try:
        if sys.stdout is not None:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stdout is not None:  # pythonw（计划任务）下无控制台，只写文件
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        log.error("找不到 config.json，请先复制 config.example.json 为 config.json 并填写")
        return None
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    sid = str(cfg.get("uis", {}).get("student_id", ""))
    pwd = str(cfg.get("uis", {}).get("password", ""))
    if not sid or "YOUR_" in sid or not pwd or "YOUR_" in pwd:
        log.warning("config.json 里的学号/密码还是占位符，请填写后再运行")
        return None
    return cfg


def fmt_hours(h):
    if h is None:
        return "未知（历史数据不足）"
    if h < 1:
        return "约 %d 分钟" % max(1, round(h * 60))
    return "约 %.1f 小时" % h


def room_line(info):
    return "%s %s %s" % (info.get("campus") or "", info.get("building") or "",
                         info.get("room") or "")


def _footer_text(links):
    return ("\n———————\n退订此类提醒（只影响你自己）：%s\n误退订了？重新订阅：%s\n"
            % (links["unsub"], links["resub"]))


def _footer_html(links):
    return ("<p style=\"color:#888;font-size:12px\">"
            "<a href=\"%s\">退订此类提醒</a> · <a href=\"%s\">重新订阅</a>"
            "（只影响你自己的收件）</p>" % (links["unsub"], links["resub"]))


def send_to_active(cfg, state, subject, make_body, make_html=None, dry_run=False):
    """给所有未退订的收件人逐一个性化发送。返回是否全部成功。"""
    smtp = cfg["smtp"]
    roster = smtp.get("to") or ([smtp.get("user")] if smtp.get("user") else [])
    recipients = state.active_recipients(roster)
    if not recipients:
        log.info("所有收件人均已退订，跳过发送: %s", subject)
        return True
    ok = True
    for addr in recipients:
        token = str(addr).split("@")[0]
        links = feedback.person_links(smtp, addr)
        ok = notifier.send_email(smtp, subject, make_body(token, links),
                                 dry_run=dry_run,
                                 html=make_html(token, links) if make_html else None,
                                 to=[addr]) and ok
    return ok


def _cheap_prediction(cfg, state, now=None):
    """不联网的快速预测：最近一次余额 + 走表衰减 + 日数据速率。"""
    info = state.data.get("last_info")
    if not info:
        return None
    _, h = estimator.predict(state.history(), info.get("surplus"),
                             daily_usage=state.daily_usage(), now=now)
    return h


def run_check(cfg, state, dry_run, allow_repeat_email=True, force_query=False):
    """执行一次完整检查。返回 {'pred_hours': float|None}。"""
    # ---- 安全区跳查：预测还很充裕且刚查过时，本轮不联网（数据反正一天一更） ----
    if not force_query:
        h_cheap = _cheap_prediction(cfg, state)
        gap = float(cfg.get("alert", {}).get("safe_skip_minutes", 120))
        last = state.data.get("last_query_ts")
        if h_cheap is not None and h_cheap > 24 and gap > 0 and last:
            try:
                dt_min = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 60.0
            except ValueError:
                dt_min = gap + 1
            if dt_min < gap:
                log.info("安全区：预计可用 %s，距上次查询 %.0f 分钟（<%.0f），跳过本轮查询",
                         fmt_hours(h_cheap), dt_min, gap)
                return {"pred_hours": h_cheap}

    uis = cfg["uis"]
    client = FduClient(uis["student_id"], uis["password"],
                       cookie_path=COOKIE_PATH, proxy=cfg.get("proxy", ""))

    # ---- 查询 ----
    try:
        info = client.get_electricity()
    except (FduError, TwoFactorRequiredError, CaptchaRequiredError,
            BadCredentialsError) as e:
        n = state.note_failure()
        log.error("查询失败（连续第 %d 次）: %s", n, e)
        if n >= 3 and state.should_send_outage_email():
            send_to_active(
                cfg, state,
                "【监控停摆】寝室电量监控已连续失败 %d 次" % n,
                lambda token, links: (
                    "电量监控连续失败，告警邮件和自动关机当前不可用。\n"
                    "最近一次错误：%s\n\n"
                    "常见原因：改了 UIS 密码 / 登录触发验证码 / 账号开了两步验证 / 校园网。\n"
                    "请手动运行: python main.py --dry-run --once 查看日志定位。\n"
                    "若为两步验证，见 README 的「Cookie 降级模式」。\n%s" % (e, _footer_text(links))),
                lambda token, links: None, dry_run=dry_run)
            state.mark_outage_email()
        return {"pred_hours": None}
    except Exception as e:
        n = state.note_failure()
        log.error("查询出现未预期异常（连续第 %d 次）: %r", n, e)
        if n >= 3 and state.should_send_outage_email():
            send_to_active(
                cfg, state,
                "【监控停摆】寝室电量监控已连续失败 %d 次" % n,
                lambda token, links: (
                    "最近一次异常：%r\n请手动运行: python main.py --dry-run --once 定位。\n%s"
                    % (e, _footer_text(links))),
                lambda token, links: None, dry_run=dry_run)
            state.mark_outage_email()
        return {"pred_hours": None}

    # ---- 成功：记录样本 ----
    state.note_success()
    state.append_sample(info["surplus"], info.get("update_time"))
    state.data["last_info"] = info
    surplus = info["surplus"]

    rate, h = estimator.predict(state.history(), surplus,
                                daily_usage=state.daily_usage())
    now = datetime.now()
    state.data["last_query_ts"] = now.replace(microsecond=0).isoformat()
    p_rate = estimator.pessimistic_rate_kwh_per_hour(state.daily_usage(), now=now)
    p_h = estimator.decayed_hours_left(state.history(), surplus, p_rate, now=now) \
        if p_rate else None
    p_min = None if p_h is None else p_h * 60.0
    pred_min = None if h is None else h * 60.0
    log.info("查询成功: %s | 剩余 %.1f 度 | 电表更新 %s | 速率 %s | 预计可用 %s | 悲观 %s",
             room_line(info), surplus, info.get("update_time"),
             ("%.3f 度/小时" % rate) if rate else "未知",
             fmt_hours(h), fmt_hours(p_h))

    alert_cfg = cfg.get("alert", {})
    alert_window = float(alert_cfg.get("alert_window_hours", 12))
    cold_start = float(alert_cfg.get("cold_start_threshold_kwh", 5.0))
    repeat_gap = float(alert_cfg.get("repeat_interval_hours", 12))
    silent_start = str(alert_cfg.get("silent_start", "23:00"))
    silent_end = str(alert_cfg.get("silent_end", "08:00"))
    snooze_hours = float(alert_cfg.get("snooze_hours", 24))

    # ---- 反馈轮询：识别「我充好电了」/「退订」/「重新订阅」回信 ----
    feedback.poll_reply_snooze(cfg["smtp"], state, snooze_hours=snooze_hours,
                               roster=cfg["smtp"].get("to") or [], dry_run=dry_run)

    alert_now = (h is not None and h < alert_window) or \
                (h is None and surplus <= cold_start)
    night_silent = shutdowner.is_night(now, silent_start, silent_end)

    def _alert_email(repeat):
        title = "【充电提醒%s】寝室电量%s，请尽快充值" % (
            "·持续" if repeat else "", fmt_hours(h))
        reason = ("预计可用时长不足 %.0f 小时" % alert_window) if h is not None else (
            "历史数据不足，且剩余电量已达保守阈值 %.1f 度" % cold_start)

        def make_body(token, links):
            return ("寝室：%s\n剩余电量：%.1f 度（电表更新：%s）\n"
                    "近期用电速率：%s\n预计还能使用：%s（实时走表，随时间递减）\n"
                    "触发原因：%s\n\n"
                    "充好电后请二选一告诉本系统（电表余额一天才更新一次，"
                    "不反馈的话系统会误报约一天）：\n"
                    "  1. 点下面链接，发出去那封预写好的反馈邮件；\n"
                    "  2. 双击监控电脑上的 我充好电了.bat\n\n"
                    "反馈后静默 %.0f 小时，期间不再提醒、不触发自动关机。\n"
                    "反馈链接（手机点开即可，直接发送）：\n%s\n%s"
                    % (room_line(info), surplus, info.get("update_time") or "未知",
                       ("%.3f 度/小时" % rate) if rate else "未知", fmt_hours(h),
                       reason, snooze_hours, links["refill"], _footer_text(links)))

        def make_html(token, links):
            return ("<html><body>"
                    "<p>寝室：%s<br>剩余电量：<b>%.1f 度</b>"
                    "<br>预计还能使用：<b>%s</b>（实时走表）</p>"
                    "<p><a href=\"%s\" style=\"display:inline-block;padding:10px 18px;"
                    "background:#2e7d32;color:#ffffff;border-radius:6px;"
                    "text-decoration:none;font-size:15px\">"
                    "✅ 我充好电了（点此静默 24 小时）</a></p>%s"
                    "<p style=\"color:#666\">学校电表余额一天才更新一次，充完电请点上面按钮，"
                    "否则系统会误报约一天。重复提醒每 %.0f 小时一封；%s–%s 夜间静默。</p>"
                    "</body></html>"
                    % (room_line(info), surplus, fmt_hours(h), links["refill"],
                       _footer_html(links), repeat_gap, silent_start, silent_end))

        return send_to_active(cfg, state, title, make_body, make_html, dry_run=dry_run)

    # ---- 半天告警（12h 节流 + 夜间静默 + 充值静默） ----
    if state.snoozed(now):
        if not alert_now:
            log.info("静默期（至 %s）且电量已恢复，静默提前结束", state.snooze_until_str())
            state.data["snooze_until"] = None
        else:
            log.info("静默期（至 %s），跳过告警与关机判定", state.snooze_until_str())
    elif alert_now and not state.alerting:
        state.alerting = True
        if night_silent:
            log.info("夜间静默时段（%s-%s），首封告警邮件推迟", silent_start, silent_end)
        else:
            _alert_email(repeat=False)
            state.mark_alert_email(now)
    elif alert_now and state.alerting:
        if not night_silent and allow_repeat_email and \
                state.should_send_alert(now, repeat_gap):
            _alert_email(repeat=state.data.get("last_alert_email_ts") is not None)
            state.mark_alert_email(now)
    elif not alert_now and state.alerting:
        def _recovery_body(token, links):
            return ("寝室：%s\n剩余电量：%.1f 度\n预计还能使用：%s\n\n告警已解除。\n%s"
                    % (room_line(info), surplus, fmt_hours(h), _footer_text(links)))

        def _recovery_html(token, links):
            return ("<html><body><p>寝室：%s<br>剩余电量：<b>%.1f 度</b>"
                    "<br>预计还能使用：<b>%s</b></p><p>告警已解除。</p>%s</body></html>"
                    % (room_line(info), surplus, fmt_hours(h), _footer_html(links)))

        send_to_active(cfg, state, "【解除提醒】寝室电量已可支撑半天以上",
                       _recovery_body, _recovery_html, dry_run=dry_run)
        state.alerting = False
        log.info("告警已解除")

    # ---- 低电量自动关机（静默期不触发；关机通知不受夜间静默限制） ----
    # 关机看悲观口径：速率取 max(EWMA, 近7天最大单日)，避免一天一更数据下的漏保护
    sd_cfg = cfg.get("shutdown", {})
    if sd_cfg.get("enabled", True) and not state.snoozed(now):
        today = now.strftime("%Y-%m-%d")
        if p_min is not None:
            eval_min = p_min
            eval_cfg = dict(sd_cfg)
            eval_cfg["day_trigger_minutes"] = float(sd_cfg.get("p_day_trigger_minutes", 20))
            eval_cfg["night_trigger_minutes"] = float(
                sd_cfg.get("p_night_trigger_minutes", 60))
        else:
            eval_min, eval_cfg = pred_min, sd_cfg
        do_it, countdown, threshold, night = shutdowner.decide(now, eval_min, eval_cfg)
        # 白天悲观触发的护栏：数据一天一更容易陈旧，若平均口径还有 1 小时以上余量，
        # 说明大概率只是估计过悲观（人还在电脑前，邮件提醒就够），不执行关机
        if do_it and not night and p_min is not None and \
                (pred_min is None or pred_min > 60):
            log.warning("悲观口径触发白天关机，但平均口径还有 %s，判定为估计过悲观，"
                        "改为仅提醒不关机", fmt_hours(h))
            do_it = False
        if do_it and not state.shutdown_issued_today(today) and \
                not shutdowner.cancelled_today(BASE_DIR, now):
            when = "凌晨时段（提前 30 分钟保护）" if night else "白天时段"

            def _sd_body(token, links):
                return ("寝室：%s\n剩余电量：%.1f 度\n预计仅能支撑：%s"
                        "（低于%s阈值 %.0f 分钟，悲观口径）\n\n"
                        "电脑将在 %d 秒后自动关机，防止突然断电损伤硬盘/丢失数据。\n"
                        "如需取消：立即双击运行本目录下 cancel_shutdown.bat"
                        "（取消后今天不会再自动关机）。\n充值后本机制自动恢复待命。\n%s"
                        % (room_line(info), surplus, fmt_hours(h),
                           "夜间" if night else "白天", threshold, countdown,
                           _footer_text(links)))

            def _sd_html(token, links):
                return ("<html><body><p>寝室：%s<br>剩余电量：<b>%.1f 度</b><br>"
                        "预计仅能支撑：<b>%s</b>（低于%s阈值 %.0f 分钟，悲观口径）</p>"
                        "<p>电脑将在 <b>%d 秒</b>后自动关机。"
                        "如需取消请到监控电脑双击 cancel_shutdown.bat。</p>%s</body></html>"
                        % (room_line(info), surplus, fmt_hours(h),
                           "夜间" if night else "白天", threshold, countdown,
                           _footer_html(links)))

            sent = send_to_active(cfg, state,
                                  "【即将关机】寝室电量仅剩%s，电脑将自动关机" % fmt_hours(h),
                                  _sd_body, _sd_html, dry_run=dry_run)
            if not sent:
                log.warning("关机通知邮件发送失败，仍然继续关机")
            shutdowner.issue_shutdown(
                countdown,
                "dorm power low: ~%d min left" % max(0, round(pred_min or 0)),
                dry_run=dry_run)
            state.mark_shutdown_issued(today)
        elif not do_it:
            state.clear_shutdown_marks_if_recovered(
                pred_min, sd_cfg.get("day_trigger_minutes", 10),
                sd_cfg.get("night_trigger_minutes", 40), threshold)

    state.save()
    return {"pred_hours": h}


def once(cfg, dry_run, force_query=False):
    state = StateStore(STATE_PATH)
    result = run_check(cfg, state, dry_run, force_query=force_query)
    h = result.get("pred_hours")
    fast_below = float(cfg.get("alert", {}).get("fast_check_below_hours", 6))
    if h is None or h >= fast_below:
        return
    # 危险区（≤6h）：单次运行内加密复查（默认 10 分钟一次，最多约 25 分钟），
    # 让分钟级关机触发来得及；计划任务的 30 分钟节奏不受影响
    log.warning("预计电量 %s 内耗尽，进入快速复查（每 %d 分钟）",
                fmt_hours(h), FAST_CHECK_MINUTES)
    deadline = time.time() + FAST_CHECK_WINDOW_MINUTES * 60
    while time.time() < deadline:
        time.sleep(FAST_CHECK_MINUTES * 60)
        result = run_check(cfg, state, dry_run, allow_repeat_email=False,
                           force_query=force_query)
        h = result.get("pred_hours")
        if h is None or h >= fast_below:
            break


def loop(cfg, dry_run):
    interval_min = float(cfg.get("alert", {}).get("interval_minutes", 30))
    fast_below = float(cfg.get("alert", {}).get("fast_check_below_hours", 6))
    state = StateStore(STATE_PATH)
    while True:
        h = run_check(cfg, state, dry_run).get("pred_hours")
        sleep_min = interval_min
        if h is not None and h < fast_below:
            sleep_min = FAST_CHECK_MINUTES
        log.info("下次检查：%g 分钟后", sleep_min)
        time.sleep(sleep_min * 60)


def main_args(run_once=False, run_loop=False, test_email=False, dry_run=False,
              force_query=False):
    setup_logging()
    log.info("=== FDU 寝室电量监控启动 (mode=%s dry_run=%s) ===",
             "once" if run_once else ("loop" if run_loop else "test-email"),
             dry_run)
    cfg = load_config()
    if cfg is None:
        return 0

    if test_email:
        ok = send_to_active(
            cfg, StateStore(STATE_PATH),
            "【测试】寝室电量监控邮件通道正常",
            lambda token, links: ("这是一封测试邮件。收到即说明 SMTP 配置正确。\n%s"
                                  % _footer_text(links)),
            lambda token, links: (
                "<html><body><p>这是一封测试邮件。收到即说明 SMTP 配置正确。</p>%s</body></html>"
                % _footer_html(links)),
            dry_run=dry_run)
        log.info("测试邮件%s", "发送成功" if ok else "未发送/失败")
        return 0
    if run_once:
        once(cfg, dry_run, force_query=force_query)
    else:
        loop(cfg, dry_run)
    return 0


def main():
    parser = argparse.ArgumentParser(description="复旦寝室电量监控")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true", help="查一次（计划任务用）")
    g.add_argument("--loop", action="store_true", help="常驻循环（调试用）")
    g.add_argument("--test-email", action="store_true", help="发一封测试邮件")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打日志：不发邮件、不关机")
    args = parser.parse_args()
    return main_args(run_once=args.once, run_loop=args.loop,
                     test_email=args.test_email, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
