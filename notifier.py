# -*- coding: utf-8 -*-
"""SMTP 邮件通知（默认复旦学校邮箱，配置可换成任意邮箱）。"""

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

log = logging.getLogger("fdu")


def _smtp_connect(cfg):
    if cfg.get("ssl", True):
        server = smtplib.SMTP_SSL(cfg["host"], int(cfg.get("port", 465)), timeout=30)
    else:
        server = smtplib.SMTP(cfg["host"], int(cfg.get("port", 25)), timeout=30)
        if cfg.get("starttls"):
            server.starttls()
    return server


def send_email(smtp_cfg, subject, body, dry_run=False, html=None, to=None):
    """发一封邮件（纯文本 + 可选 HTML 部分）。to 为空则用配置里的收件人列表。"""
    user = smtp_cfg.get("user")
    recipients = list(to) if to else (smtp_cfg.get("to") or [user])
    sender_name = smtp_cfg.get("from_name", "FDU Dorm Power Sentinel")
    sender = formataddr((sender_name, user))

    if dry_run:
        log.info("[dry-run] 跳过发送邮件 -> %s | %s", ", ".join(recipients), subject)
        return True
    if not user or not smtp_cfg.get("password") \
            or "YOUR_" in str(user) or "YOUR_" in str(smtp_cfg.get("password")):
        log.warning("SMTP 未配置（config.json 里还是占位符），跳过发信")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

    try:
        with _smtp_connect(smtp_cfg) as server:
            server.login(user, smtp_cfg["password"])
            server.send_message(msg)
        log.info("邮件已发送 -> %s | %s", ", ".join(recipients), subject)
        return True
    except Exception as e:
        log.error("邮件发送失败: %s", e)
        return False
