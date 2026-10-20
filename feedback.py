# -*- coding: utf-8 -*-
"""邮件反馈通道（全部通过 IMAP 识别回信主题，零人工干预）：

- REFILLED          -> 「我充好电了」：整体静默 snooze_hours 小时
- UNSUB-<学号>      -> 该收件人永久退订（只影响他自己）
- RESUB-<学号>      -> 该收件人重新订阅

告警邮件的「✅ 我充好电了」按钮和页脚「退订 / 重新订阅」链接都是预写好的
mailto: 链接，手机点开发送即可，本模块负责识别。
"""

import imaplib
import logging
import re
from datetime import datetime, timedelta
from email.utils import parseaddr

log = logging.getLogger("fdu")

FEEDBACK_SUBJECT = "REFILLED"
RE_UNSUB = re.compile(r"UNSUB-([A-Za-z0-9._-]+)")
RE_RESUB = re.compile(r"RESUB-([A-Za-z0-9._-]+)")

# 常见邮箱的 IMAP 服务器自动猜测表
IMAP_HOSTS = {
    "163.com": "imap.163.com",
    "qq.com": "imap.qq.com",
    "exmail.qq.com": "imap.exmail.qq.com",
    "fudan.edu.cn": "mail.fudan.edu.cn",
}


def guess_imap_host(smtp_host):
    for suffix, host in IMAP_HOSTS.items():
        if smtp_host.endswith(suffix) or smtp_host == suffix:
            return host
    return None


def _month_abbr(d):
    return ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.month - 1]


def _token_to_addr(token, roster):
    """学号（或完整地址）-> 名册中的完整地址。"""
    token = token.strip().lower()
    for addr in roster:
        a = str(addr).strip().lower()
        if a == token or a.split("@")[0] == token:
            return a
    return None


def poll_reply_snooze(smtp_cfg, state, snooze_hours=24, roster=None, dry_run=False):
    """轮询收件箱，处理 REFILLED / UNSUB- / RESUB- 反馈。

    roster: config smtp.to 的完整地址列表，用于把学号 token 映射回地址。
    返回 {"refill": bool, "unsub": [addr], "resub": [addr]}。
    """
    result = {"refill": False, "unsub": [], "resub": []}
    if state.snoozed():
        return result  # 已静默，无需轮询
    user = (smtp_cfg.get("user") or "").lower()
    imap_host = smtp_cfg.get("imap_host") or guess_imap_host(smtp_cfg.get("host", ""))
    if not imap_host or not smtp_cfg.get("password") \
            or "YOUR_" in str(smtp_cfg.get("password")):
        return result
    roster = roster or []
    imap = None
    try:
        imap = imaplib.IMAP4_SSL(imap_host, 993, timeout=20)
        imap.login(smtp_cfg["user"], smtp_cfg["password"])
        try:  # 网易 IMAP 需要 ID 命令，否则报 Unsafe Login
            imaplib.Commands["ID"] = ("AUTH",)
            imap._simple_command(
                "ID", '("name" "fdu-dorm-power-sentinel" "version" "1.0")')
        except Exception:
            pass
        imap.select("INBOX")
        since = datetime.now() - timedelta(days=2)
        date_str = "%02d-%s-%04d" % (since.day, _month_abbr(since), since.year)

        def _search(prefix):
            typ, data = imap.search(None, "SINCE", date_str, "UNSEEN",
                                    "SUBJECT", '"%s"' % prefix)
            return (data[0] or b"").split() if typ == "OK" else []

        def _header(num, field):
            typ, msg_data = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (%s)])" % field)
            if typ != "OK":
                return b""
            return b"".join(p[1] for p in msg_data if isinstance(p, tuple))

        all_ids = []
        # 1) 整体静默
        for num in _search(FEEDBACK_SUBJECT):
            all_ids.append(num)
            sender = parseaddr(_header(num, "FROM").decode("utf-8", "replace"))[1].lower()
            if sender and sender != user:
                result["refill"] = True
        # 2) 逐人退订 / 恢复
        for num in _search("UNSUB-"):
            all_ids.append(num)
            subject = _header(num, "SUBJECT").decode("utf-8", "replace")
            m = RE_UNSUB.search(subject)
            addr = _token_to_addr(m.group(1), roster) if m else None
            if addr:
                state.opt_out(addr)
                result["unsub"].append(addr)
        for num in _search("RESUB-"):
            all_ids.append(num)
            subject = _header(num, "SUBJECT").decode("utf-8", "replace")
            m = RE_RESUB.search(subject)
            addr = _token_to_addr(m.group(1), roster) if m else None
            if addr:
                state.opt_in(addr)
                result["resub"].append(addr)
        # 3) 处理过的标记已读，避免重复识别
        for num in all_ids:
            try:
                imap.store(num, "+FLAGS", "\\Seen")
            except Exception:
                pass
        if result["refill"]:
            state.set_snooze(snooze_hours)
            log.warning("收到「我充好电了」反馈，静默至 %s", state.snooze_until_str())
        if result["unsub"]:
            log.warning("收到退订反馈: %s", ", ".join(result["unsub"]))
        if result["resub"]:
            log.warning("收到重新订阅反馈: %s", ", ".join(result["resub"]))
        if any(result.values()):
            state.save()
        return result
    except Exception as e:
        log.warning("IMAP 检查反馈失败（不影响监控）: %s", e)
        return result
    finally:
        if imap:
            try:
                imap.logout()
            except Exception:
                pass


def feedback_mailto(smtp_cfg):
    """「我充好电了」mailto 链接。"""
    return "mailto:%s?subject=%s&body=charged" % (smtp_cfg.get("user", ""),
                                                  FEEDBACK_SUBJECT)


def person_links(smtp_cfg, addr):
    """给单个收件人的个性化链接：充好电（通用）+ 退订/恢复（只影响他自己）。"""
    user = smtp_cfg.get("user", "")
    token = str(addr).split("@")[0]
    return {
        "refill": feedback_mailto(smtp_cfg),
        "unsub": "mailto:%s?subject=UNSUB-%s" % (user, token),
        "resub": "mailto:%s?subject=RESUB-%s" % (user, token),
    }
