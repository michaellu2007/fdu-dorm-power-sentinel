# -*- coding: utf-8 -*-
"""双击「我充好电了.bat」调用的静默脚本：静默默认 24 小时。"""

import os

from storage import StateStore

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNOOZE_HOURS = 24


def main():
    state = StateStore(os.path.join(BASE_DIR, "state.json"))
    state.set_snooze(SNOOZE_HOURS)
    state.save()
    print("已静默 %d 小时（至 %s），期间不再发送告警邮件、不再触发自动关机。"
          % (SNOOZE_HOURS, state.snooze_until_str()))
    print("电表余额明天更新后，若电量确实充足，系统会自动恢复正常待命。")
    input("按回车关闭...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
