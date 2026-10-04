import os
import re
import sqlite3
import datetime
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from dotenv import load_dotenv
from price import fetch_daily_kline

load_dotenv()  # 加载 .env 环境变量

DB_PATH = "hk_monitor.db"

SEARCH_URL = "https://di.hkex.com.hk/di/NSSrchCorp.aspx?src=MAIN&lang=ZH&g_lang=zh-CN"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


class HKDataPipeline:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH)
        self.cursor = self.conn.cursor()
        self.session = requests.Session()
        # 禁用环境/系统代理跟随：流量固定走 mihomo TUN 的"股票数据"直连通道，
        # 不依赖 7897 端口，Clash 任意改配置都不影响数据抓取
        self.session.trust_env = False
        self.session.headers.update(HEADERS)
        self._init_db()

    def _init_db(self):
        """初始化表，增加 tracked_stocks 记录股票首次录入状态"""
        # 记录已追踪股票表
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS tracked_stocks (
                stock_code TEXT PRIMARY KEY,
                first_fetched_at TEXT
            )
        """)
        # DI 披露表
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS di_disclosure (
                hash_id TEXT PRIMARY KEY,
                date_fetched TEXT,
                stock_code TEXT,
                shareholder_name TEXT,
                event_date TEXT,
                shares_change TEXT,
                holding_shares TEXT,
                holding_percent TEXT,
                reason TEXT,
                event_open TEXT,
                event_high TEXT,
                event_low TEXT,
                event_close TEXT
            )
        """)
        self.conn.commit()

    # ---------------- 基础工具 ----------------

    def get_stock_pool(self) -> list:
        """从 .env 中读取股票池"""
        env_stocks = os.getenv("STOCK_POOL", "")
        if not env_stocks:
            return []
        return [s.strip().zfill(5) for s in env_stocks.split(",") if s.strip()]

    def is_new_stock(self, stock_code: str) -> bool:
        """检查是否为新增加的股票"""
        self.cursor.execute("SELECT 1 FROM tracked_stocks WHERE stock_code = ?", (stock_code,))
        return self.cursor.fetchone() is None

    def mark_stock_as_tracked(self, stock_code: str):
        """标记股票已被追踪"""
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.cursor.execute("INSERT OR REPLACE INTO tracked_stocks VALUES (?, ?)", (stock_code, now_str))
        self.conn.commit()

    def _date_parts(self, d: datetime.date):
        """转换为 HKEX DI 下拉框所需的年月日"""
        return {"DD": f"{d.day:02d}", "MM": f"{d.month:02d}", "YYYY": str(d.year)}

    # ---------------- 抓取流程 ----------------

    def fetch_di_data(self, stock_code: str):
        """抓取 DI 披露数据，判断是抓半年还是抓增量"""
        new_flag = self.is_new_stock(stock_code)

        # 1. 判定拉取时间范围：新股票拉取过去 180 天（半年），老股票拉取 3 天增量
        days_to_fetch = 180 if new_flag else 3

        today = datetime.date.today()
        start_date = today - datetime.timedelta(days=days_to_fetch)

        print(f"🔍 处理股票 [{stock_code}] | {'[新股票首次抓取180天数据]' if new_flag else '[日常更新3天数据]'}")

        try:
            # ---- 第一步：搜索公司，获取"所有披露权益通知"页面链接 ----
            detail_url = self._search_company(stock_code, start_date, today)
            if not detail_url:
                print(f"❌ [{stock_code}] 未找到该股票的披露记录入口")
                return

            # ---- 第二步：抓取披露记录列表（处理分页） ----
            page_url = detail_url
            total_new = 0
            seen_pages = set()
            while page_url and page_url not in seen_pages:
                seen_pages.add(page_url)
                fetched, next_url = self._fetch_list_page(page_url, stock_code)
                total_new += fetched
                page_url = next_url

            print(f"✅ [{stock_code}] 共写入 {total_new} 条新披露记录")

            # ---- 第三步：为变动日期补全当日 OHLC 行情 ----
            self._fill_event_prices(stock_code)

            # 只有成功抓取到披露列表（即使全部已存在）才标记为已追踪
            if new_flag:
                self.mark_stock_as_tracked(stock_code)

        except Exception as e:
            print(f"❌ [{stock_code}] 抓取失败: {e}")

    def _search_company(self, stock_code: str, start_date, end_date) -> str:
        """第一步：POST 搜索公司，从结果中提取'所有披露权益通知'链接"""
        res = self.session.get(SEARCH_URL, timeout=20)
        soup = BeautifulSoup(res.text, "html.parser")

        def input_value(fid):
            el = soup.find("input", {"id": fid})
            return el.get("value", "") if el else ""

        form_data = {
            "__VIEWSTATE": input_value("__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": input_value("__VIEWSTATEGENERATOR"),
            "__EVENTVALIDATION": input_value("__EVENTVALIDATION"),
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "txtStockCode": stock_code,
            "txtCorpName": "",
            **{f"ddlStartDate{k}": v for k, v in self._date_parts(start_date).items()},
            **{f"ddlEndDate{k}": v for k, v in self._date_parts(end_date).items()},
            "cmdSearch": "Search",
        }

        res_post = self.session.post(SEARCH_URL, data=form_data, timeout=25)
        soup_post = BeautifulSoup(res_post.text, "html.parser")

        # 结果列表中查找"所有披露权益通知"链接
        for a in soup_post.find_all("a"):
            href = a.get("href", "")
            if "NSAllFormList.aspx" in href:
                return urljoin(SEARCH_URL, href)
        return None

    def _fetch_list_page(self, page_url: str, stock_code: str):
        """第二步：抓取单页披露列表，返回 (新增条数, 下一页URL或None)"""
        res = self.session.get(page_url, timeout=25)
        soup = BeautifulSoup(res.text, "html.parser")

        table = soup.find("table", {"id": "grdPaging"})
        if not table:
            return 0, None

        today_str = datetime.date.today().strftime("%Y-%m-%d")
        inserted = 0

        for tr in table.find_all("tr"):
            cols = [td.get_text(strip=True) for td in tr.find_all("td")]
            # 数据行结构: 0=表格序号 1=名称 2=原因 3=买入/卖出数量 4=均价 5=持有权益股份数 6=百分比 7=事件日期 ...
            if len(cols) < 8 or not cols[0] or not cols[1]:
                continue
            if not re.match(r"^[A-Z]{2}\d{8}[A-Z]\d+$", cols[0]):
                continue  # 表格序号格式校验，跳过表头/非数据行

            form_no = cols[0]
            shareholder_name = cols[1]
            reason = cols[2]            # 做出披露的原因（HKEX 事件性质代码）
            shares_change = cols[3]     # 买入/出售或涉及股份的数目
            holding_shares = cols[5]    # 持有权益的股份数目
            holding_percent = cols[6]   # 占已发行的有投票权股份百分比
            event_date = cols[7]        # 有关事件的日期（变动日期）

            # 唯一摘要标识：表格序号全局唯一，防重最可靠
            hash_id = f"{stock_code}_{form_no}"

            try:
                self.cursor.execute("""
                    INSERT INTO di_disclosure
                    (hash_id, date_fetched, stock_code, shareholder_name, event_date,
                     shares_change, holding_shares, holding_percent, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (hash_id, today_str, stock_code, shareholder_name, event_date,
                      shares_change, holding_shares, holding_percent, reason))
                inserted += 1
            except sqlite3.IntegrityError:
                pass  # 已存在则跳过

        self.conn.commit()

        # 解析下一页链接：找出所有分页数字链接，取大于当前页的最小页码
        # 从"頁 <N>"标签获取当前页
        current_page = 1
        m = re.search(r"頁\s*<(\d+)>", soup.get_text())
        if m:
            current_page = int(m.group(1))

        next_url = None
        for a in soup.find_all("a"):
            href = a.get("href", "")
            text = a.get_text(strip=True)
            if "NSAllFormList.aspx" in href and text.isdigit():
                page_num = int(text)
                if page_num > current_page:
                    # 当前实现单页最多 50 条，180 天数据一般 1-2 页，直接取第一个更大的页即可
                    next_url = urljoin(page_url, href)
                    break

        return inserted, next_url

    def _fill_event_prices(self, stock_code: str):
        """为该股票已入库记录的每个变动日期，从行情源补全当日开/高/低/收"""
        self.cursor.execute("""
            SELECT DISTINCT event_date FROM di_disclosure
            WHERE stock_code = ? AND event_date IS NOT NULL AND event_date != ''
        """, (stock_code,))
        dates = [r[0] for r in self.cursor.fetchall()]
        if not dates:
            return

        try:
            klines = fetch_daily_kline(stock_code, limit_days=220)  # 覆盖半年追溯范围
        except Exception as e:
            print(f"⚠️ [{stock_code}] 行情日线获取失败: {e}")
            return

        price_map = {}
        for row in klines:
            # 入库 event_date 为 DD/MM/YYYY，与行情 ISO 日期互转
            iso_date = datetime.date.fromisoformat(row["date"])
            price_map[iso_date.strftime("%d/%m/%Y")] = row

        filled = 0
        for ev in dates:
            row = price_map.get(ev)
            if not row:
                continue
            self.cursor.execute("""
                UPDATE di_disclosure
                SET event_open = ?, event_high = ?, event_low = ?, event_close = ?
                WHERE stock_code = ? AND event_date = ?
            """, (row["open"], row["high"], row["low"], row["close"], stock_code, ev))
            filled += 1
        self.conn.commit()
        print(f"📈 [{stock_code}] 已为 {filled}/{len(dates)} 个变动日期补全当日 OHLC 行情")

    def run_sync(self):
        stocks = self.get_stock_pool()
        print(f"🚀 开始同步环境变量中的股票列表: {stocks}")
        for stock in stocks:
            self.fetch_di_data(stock)
        self.conn.close()


if __name__ == "__main__":
    pipeline = HKDataPipeline()
    pipeline.run_sync()
