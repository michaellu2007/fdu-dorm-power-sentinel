# -*- coding: utf-8 -*-
"""复旦寝室电量查询客户端。

复刻 DanXi-Dev/DanXi 的实现：
- 数据接口: GET https://zlapp.fudan.edu.cn/fudanelec/wap/default/info
  返回 JSON (d 字段) 含 fj_surplus(剩余电量/度)、fj_update_time、ssmc(楼号)、fjmc(房间号) 等。
- 认证: Neo 统一身份认证 (id.fudan.edu.cn)：
  1) GET 业务地址 -> 302 跳到 id.fudan.edu.cn，从最终 URL fragment / 页面中取 lck + entityId
  2) POST /idp/authn/queryAuthMethods {lck, entityId} -> 认证链 (second=true 表示需要两步验证)
  3) POST /idp/authn/getJsPublicKey -> RSA 公钥 (base64 SPKI)
  4) POST /idp/authn/authExecute {学号, RSA-PKCS1 加密密码} -> loginToken
  5) POST /idp/authCenter/authnEngine {loginToken} -> HTML 里的 form#logon(action) + input#ticket
  6) GET action?ticket=... 回跳业务站点，会话建立
Cookie 持久化到本地，未过期时免登录直接查询。
"""

import base64
import json
import logging
import os
import re
import urllib.parse

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from http.cookiejar import MozillaCookieJar

log = logging.getLogger("fdu")

ID_HOST = "id.fudan.edu.cn"
ELEC_INFO_URL = "https://zlapp.fudan.edu.cn/fudanelec/wap/default/info"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

REDIRECT_STATUSES = (301, 302, 303, 307, 308)


class FduError(Exception):
    """复旦接口/认证相关错误基类。"""


class TwoFactorRequiredError(FduError):
    """需要两步验证，纯脚本无法继续。"""


class CaptchaRequiredError(FduError):
    """触发图形验证码（多为密码连续错误或登录过于频繁）。"""


class BadCredentialsError(FduError):
    """学号或密码错误。"""


class FduClient:
    def __init__(self, student_id, password, cookie_path="cookies.txt", proxy=""):
        self.student_id = str(student_id)
        self.password = str(password)
        self.cookie_path = cookie_path
        self.sess = requests.Session()
        self.sess.trust_env = False  # 校内站点直连，不吃系统代理
        if proxy:
            self.sess.proxies = {"http": proxy, "https": proxy}
        self.sess.headers.update({"User-Agent": UA})
        self._load_cookies()

    # ---------- cookie 持久化 ----------

    def _load_cookies(self):
        jar = MozillaCookieJar(self.cookie_path)
        if os.path.exists(self.cookie_path):
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
                self.sess.cookies.update(jar)
                log.info("已加载本地 Cookie: %s", self.cookie_path)
            except Exception as e:  # 损坏就重建
                log.warning("Cookie 文件读取失败，忽略: %s", e)

    def _save_cookies(self):
        jar = MozillaCookieJar(self.cookie_path)
        for c in self.sess.cookies:
            jar.set_cookie(c)
        try:
            jar.save(ignore_discard=True, ignore_expires=True)
        except Exception as e:
            log.warning("Cookie 保存失败: %s", e)

    # ---------- HTTP 基础 ----------

    def _get_follow_redirects(self, url, max_hops=10, timeout=30):
        """手动跟随重定向（保留 Location 里的 fragment，Neo 的 lck 可能藏在 fragment）。"""
        cur = url
        resp = None
        for _ in range(max_hops):
            resp = self.sess.get(cur, allow_redirects=False, timeout=timeout)
            if resp.status_code in REDIRECT_STATUSES:
                loc = resp.headers.get("Location")
                if not loc:
                    break
                nxt = urllib.parse.urljoin(cur, loc)
                if nxt == cur:
                    break
                cur = nxt
                continue
            break
        return resp, cur

    @staticmethod
    def _parse_ticket_form(html_text):
        """从 HTML 中解析 form#logon 的 action 和 input#ticket 的 value。"""
        if not html_text:
            return None
        m_form = re.search(r'<form[^>]*\bid\s*=\s*["\']logon["\'][^>]*>', html_text, re.I)
        if not m_form:
            return None
        tag = m_form.group(0)
        m_action = re.search(r'action\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        m_ticket = re.search(
            r'<input[^>]*\bid\s*=\s*["\']ticket["\'][^>]*>', html_text, re.I)
        if not m_action or not m_ticket:
            return None
        m_val = re.search(r'value=["\']([^"\']*)["\']', m_ticket.group(0), re.I)
        if not m_val:
            return None
        action = urllib.parse.urljoin(
            "https://" + ID_HOST + "/", m_action.group(1).replace("&amp;", "&"))
        return action, m_val.group(1)

    @staticmethod
    def _extract_lck_entity_id(url, html_text):
        """从 URL (query/fragment) 或页面源码里抠出 lck 和 entityId。"""
        candidates = []
        if url:
            candidates.append(url)
        if html_text:
            candidates.append(html_text)
        for src in candidates:
            m_lck = re.search(r'\blck\s*[=:]\s*["\']?([^"\'&\s<>]+)', src)
            m_ent = re.search(r'\bentityId\s*[=:]\s*["\']?([^"\'&\s<>]+)', src)
            if m_lck and m_ent:
                lck = urllib.parse.unquote(m_lck.group(1))
                ent = urllib.parse.unquote(m_ent.group(1))
                return lck, ent
        return None, None

    @staticmethod
    def _parse_info_json(resp):
        """把电量接口响应解析成 dict；不是有效数据时返回 None。"""
        if resp is None or resp.status_code != 200:
            return None
        ctype = resp.headers.get("Content-Type", "")
        if "html" in ctype.lower():
            return None
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            return None
        d = data.get("d") if isinstance(data, dict) else None
        if not isinstance(d, dict) or "fj_surplus" not in d:
            return None
        return {
            "campus": d.get("xq"),
            "building": d.get("ssmc"),
            "room": d.get("fjmc"),
            "surplus": float(d.get("fj_surplus") or 0),
            "used": float(d.get("fj_used") or 0),
            "total": float(d.get("fj_all") or 0),
            "update_time": d.get("fj_update_time"),
        }

    # ---------- 对外接口 ----------

    def get_electricity(self):
        """查询寝室电量；会话过期则自动重新登录后重试一次。"""
        resp, final_url = self._get_follow_redirects(ELEC_INFO_URL)
        info = self._parse_info_json(resp)
        if info is not None:
            return info
        log.info("会话失效（%s），重新登录...", resp.status_code if resp else "no resp")
        self.login()
        resp, final_url = self._get_follow_redirects(ELEC_INFO_URL)
        info = self._parse_info_json(resp)
        if info is None:
            raise FduError("登录后仍未获取到电量数据，请检查账号或接口是否变化")
        return info

    def login(self):
        """完整的 Neo 认证流程。"""
        self.sess.cookies.clear()
        resp, final_url = self._get_follow_redirects(ELEC_INFO_URL)
        html_text = resp.text if resp is not None else ""

        # 已有 SSO 会话：直接吃 ticket 回跳
        form = self._parse_ticket_form(html_text)

        lck, entity_id = self._extract_lck_entity_id(final_url, html_text)
        if form is None:
            if not lck or not entity_id:
                raise FduError(
                    "未能从登录页获取 lck/entityId（登录流程可能已变化），"
                    f"最终URL: {final_url}")
            log.info("开始 Neo 认证 (lck=%s...)", lck[:12])

            # 1. 认证方式
            r = self.sess.post(
                f"https://{ID_HOST}/idp/authn/queryAuthMethods",
                json={"lck": lck, "entityId": entity_id}, timeout=30)
            methods = r.json()
            if methods.get("second"):
                raise TwoFactorRequiredError(
                    "该账号/该服务要求两步验证(2FA)，脚本无法自动登录。"
                    "可改用「浏览器手动登录导出 Cookie」模式。")
            chain = next((m.get("authChainCode") for m in methods.get("data", [])
                          if m.get("moduleCode") == "userAndPwd"), None)
            if not chain:
                raise FduError(f"认证方式列表里没有账密登录模块: {methods}")

            # 2. RSA 公钥
            r = self.sess.post(f"https://{ID_HOST}/idp/authn/getJsPublicKey",
                               json={}, timeout=30)
            pub_b64 = r.json().get("data")
            if not pub_b64:
                raise FduError(f"获取公钥失败: {r.text[:200]}")
            pub = serialization.load_der_public_key(base64.b64decode(pub_b64))
            enc_pwd = base64.b64encode(
                pub.encrypt(self.password.encode("utf-8"), padding.PKCS1v15())
            ).decode("ascii")

            # 3. 提交账密
            r = self.sess.post(
                f"https://{ID_HOST}/idp/authn/authExecute",
                json={
                    "authModuleCode": "userAndPwd",
                    "authChainCode": chain,
                    "entityId": entity_id,
                    "requestType": "chain_type",
                    "lck": lck,
                    "authPara": {
                        "loginName": self.student_id,
                        "password": enc_pwd,
                        "verifyCode": "",
                    },
                }, timeout=30)
            auth = r.json()
            token = auth.get("loginToken")
            msg = auth.get("message", "") or ""
            if "验证码" in msg:
                raise CaptchaRequiredError(f"触发验证码: {msg}")
            if not token:
                if "用户名" in msg or "密码" in msg:
                    raise BadCredentialsError(f"学号或密码错误: {msg}")
                raise TwoFactorRequiredError(
                    f"登录未返回 token（大概率需要两步验证）: {msg}")

            # 4. 换 ticket（form 编码的 loginToken=... 是正确姿势，raw body 兜底）
            r = self.sess.post(
                f"https://{ID_HOST}/idp/authCenter/authnEngine",
                data={"loginToken": token}, timeout=30)
            form = self._parse_ticket_form(r.text)
            if form is None:
                r = self.sess.post(
                    f"https://{ID_HOST}/idp/authCenter/authnEngine",
                    data=token.encode("utf-8"),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=30)
                form = self._parse_ticket_form(r.text)
            if form is None:
                raise FduError("authnEngine 未返回 ticket 表单，登录失败")

        action_url, ticket = form
        sep = "&" if "?" in action_url else "?"
        jump = f"{action_url}{sep}ticket={urllib.parse.quote(ticket)}"
        self.sess.get(jump, timeout=30, allow_redirects=True)
        self._save_cookies()
        log.info("Neo 认证完成，会话已建立")


if __name__ == "__main__":
    # 快速自测：解析/正则部分（不需要网络）
    html = ('<form id="logon" action="https://zlapp.fudan.edu.cn/cas?foo=1&amp;bar=2">'
            '<input id="ticket" name="ticket" value="ST-123-abc"/>'
            '<input type="submit"/></form>')
    f = FduClient._parse_ticket_form(html)
    assert f and f[0] == "https://zlapp.fudan.edu.cn/cas?foo=1&bar=2" and f[1] == "ST-123-abc", f
    l, e = FduClient._extract_lck_entity_id(
        "https://id.fudan.edu.cn/idp/page/login.html#/?lck=ABC123&entityId=urn%3Aexample", None)
    assert l == "ABC123" and e == "urn:example", (l, e)
    l2, e2 = FduClient._extract_lck_entity_id(
        None, 'var lck = "XYZ"; var entityId = "urn:test";')
    assert l2 == "XYZ" and e2 == "urn:test", (l2, e2)
    info = FduClient._parse_info_json(type("R", (), {
        "status_code": 200, "headers": {"Content-Type": "application/json"},
        "text": json.dumps({"e": 0, "m": "operation successful", "d": {
            "xq": "邯郸", "ssmc": "15号楼", "fjmc": "1501-1", "fj_surplus": 145,
            "fj_used": 5015, "fj_all": 5160, "fj_update_time": "2026-10-19 00:00:00"}}),
        "json": lambda self: json.loads(self.text)})())
    assert info["surplus"] == 145 and info["building"] == "15号楼", info
    print("fdu_client self-test OK")
