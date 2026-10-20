# -*- coding: utf-8 -*-
"""双击「一键运行.bat」后进入的交互菜单。

执行完一个操作后回到菜单（而不是退出关窗），任何崩溃都打印完整
traceback 并停窗，保证错误信息一定能被看到。
"""

import traceback

import main
from main import setup_logging, load_config


def banner():
    print("=" * 56)
    print("  复旦寝室电量哨兵 · FDU Dorm Power Sentinel")
    print("  计划任务已每 30 分钟自动检查；下面是手动操作入口")
    print("=" * 56)


def run_choice(choice):
    if choice == "1":
        main.main_args(run_once=True, dry_run=True, force_query=True)
    elif choice == "2":
        main.main_args(run_once=True, dry_run=False, force_query=True)
    elif choice == "3":
        main.main_args(test_email=True)
    elif choice == "5":
        import import_usage
        import_usage.main()
        print("\n导入完成，马上按新速率正式查一次...")
        main.main_args(run_once=True, dry_run=False, force_query=True)
    elif choice == "4":
        return False
    else:
        print("没有这个选项。")
    return True


def menu():
    setup_logging()
    banner()
    if load_config() is None:
        print("\n>>> 请先用记事本打开 config.json 填好学号/密码，再回来运行 <<<\n")
    while True:
        print("\n  [1] 演示查询（dry-run：只看结果，不发邮件不关机）")
        print("  [2] 正式查询一次（会发告警邮件/可能触发关机）")
        print("  [3] 发一封测试邮件（验证邮箱配置）")
        print("  [5] 导入住宿区域用电情况.xlsx（校准日均用电，推荐隔几周做一次）")
        print("  [4] 退出")
        try:
            choice = input("\n请输入序号并回车: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        try:
            if not run_choice(choice):
                return
        except Exception:
            print("\n!!! 出错了，详细信息如下（已写入 monitor.log）:")
            traceback.print_exc()


if __name__ == "__main__":
    try:
        menu()
    except KeyboardInterrupt:
        pass
    except Exception:
        print("\n!!! 程序崩溃，详细信息如下:")
        traceback.print_exc()
        input("\n按回车关闭窗口...")
