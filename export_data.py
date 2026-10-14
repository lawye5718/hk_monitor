#!/usr/bin/env python3
"""定时更新后，把数据库里的全部数据导出到 Downloads 文件夹。

输出位置：~/Downloads/hk_monitor/
  - di_disclosure_<日期>.csv     全量权益披露记录
  - tracked_stocks_<日期>.csv    全量跟踪股票池
  - summary_<日期>.json          本次导出的摘要（时间 / 各表行数 / 文件名）
  - latest/                      最新一份的副本（固定文件名，每次覆盖，便于其它程序读取）

CSV 使用 utf-8-sig（带 BOM），确保 Excel 直接打开中文不乱码。
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hk_monitor.db")
OUT_ROOT = os.path.expanduser("~/Downloads/hk_monitor")

# 需要导出的表及其字段（与库结构一致，缺表/缺字段时会自动跳过并告警）
TABLES: dict[str, list[str]] = {
    "di_disclosure": [
        "hash_id",
        "date_fetched",
        "stock_code",
        "shareholder_name",
        "event_date",
        "shares_change",
        "holding_shares",
        "holding_percent",
        "reason",
        "event_open",
        "event_high",
        "event_low",
        "event_close",
    ],
    "tracked_stocks": [
        "stock_code",
        "first_fetched_at",
    ],
}


def beijing_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=8)


def export(out_root: str = OUT_ROOT) -> dict:
    bj = beijing_now()
    stamp = bj.strftime("%Y-%m-%d")

    os.makedirs(out_root, exist_ok=True)
    latest_dir = os.path.join(out_root, "latest")
    os.makedirs(latest_dir, exist_ok=True)

    if not os.path.isfile(DB_PATH):
        raise FileNotFoundError(f"数据库不存在: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    summary: dict = {
        "exported_at_beijing": bj.strftime("%Y-%m-%d %H:%M:%S"),
        "database": DB_PATH,
        "output_dir": out_root,
        "tables": [],
    }

    try:
        for table, cols in TABLES.items():
            try:
                # 校验表存在
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not exists:
                    print(f"  ⚠️ 跳过 {table}：库中无此表")
                    continue

                # 以实际存在的字段为准，避免字段变更导致崩溃
                actual = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
                use_cols = [c for c in cols if c in actual]
                missing = [c for c in cols if c not in actual]
                if missing:
                    print(f"  ⚠️ {table} 缺少字段 {missing}，将按实际字段导出")

                rows = conn.execute(
                    f'SELECT {", ".join(use_cols)} FROM "{table}"'
                ).fetchall()

                dated_path = os.path.join(out_root, f"{table}_{stamp}.csv")
                with open(dated_path, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.writer(f)
                    writer.writerow(use_cols)
                    for r in rows:
                        writer.writerow([r[c] for c in use_cols])

                latest_path = os.path.join(latest_dir, f"{table}.csv")
                shutil.copyfile(dated_path, latest_path)

                summary["tables"].append(
                    {
                        "table": table,
                        "rows": len(rows),
                        "dated_file": os.path.basename(dated_path),
                        "latest_file": os.path.join("latest", os.path.basename(latest_path)),
                    }
                )
                print(f"  ✅ {table}: {len(rows)} 行 -> {dated_path}")

            except sqlite3.Error as exc:
                print(f"  ⚠️ 导出 {table} 失败: {exc}")
                continue
    finally:
        conn.close()

    summary_path = os.path.join(out_root, f"summary_{stamp}.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"  📝 摘要 -> {summary_path}")

    return summary


if __name__ == "__main__":
    print("[export_data] 开始导出全量数据")
    result = export()
    print(
        "[export_data] 完成："
        + ", ".join(f"{t['table']}={t['rows']}行" for t in result["tables"])
    )
