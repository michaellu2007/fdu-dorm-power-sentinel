# -*- coding: utf-8 -*-
"""一键自测：所有模块的单测 + 一条模拟整条流水线的集成测试。

运行: python selftest.py
（集成测试使用注入的假电量数据，不访问网络、不发邮件、不真关机。）
"""

import os
import tempfile
from datetime import datetime, timedelta

import main
from storage import StateStore


def test_integration():
    """注入假 FduClient，走一遍 run_check 的完整状态机。"""
    now = datetime.now()

    # 构造 48h 历史消耗：20 度 -> 14 度（速率 0.25 度/小时）
    fd, state_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(state_path)
    state = StateStore(state_path)
    state.append_sample(20.0, "2026-10-18 00:00:00", now=now - timedelta(hours=48))
    state.append_sample(14.0, "2026-10-18 00:00:00", now=now - timedelta(hours=24))
    state.save()

    cfg = {
        "uis": {"student_id": "test", "password": "test"},
        "smtp": {"host": "invalid", "port": 465, "ssl": True,
                 "user": "x", "password": "x", "to": ["x"]},
        "alert": {"alert_window_hours": 12, "cold_start_threshold_kwh": 5.0},
        "shutdown": {"enabled": True, "night_start": "00:00", "night_end": "07:00",
                     "day_trigger_minutes": 10, "night_trigger_minutes": 40,
                     "day_countdown_seconds": 60, "night_countdown_seconds": 600},
        "proxy": "",
    }

    class FakeClient:
        payload = {"campus": "邯郸", "building": "15号楼", "room": "1501-1",
                   "surplus": 8.0, "used": 0, "total": 0,
                   "update_time": "2026-10-19 00:00:00"}

        def __init__(self, *a, **k):
            pass

        def get_electricity(self):
            return dict(FakeClient.payload)

    orig_client, orig_state = main.FduClient, main.STATE_PATH
    main.FduClient, main.STATE_PATH = FakeClient, state_path
    try:
        # 1) 剩余 8 度 / 0.25 度每小时 = 32h > 12h：不告警
        r = main.run_check(cfg, state, dry_run=True)
        assert r["pred_hours"] > 12 and not state.alerting

        # 1.5) 安全区自适应跳查：刚查过且预测 >24h → 本轮不联网
        calls = {"n": 0}

        class CountingClient(FakeClient):
            def get_electricity(self):
                calls["n"] += 1
                return super().get_electricity()

        orig2, main.FduClient = main.FduClient, CountingClient
        r = main.run_check(cfg, state, dry_run=True)  # 不 force：应命中跳查
        main.FduClient = orig2
        assert calls["n"] == 0, "安全区且刚查过，应跳过查询"
        assert r["pred_hours"] > 24

        # 2) 剩余 2 度 → 8h < 12h：触发告警
        FakeClient.payload["surplus"] = 2.0
        r = main.run_check(cfg, state, dry_run=True, force_query=True)
        assert r["pred_hours"] < 12 and state.alerting

        # 2.4) 唯一收件人退订：告警挂起、不发信、不崩溃
        state.opt_out("x")
        FakeClient.payload["surplus"] = 1.0
        r = main.run_check(cfg, state, dry_run=True, force_query=True)
        assert state.alerting
        state.opt_in("x")

        # 2.5) 静默期（充过电）：低电量也不发告警、不触发关机
        state.set_snooze(24)
        FakeClient.payload["surplus"] = 0.02
        r = main.run_check(cfg, state, dry_run=True, force_query=True)
        assert state.snoozed()
        assert not state.shutdown_issued_today(now.strftime("%Y-%m-%d"))
        state.data["snooze_until"] = None  # 解除静默

        # 3) 剩余 0.02 度 → <10 分钟：白天触发关机(dry-run)
        FakeClient.payload["surplus"] = 0.02
        r = main.run_check(cfg, state, dry_run=True, force_query=True)
        assert r["pred_hours"] * 60 <= 10
        assert state.shutdown_issued_today(now.strftime("%Y-%m-%d"))

        # 4) 充值到 50 度：速率无法估计（充值后仅一个点）→ 告警解除
        FakeClient.payload["surplus"] = 50.0
        r = main.run_check(cfg, state, dry_run=True, force_query=True)
        assert not state.alerting, "充值后应解除告警"
    finally:
        main.FduClient, main.STATE_PATH = orig_client, orig_state
        os.unlink(state_path)
    print("integration test OK")


if __name__ == "__main__":
    import estimator
    estimator._self_test()
    import fdu_client
    # fdu_client 的自测在其 __main__ 里，这里重跑正则部分
    html = ('<form id="logon" action="https://x/cas?a=1"><input id="ticket" value="T"/></form>')
    assert fdu_client.FduClient._parse_ticket_form(html)
    import shutdowner
    shutdowner._self_test()

    # storage 静默/节流往返
    import tempfile, os
    fd, sp = tempfile.mkstemp(suffix=".json"); os.close(fd); os.unlink(sp)
    st = StateStore(sp)
    now = datetime.now()
    assert not st.snoozed(now)
    st.set_snooze(24, now=now)
    assert st.snoozed(now)
    assert not st.snoozed(now + timedelta(hours=25))
    st.mark_alert_email(now=now)
    st.save()
    st2 = StateStore(sp)
    assert st2.snoozed(now) and not st2.should_send_alert(now, 12)
    assert not st.should_send_alert(now + timedelta(hours=11), 12)
    assert st.should_send_alert(now + timedelta(hours=13), 12)
    # 逐人退订/恢复（大小写不敏感）
    st.opt_out("A@X.com")
    assert st.is_opted_out("a@x.com")
    assert st.active_recipients(["a@x.com", "b@x.com"]) == ["b@x.com"]
    st.opt_in("a@x.com")
    assert st.active_recipients(["a@x.com", "b@x.com"]) == ["a@x.com", "b@x.com"]
    if os.path.exists(sp):
        os.unlink(sp)
    print("storage snooze/throttle/optout OK")

    # feedback 纯函数：token 映射与个性化链接
    import feedback
    roster = ["21307130001@m.fudan.edu.cn", "21307130002@m.fudan.edu.cn"]
    assert feedback._token_to_addr("21307130002", roster) == "21307130002@m.fudan.edu.cn"
    assert feedback._token_to_addr("nobody", roster) is None
    links = feedback.person_links({"user": "me@163.com"}, "21307130002@m.fudan.edu.cn")
    assert links["unsub"].endswith("subject=UNSUB-21307130002")
    assert links["resub"].endswith("subject=RESUB-21307130002")
    assert "subject=REFILLED" in links["refill"]
    print("feedback token/links OK")

    test_integration()
    print("ALL SELF-TESTS PASSED")
