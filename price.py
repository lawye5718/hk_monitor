"""腾讯港股行情模块：历史日K线 + 实时快照 + 看板行情视图。
供 data_fetcher.py（变动日 OHLC 补全）与 app.py（实时行情栏）共用。
- 历史日K：web.ifzq.gtimg.cn（空复权段 = 原始价格，不复权）
- 实时快照：qt.gtimg.cn（GBK，含中文名与当日开/高/低/现价）
"""
import datetime
import json
import re
import time
import zoneinfo

import requests

QT_REALTIME = "https://qt.gtimg.cn/q=hk{code}"
QT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://gu.qq.com/",
}
# 显式禁用系统代理，走直连（腾讯行情海外可直连）
NO_PROXY = {"http": None, "https": None}
HKTZ = zoneinfo.ZoneInfo("Asia/Hong_Kong")
HK_CLOSE_MINUTE = 16 * 60 + 10  # 港股收盘(含收市竞价)约 16:10


def now_hk() -> datetime.datetime:
    return datetime.datetime.now(HKTZ)


def today_hk() -> datetime.date:
    return now_hk().date()


def _get(url: str, params: dict = None, decode_gbk: bool = False, retries: int = 2):
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=HEADERS,
                             proxies=NO_PROXY, timeout=12)
            if decode_gbk:
                return r.content.decode("gbk", errors="ignore")
            return r.text
        except requests.RequestException as e:
            last_err = e
            time.sleep(1.0 * (attempt + 1))
    raise last_err


def fetch_daily_kline(code5: str, limit_days: int = 260) -> list[dict]:
    """腾讯港股日K线（原始价/不复权），返回升序 [{date, open, close, high, low, volume}]"""
    end = today_hk()
    start = end - datetime.timedelta(days=limit_days)
    text = _get(QT_KLINE, params={
        "param": f"hk{code5},day,{start:%Y-%m-%d},{end:%Y-%m-%d},{limit_days},"
    })
    js = _safe_json(text)
    data = (js or {}).get("data", {}).get(f"hk{code5}", {}) if js else {}
    rows = []
    for bar in (data.get("day") or data.get("qfqday") or []):
        if not bar or len(bar) < 6:
            continue
        # 行格式: [日期, 开, 收, 高, 低, 成交量(, 复权信息)]
        try:
            rows.append({
                "date": bar[0],
                "open": float(bar[1]),
                "close": float(bar[2]),
                "high": float(bar[3]),
                "low": float(bar[4]),
                "volume": bar[5],
            })
        except (TypeError, ValueError):
            continue
    return rows


def _safe_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def fetch_realtime(code5: str) -> dict | None:
    """实时快照 {name, price, open, high, low, prev_close, time}；失败/停牌返回 None"""
    text = _get(QT_REALTIME.format(code=code5), decode_gbk=True)
    m = re.search(r'="(.*)"', text)
    if not m:
        return None
    v = m.group(1).split("~")
    if len(v) < 38 or v[3] in ("", "0.000"):
        return None  # 无实时价（停牌/未开盘数据缺失）
    return {
        "name": v[1],
        "price": _f(v[3]),
        "prev_close": _f(v[4]),
        "open": _f(v[5]),
        "high": _f(v[33]),
        "low": _f(v[34]),
        "time": v[30],
    }


def _f(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def get_market_view(code5: str) -> dict:
    """看板行情视图（供 app.py）：
    status: trading 交易中 / closed 已收盘 / preopen 未开市(展示前一交易日) / error 获取失败
    返回 {code, name, status, status_text, prev_date, bars:{open, high, low, latest, close}}
    """
    try:
        rt = fetch_realtime(code5)
        klines = fetch_daily_kline(code5, limit_days=15)
    except requests.RequestException:
        return {"code": code5, "name": "", "status": "error",
                "status_text": "行情获取失败", "prev_date": None, "bars": None}

    if not klines:
        return {"code": code5, "name": (rt or {}).get("name", ""), "status": "error",
                "status_text": "暂无行情数据", "prev_date": None, "bars": None}

    name = (rt or {}).get("name", "")
    last = klines[-1]
    last_date = datetime.date.fromisoformat(last["date"])
    today = today_hk()
    now = now_hk()
    minute = now.hour * 60 + now.minute
    base = {"code": code5, "name": name}

    if last_date == today:
        # 今日有日K：仍在交易或已收盘
        if minute >= HK_CLOSE_MINUTE:
            return {**base, "status": "closed", "status_text": f"已收盘 {today:%m-%d}",
                    "prev_date": None,
                    "bars": {"open": last["open"], "high": last["high"],
                             "low": last["low"], "close": last["close"], "latest": last["close"]}}
        latest = (rt or {}).get("price") or last["close"]
        return {**base, "status": "trading", "status_text": f"交易中 {now:%H:%M} (HKT)",
                "prev_date": None,
                "bars": {"open": last["open"], "high": last["high"],
                         "low": last["low"], "close": None, "latest": latest}}
    # 今日无日K：未开市（周末/盘前/节假日），展示前一交易日 OHLC
    return {**base, "status": "preopen",
            "status_text": f"未开市（展示前一交易日 {last_date:%m-%d}）",
            "prev_date": last_date.strftime("%Y-%m-%d"),
            "bars": {"open": last["open"], "high": last["high"],
                     "low": last["low"], "close": last["close"], "latest": last["close"]}}
