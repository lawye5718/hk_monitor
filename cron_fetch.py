#!/usr/bin/env python3
"""
定时任务入口：每天【北京时间 06:00】拉取一次数据。
系统时区为 PDT/PST（美西），launchd 按本地时间调度，因此本脚本用 UTC 强校验，
确保只有北京时间 05:50~06:10 窗口内才真正执行，自动兼容夏令时切换。
流程：读取 .env 股票池 -> 新股票自动 180 天全量 / 已有股票 3 天增量。
"""
import os
import sys
import subprocess
from datetime import datetime, timedelta, timezone

LOCK_FILE = "/tmp/hk_monitor_daily.lock"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def beijing_now():
    return datetime.now(timezone.utc) + timedelta(hours=8)


def main():
    bj = beijing_now()
    # 窗口：北京时间 05:50 ~ 06:10（UTC 前一日 21:50 ~ 22:10）
    in_window = (bj.hour == 5 and bj.minute >= 50) or (bj.hour == 6 and bj.minute <= 10)
    if not in_window:
        print(f"[{bj:%Y-%m-%d %H:%M}] 跳过：北京时间 {bj.hour:02d}:{bj.minute:02d} 不在 06:00 窗口")
        return

    # 防重复：24 小时内只执行一次
    now_ts = bj.timestamp()
    if os.path.exists(LOCK_FILE):
        try:
            last_ts = float(open(LOCK_FILE).read().strip())
            if now_ts - last_ts < 20 * 3600:  # 20 小时内不重复
                print(f"[{bj:%Y-%m-%d %H:%M}] 跳过：24 小时内已执行过")
                return
        except (ValueError, OSError):
            pass
    open(LOCK_FILE, "w").write(str(now_ts))

    print(f"[{bj:%Y-%m-%d %H:%M}] 北京时间 06:00 定时拉取开始")
    os.chdir(BASE_DIR)
    # 输出实时刷新（-u），便于 launchd 日志查看
    proc = subprocess.run([sys.executable, "-u", "data_fetcher.py"])
    print(f"[{bj:%Y-%m-%d %H:%M}] 拉取结束，退出码 {proc.returncode}")


if __name__ == "__main__":
    main()
