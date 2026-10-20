# fdu-dorm-power-sentinel

**复旦寝室电量哨兵**：自适应监控宿舍剩余电量，撑不过半天自动发邮件催充电（持续提醒直到充值），
深夜断电前自动关机保护电脑，充值后一键静默。

数据来源与认证流程复刻自复旦校园 App [DanXi（旦夕）](https://github.com/DanXi-Dev/DanXi)
的开源实现（`dorm_repository.dart` + `neo_login_tool.dart`），在此致谢。本项目与其无隶属关系。
纯 Python 标准库风格实现，仅依赖 `requests` + `cryptography`，Windows 计划任务托管，无常驻进程。

---

## 功能特性

- **电量监控**：登录复旦统一身份认证，每 30 分钟自动查询寝室剩余电量（`fj_surplus`）
- **自适应速率**：本地余额差优先 + 指数加权日用电量（半衰期 2 天），开空调/关空调等行为变化 2~3 天自动跟上
- **走表倒计时**：余额一天只同步几次？没关系——「还能用 X 小时」按已过时间实时递减，不干等刷新
- **半天告警**：预计可用 < 12 小时 → 发邮件催充值；持续期每 12 小时重发一封；恢复后自动发解除邮件
- **充值静默**：邮件里点绿色按钮 / 双击 `我充好电了.bat` → 静默 24 小时（盖过电表同步延迟）
- **逐人退订**：每封邮件页脚带「退订 / 重新订阅」链接，每个收件人自己决定收不收
- **低电量自动关机**：悲观口径触发（夜间提前 60 分钟、白天 20 分钟），10 分钟倒计时可一键取消
- **降噪**：夜间（23:00–08:00）不发提醒邮件；安全区（>24h）自动降低查询频率

---

## 工作原理

### 1. 数据获取

登录复旦新版统一身份认证（Neo，`id.fudan.edu.cn`）后，请求上网服务大厅的接口：

```
GET https://zlapp.fudan.edu.cn/fudanelec/wap/default/info
```

返回 JSON 中关键字段：`fj_surplus`（剩余电量/度）、`fj_used`、`fj_all`、`fj_update_time`、
`ssmc`/`fjmc`（楼栋/房间号）。服务端按学号自动关联寝室，**无需配置房间**。

Neo 认证流程（与 DanXi 一致）：

```
GET 业务地址 ──302──> id.fudan.edu.cn（fragment 里有 lck + entityId）
POST /idp/authn/queryAuthMethods     -> 认证链（second:true = 需要两步验证）
POST /idp/authn/getJsPublicKey       -> RSA 公钥
密码 RSA-PKCS1 加密
POST /idp/authn/authExecute          -> loginToken
POST /idp/authCenter/authnEngine     -> ticket 表单
GET  action?ticket=...               -> 会话建立（Cookie 持久化，未过期免登录）
```

> 该接口不在复旦强制 2FA 的接口名单内（强制名单是 my.fudan.edu.cn 等，见 DanXi issue #607）。
> 万一被拦，见下文「Cookie 降级模式」。

### 2. 速率估算（自适应）

每次查询的余额都会记录到本地 `state.json`（全量保留）。速率按以下优先级估算：

1. **本地窗口**：最近 48h 内、最后一次充值跳变之后的首尾样本，`速率 = 电量差 ÷ 时间差`
2. **EWMA 日用电**：上一步无效时，用数据中心导出的每日用电量做指数加权平均
   （半衰期 2 天，最近几天权重高），`速率 = 加权日均 ÷ 24`
3. 都无效 → 返回"未知"，此时若剩余 ≤ 5 度按保守策略直接告警

**走表衰减**：余额快照会陈旧，所以预测按「距上次余额变化已消耗的量」折算：

```
可用小时 = (当前余额 − 速率 × 距上次余额变化的小时数) ÷ 速率     （衰减上限 48h，下限 0）
```

于是倒计时随时间实时递减，而不是干等电表刷新。

**悲观速率**（专供关机决策）：`max(EWMA 速率, 近 7 天最大单日 ÷ 24)`。
误关机概率 = 某天用电超过近期最大单日的概率，很小；漏保护的代价由夜间提前窗口兜底。

### 3. 检查节奏（自适应）

计划任务固定每 30 分钟触发一次，脚本按**预测可用时长**分档自适应：

| 预计可用 | 档位 | 行为 |
|---|---|---|
| > 24h | 安全区 | 距上次查询不足 `safe_skip_minutes`（默认 120 分钟）就直接跳过联网 |
| 6–24h | 关注区 | 每 30 分钟正常查询 |
| ≤ 6h | 危险区 | 单次运行内每 10 分钟加密复查（最多约 25 分钟，不占用下一个任务周期） |

### 4. 告警逻辑

```
预测可用 < 12h（或冷启动且余额 ≤ 5 度）
    ├─ 首次触发 ──> 立即发「充电提醒」（夜间 23:00–08:00 推迟到早上）
    ├─ 持续中   ──> 每 12 小时重发一封（夜间静默顺延）
    └─ 恢复 ≥12h ─> 发「解除提醒」
```

- 邮件为纯文本 + HTML 双部分，HTML 版含绿色「✅ 我充好电了」按钮
- 告警判定的预测值就是走表倒计时，会随时间真实递减

### 5. 反馈通道（零人工，全靠邮件主题令牌）

脚本每轮检查时用 IMAP 轮询收件箱（与发件同一账号），识别三类主题：

| 回信主题 | 效果 |
|---|---|
| `REFILLED` | 整体静默 24 小时：不发告警、不触发关机（盖过电表同步延迟） |
| `UNSUB-<学号>` | 该收件人退订（只影响自己） |
| `RESUB-<学号>` | 该收件人重新订阅 |

告警邮件里的按钮和页脚链接都是预写好的 `mailto:` 链接，手机点开发送即可。
静默到期后电表早已刷新：电量够了自动解除，不够则自动恢复告警。

### 6. 低电量自动关机（悲观口径）

```
悲观预测 = 余额（走表衰减后） ÷ 悲观速率

夜间（silent 前的 night_start–night_end，默认 00:00–07:00）:
    悲观预测 < 60 分钟 -> 发说明邮件 + shutdown /s /t 600（10 分钟倒计时）
白天:
    悲观预测 < 20 分钟 -> 发说明邮件 + shutdown /s /t 60
    护栏: 若平均口径预测仍 > 60 分钟，判定为数据陈旧导致的悲观误报，
          只提醒不关机（白天人在电脑前，邮件足够）
```

- `cancel_shutdown.bat` 一键取消当次关机，且当天不再重复发起
- 充值静默期间关机机制整体挂起
- 已验证电表实际约几小时同步一次（不同楼栋可能不同），悲观口径 + 提前窗口
  保证即使数据滞后也能在断电前干净关机

### 7. 状态流转总览

```
                 ┌────────────────────────────────────────────┐
                 │  计划任务(30min) -> main.py --once          │
                 └──────────────────┬─────────────────────────┘
                                    v
              安全区(>24h)且2h内查过？ ──是──> 跳过本轮
                                    │否
                                    v
                 登录查询余额 -> 记录样本 -> 估算速率/走表
                                    v
        ┌────────────── 静默期？ ──是──> 恢复了？解除静默 : 什么都不做
        │否
        v
   预测<12h？ ──否──> (原告警中？发解除邮件并复位)
        │是
        v
   夜间静默？──是──> 挂起邮件，早上补发
        │否
        v
   发/重发告警邮件(12h节流) ──> 悲观口径判关机 ──> 邮件 + shutdown 倒计时
```

---

## 快速开始

### 1. 安装

```bash
git clone https://github.com/michaellu2007/fdu-dorm-power-sentinel.git
cd fdu-dorm-power-sentinel
pip install -r requirements.txt   # 仅 requests + cryptography + openpyxl
```

### 2. 配置

复制 `config.example.json` 为 `config.json`（**已被 .gitignore 排除，不会进仓库**），填写：

| 字段 | 说明 |
|---|---|
| `uis.student_id` / `uis.password` | UIS 学号密码（仅存本地，用于登录查询） |
| `smtp.host` / `port` / `ssl` | 发件邮箱 SMTP。163: `smtp.163.com:465`，QQ: `smtp.qq.com:465`，教师邮箱: `mail.fudan.edu.cn:465` |
| `smtp.user` / `smtp.password` | 发件邮箱账号 + **授权码**（163/QQ 在邮箱设置里开启 SMTP 后生成；复旦学生云邮箱是腾讯企业邮，用 `smtp.exmail.qq.com` + 客户端专用密码） |
| `smtp.imap_host` | 反馈轮询用的 IMAP 服务器（163: `imap.163.com`） |
| `smtp.to` | 收件人列表，**每个人都会收到带专属退订链接的邮件** |

### 3. 验证

```bash
python main.py --dry-run --once   # 只查+打印：不发邮件不关机（首次强烈建议）
python main.py --test-email       # 发测试邮件验证 SMTP
python selftest.py                # 跑全部单元/集成自测
```

### 4. 常驻（Windows 计划任务，推荐）

右键 `register_task.ps1` →「使用 PowerShell 运行」（需在 UAC 弹窗点确认）：

```powershell
powershell -ExecutionPolicy Bypass -File register_task.ps1
```

任务 `FDU-DormPowerSentinel` 每 30 分钟静默运行一次（`pythonw.exe`，无黑窗），
开机持久、断电错过会补跑。删除：`schtasks /Delete /TN FDU-DormPowerSentinel /F`

---

## 日常使用

**最简单**：双击 `一键运行.bat`，菜单选择：

| 选项 | 作用 |
|---|---|
| `[1]` 演示查询 | dry-run：只看结果，不发邮件不关机 |
| `[2]` 正式查询一次 | 会发告警邮件/可能触发关机 |
| `[3]` 发测试邮件 | 验证邮箱配置 |
| `[5]` 导入用电历史 | 见下 |

命令行等价：`python main.py --once` / `--loop`（常驻调试）/ `--dry-run` / `--test-email`

### 充值后告诉系统「别再报了」（二选一）

1. 点最近一封告警邮件里的绿色按钮「✅ 我充好电了」，发送那封预写好的邮件；
2. 或双击 `我充好电了.bat`。

→ 静默 24 小时（不发告警、不触发关机），到期后电表已刷新，自动恢复正常判断。

### 断电前自动关机取消了，但电其实还没断？

双击 `cancel_shutdown.bat`：取消当次倒计时，且当天不再重复发起。

### 校准预测精度（推荐，隔几周做一次）

学校数据中心（my.fudan.edu.cn → 住宿区域用电情况）可导出**每日用电量 xlsx**。
把它放进本目录，菜单选 `[5]`（或 `python import_usage.py`），速率估算立即以你的
真实历史校准。没有这份文件系统也能跑（用本地记录的余额差估算）。

---

## 配置项总览（config.json）

### alert

| 键 | 默认 | 说明 |
|---|---|---|
| `alert_window_hours` | 12 | 预测可用低于该值即触发告警（"半天"线） |
| `cold_start_threshold_kwh` | 5 | 冷启动（速率未知）时的保守告警余额 |
| `repeat_interval_hours` | 12 | 告警持续期间重发邮件的间隔 |
| `silent_start` / `silent_end` | 23:00 / 08:00 | 夜间静默时段，不发提醒邮件 |
| `snooze_hours` | 24 | 反馈后静默时长 |
| `interval_minutes` | 30 | `--loop` 模式的常规间隔 |
| `fast_check_below_hours` | 6 | 危险区阈值，进入后单次运行内加密复查 |
| `safe_skip_minutes` | 120 | 安全区（>24h）跳查的最小查询间隔 |

### shutdown

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 低电量自动关机总开关 |
| `night_start` / `night_end` | 00:00 / 07:00 | 凌晨保护窗口（跨零点写法） |
| `night_trigger_minutes` / `p_night_trigger_minutes` | 40 / 60 | 夜间触发阈值（平均/悲观口径） |
| `day_trigger_minutes` / `p_day_trigger_minutes` | 10 / 20 | 白天触发阈值（平均/悲观口径） |
| `night_countdown_seconds` / `day_countdown_seconds` | 600 / 60 | 关机倒计时 |

> 凌晨提前量 = 悲观触发线 60 分钟 − 倒计时 10 分钟，即**剩约 50 分钟电量时就开始保护**。

### 其他

| 键 | 说明 |
|---|---|
| `proxy` | 一般留空直连；校内接口不通时才填 `"http://127.0.0.1:7897"` 这类 |

---

## 文件说明

| 文件 | 用途 | 进仓库？ |
|---|---|---|
| `main.py` | 入口与编排（查询→估算→告警→关机） | ✅ |
| `fdu_client.py` | Neo 认证 + 电量查询 + Cookie 持久化 | ✅ |
| `estimator.py` | 速率估算（本地窗口/EWMA/悲观）+ 走表衰减 | ✅ |
| `feedback.py` | IMAP 识别 REFILLED / UNSUB / RESUB 反馈 | ✅ |
| `notifier.py` | SMTP 邮件（文本+HTML，逐人个性化） | ✅ |
| `shutdowner.py` | 关机判定与执行、取消标记 | ✅ |
| `storage.py` | state.json（历史/告警/静默/退订） | ✅ |
| `import_usage.py` | 导入数据中心日用电 xlsx | ✅ |
| `menu.py` / `selftest.py` | 一键菜单 / 自测 | ✅ |
| `config.json` `state.json` `cookies.txt` `monitor.log` `roommates.txt` | 本地隐私数据 | ❌ 已 ignore |

---

## 常见问题

- **两步验证（2FA/增强认证）**：复旦对部分系统接口侧强制 2FA（见 DanXi issue #607），无法关闭；
  本脚本只用 `zlapp.fudan.edu.cn` 的余额接口，正常无需 2FA。若被拦（日志出现
  `TwoFactorRequired`），用 Cookie 降级模式：浏览器登录 zlapp 电费页 → 用 Cookie 导出插件
  把 `zlapp.fudan.edu.cn` 的 Cookie 导出为 Netscape 格式存成 `cookies.txt` → 脚本复用直到过期
  （过期会收到「监控停摆」邮件，重新导一次即可）。
- **登录触发验证码**：多为密码连续输错或登录过频，等一段时间重试。
- **改了 UIS 密码 / 连续失败**：连续 3 次失败会收到「监控停摆」邮件（24h 限一次），改好配置即可。
- **电表更新频率**：不同楼栋不同（实测有楼栋几小时一更）。走表倒计时 + 悲观关机 +
  充值静默三层机制保证任何粒度下都不误事。
- **夜间断网/睡眠**：任务错过会由 `StartWhenAvailable` 补跑；电脑关机状态下自然不会查
  （也无需查——真断电时关机保护早已执行）。

## 数据与隐私

仓库只包含代码、`config.example.json`（纯占位符）与本文档。所有含个人信息的文件
（`config.json`、`state.json`、`cookies.txt`、`monitor.log`、`roommates.txt`）均被
`.gitignore` 排除，请勿 `git add -f`。Git 提交请使用 noreply 邮箱。

## License

MIT
