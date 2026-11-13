# -*- coding: utf-8 -*-
"""
選股分析：抓資料，產生 dashboard.html

用法：
    python dashboard.py            抓真實資料，產生 dashboard.html 並自動打開
    python dashboard.py --demo     用假資料測試版面（不連網）
    python dashboard.py --no-open  產生後不要自動打開瀏覽器（給排程用）
    python dashboard.py --no-open --public --out site/index.html
                                   給 GitHub Actions 用：輸出到 site 資料夾，並隱藏加碼金等個人數字

需要先安裝套件（只要做一次）：
    pip install requests pandas yfinance
"""
import sys, os, io, re, json, random, time, webbrowser, pathlib, hashlib
import datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import pandas as pd
import requests

# =====================================================================
# 設定區：這一段可以自己改
# =====================================================================

# FinMind 金鑰：到 https://finmindtrade.com 免費註冊後貼在引號裡。
# 空白也能用，只是每小時可呼叫次數比較少。
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN") or ""   # 在自己電腦跑才需要把 token 貼在最後的引號裡

STOCKS = [
    ("0050",   "元大台灣50",       "ETF"),
    ("006208", "富邦台50",         "ETF"),
    ("00919",  "群益台灣精選高息", "ETF"),
    ("00918",  "大華優利高填息30", "ETF"),
    ("00406A", "主動中信台灣收益", "ETF"),
    ("00981A", "主動統一台股增長", "ETF"),
    ("2330",   "台積電",           "個股"),
    ("2454",   "聯發科",           "個股"),
    ("2360",   "致茂",             "個股"),
]

# 以下都是「自動抓失敗時」才會用的備用值，平常留空即可
MANUAL = {
    "景氣燈號": "",        # 例如 "黃紅燈 35 分"
    "美國CPI年增率": "",   # 自動抓不到時才用，例如 "2.9%"
    "非農新增就業": "",    # 自動抓不到時才用，例如 "+11.9 萬人"
}

# 「我的策略」新增策略時的預設參數。每個人可以替任何一檔已收錄的標的新增策略，各自調整。
STRATEGY = {
    "target": "0050",            # 舊資料轉換時預設的標的
    "base": 40000,               # 起算加碼金（元）
    "monthly_deposit": 15000,    # 每月 5 號入帳（元）
    "deposit_start": "",         # 留空＝新增策略後的下一個 5 號開始入帳
    "batch_pct": 15,             # 每批金額 ＝ 本輪開始時的加碼金餘額 × 15%
    "pullback_pct": 5,           # 第 1–6 批：比參考價再跌 5% 觸發
    "final_pullback_pct": 10,    # 第 7 批（最後加碼）：比第 6 批成交價再跌 10% 觸發
    "normal_batches": 6,         # 一般批數；加上最後加碼共 7 批
    "reset_day": 5,              # 每月 5 號重置加碼次數
    "high_days": 20,             # H ＝ 最近 20 個交易日的最高收盤
    "gap_reset_days": 20,        # 上一批成交後滿 20 個交易日都沒觸發，參考價改回 H
}

# 「我的策略」帳號登入（Firebase）。把 Firebase 網頁設定貼進來就會啟用；留空則維持存在瀏覽器裡的方式。
# 這些設定本來就是公開的（網頁一定要讀得到），安全性靠 Firestore 安全規則控管，可以放在公開 repo。
FIREBASE = {
    "apiKey": "AIzaSyCgE1_IKtv9qp6bmpR9Qk0SVis0OVx4DaI",
    "authDomain": "stock-dashboard-734a2.firebaseapp.com",
    "projectId": "stock-dashboard-734a2",
    "appId": "1:232203792223:web:d17e3551997dd6baa4d0ea",
}
ADMIN_USERNAME = "admin"            # 管理員帳號名稱

# 還沒設定 Firebase 時的「前端測試帳號」：格式 帳號:密碼,帳號:密碼
# 建議改放在 GitHub Secrets 的 LOCAL_USERS，就不會出現在公開的程式碼裡。
# 注意：這只是簡單的門檻，不是真正的安全登入；資料仍存在各自的瀏覽器裡。
LOCAL_USERS = os.environ.get("LOCAL_USERS") or "1234:1234,5678:5678"
LOCAL_SALT = "stock-analysis-v1"
USER_EMAIL_DOMAIN = "users.example.com"   # 帳號會在背後轉成 帳號@這個網域，使用者只需要輸入帳號
MAX_USERS = 10                      # 管理員以外，最多幾個使用者

# 新聞比對用的別名：標題裡出現代號、名稱或別名，才算是這檔的新聞
ALIASES = {
    "0050": ["元大台灣50", "元大台灣卓越50"],
    "006208": ["富邦台50", "富邦台灣50"],
    "00919": ["群益台灣精選高息", "群益精選高息"],
    "00918": ["大華優利高填息30", "大華優利高填息"],
    "00406A": ["主動中信台灣收益", "中信台灣收益"],
    "00981A": ["主動統一台股增長", "統一台股增長"],
    "2330": ["台積電", "TSMC"],
    "2454": ["聯發科", "MediaTek"],
    "2360": ["致茂", "Chroma"],
}

# 只採用這些媒體的新聞（名稱包含其中任一個字串即可）
TRUSTED_SOURCES = ["中央社", "中央通訊社", "經濟日報", "工商時報", "鉅亨", "Anue", "自由", "聯合新聞網", "聯合報",
                   "Yahoo", "MoneyDJ", "財訊", "商業周刊", "商周", "天下", "數位時代", "今周刊", "ETtoday",
                   "中時", "中國時報", "風傳媒", "TVBS", "三立", "科技新報", "財經新報", "鏡週刊", "鏡報",
                   "NOWnews", "CMoney", "民視", "公視", "非凡", "東森", "華視", "台視", "中視", "Smart",
                   "理財周刊", "動區", "INSIDE", "iThome", "DIGITIMES", "MoneyDJ理財網"]

# 美國相關新聞（台灣媒體，Google 新聞中文搜尋）：分頁名稱 → 搜尋關鍵字
US_TOPICS = [
    ("AI", "(美國 OR 美) (AI OR 人工智慧 OR 輝達 OR OpenAI OR 資料中心 OR 晶片)"),
    ("川普", "川普"),
    ("經濟", "(聯準會 OR 美國經濟 OR 美股 OR 美債 OR 通膨 OR 非農 OR 關稅)"),
    ("熱門", "美國"),
]

# 台灣財經媒體 RSS（每次執行抓一次，用來補充個股與美國新聞）
TW_RSS = [
    ("中央社", "https://feeds.feedburner.com/rsscna/finance"),
    ("經濟日報", "https://money.udn.com/rssfeed/news/1001/5591?ch=money"),
    ("經濟日報", "https://money.udn.com/rssfeed/news/1001/10846?ch=money"),
    ("自由財經", "https://news.ltn.com.tw/rss/business.xml"),
    ("Yahoo股市", "https://tw.news.yahoo.com/rss/finance"),
    ("ETtoday財經", "https://feeds.feedburner.com/ettoday/finance"),
    ("財經新報", "https://finance.technews.tw/feed/"),
]

# AI 解讀（Google Gemini 免費 API）。金鑰放在 GitHub Secrets 的 GEMINI_API_KEY；
# 在自己電腦跑可以把金鑰貼在最後的引號裡。沒有金鑰時只顯示規則整理。
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or ""
# 依你的免費額度排序：Flash Lite 系列每天 500 次，最適合；Gemma 4 額度也大；2.5 Flash 每天只有 20 次放最後。
# 程式會先向 Google 查詢你的金鑰實際能用的模型名稱，再依這個順序挑選。
GEMINI_PREFER = ["3.5-flash-lite", "3.1-flash-lite", "gemma-4-31b", "2.5-flash-lite", "gemma-4-26b", "2.5-flash"]
GEMINI_MODELS = []   # 執行時自動填入

OUTPUT = "dashboard.html"

# =====================================================================
# 以下是程式本體，一般不用改
# =====================================================================

FM_API = "https://api.finmindtrade.com/api/v4/data"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TW = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime.now(TW)
TODAY = NOW.date()
WARN = []


def log(msg):
    print(msg, flush=True)


def safe(label, fn, default=None):
    """執行一段抓資料的程式，失敗時記下來但不中斷整支程式。"""
    try:
        return fn()
    except Exception as e:
        WARN.append(f"{label}：{e}")
        log(f"  ⚠ {label} 失敗：{e}")
        return default


def days_ago(n):
    return (TODAY - dt.timedelta(days=n)).isoformat()


def fm(dataset, data_id=None, start=None, end=None):
    params = {"dataset": dataset}
    if data_id:
        params["data_id"] = data_id
    if start:
        params["start_date"] = start
    if end:
        params["end_date"] = end
    headers = dict(UA)
    if FINMIND_TOKEN:
        headers["Authorization"] = f"Bearer {FINMIND_TOKEN}"
    r = requests.get(FM_API, params=params, headers=headers, timeout=30)
    j = r.json()
    if j.get("status") not in (200, None):
        raise RuntimeError(j.get("msg", f"HTTP {r.status_code}"))
    return pd.DataFrame(j.get("data", []))


def num(v, nd=4):
    try:
        if v is None or pd.isna(v):
            return None
        return round(float(v), nd)
    except Exception:
        return None


def to_list(series, nd=4):
    return [num(v, nd) for v in series]


# ---------------------------------------------------------------------
# 技術指標
# ---------------------------------------------------------------------
def indicators(df):
    """df 欄位：date, open, max, min, close, vol（張）。回傳各指標序列。"""
    c, h, l = df["close"], df["max"], df["min"]
    ma = lambda n: c.rolling(n).mean()
    ma20 = ma(20)
    sd = c.rolling(20).std(ddof=0)

    low9, high9 = l.rolling(9).min(), h.rolling(9).max()
    rng = (high9 - low9).replace(0, pd.NA)
    rsv = ((c - low9) / rng * 100).astype("float").fillna(50)
    K, D, k, d = [], [], 50.0, 50.0
    for i, v in enumerate(rsv):
        if i < 8:
            K.append(None); D.append(None); continue
        k = k * 2 / 3 + v / 3
        d = d * 2 / 3 + k / 3
        K.append(round(k, 2)); D.append(round(d, 2))

    delta = c.diff()
    gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
    ag = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    al = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rsi = 100 - 100 / (1 + ag / al)

    dif = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    sig = dif.ewm(span=9, adjust=False).mean()

    return {
        "ma5": to_list(ma(5), 2), "ma20": to_list(ma20, 2), "ma60": to_list(ma(60), 2),
        "bbu": to_list(ma20 + 2 * sd, 2), "bbl": to_list(ma20 - 2 * sd, 2),
        "k": K, "d": D, "rsi": to_list(rsi, 2),
        "dif": to_list(dif, 3), "macd": to_list(sig, 3), "osc": to_list(dif - sig, 3),
    }


def build_tech(df, show=120):
    ind = indicators(df)
    tail = slice(-show, None)
    out = {"ohlc": [[r.date, num(r.open, 2), num(r.max, 2), num(r.min, 2), num(r.close, 2), num(r.vol, 0)]
                    for r in df.iloc[tail].itertuples()]}
    for key, vals in ind.items():
        out[key] = vals[tail]
    return out


# ---------------------------------------------------------------------
# 個股／ETF 資料
# ---------------------------------------------------------------------
def fetch_price(code):
    p = fm("TaiwanStockPrice", code, start=days_ago(420))
    if p.empty:
        raise RuntimeError("沒有股價資料")
    p = p.sort_values("date").reset_index(drop=True)
    p["vol"] = p["Trading_Volume"] / 1000
    return p[["date", "open", "max", "min", "close", "vol", "spread"]]


def fetch_fund(code, kind):
    f = {}
    per = safe(f"{code} 本益比", lambda: fm("TaiwanStockPER", code, start=days_ago(20)))
    if per is not None and not per.empty:
        r = per.sort_values("date").iloc[-1]
        f["per"] = {"date": r["date"], "per": num(r.get("PER"), 2), "pbr": num(r.get("PBR"), 2),
                    "yield": num(r.get("dividend_yield"), 2)}
    if kind != "個股":
        return f

    rev = safe(f"{code} 月營收", lambda: fm("TaiwanStockMonthRevenue", code, start=days_ago(800)))
    if rev is not None and not rev.empty:
        m = {(int(r.revenue_year), int(r.revenue_month)): float(r.revenue) for r in rev.itertuples()}
        keys = sorted(m)[-6:]
        rows = []
        for (y, mo) in reversed(keys):
            prev_y = m.get((y - 1, mo))
            prev_m = m.get((y, mo - 1) if mo > 1 else (y - 1, 12))
            rows.append({"ym": f"{y}-{mo:02d}", "rev": round(m[(y, mo)] / 1e8, 2),
                         "yoy": num((m[(y, mo)] / prev_y - 1) * 100, 1) if prev_y else None,
                         "mom": num((m[(y, mo)] / prev_m - 1) * 100, 1) if prev_m else None})
        f["revenue"] = rows

    fs = safe(f"{code} 財報", lambda: fm("TaiwanStockFinancialStatements", code, start=days_ago(800)))
    if fs is not None and not fs.empty:
        pv = fs.pivot_table(index="date", columns="type", values="value", aggfunc="last").sort_index()
        q = []
        for d, r in pv.tail(4).iloc[::-1].iterrows():
            gm = None
            if "GrossProfit" in r and "Revenue" in r and r["Revenue"]:
                gm = num(r["GrossProfit"] / r["Revenue"] * 100, 1)
            q.append({"date": d, "eps": num(r.get("EPS"), 2), "gm": gm})
        f["quarters"] = q
    return f


def fetch_chips(code):
    ch = {}
    inst = safe(f"{code} 三大法人", lambda: fm("TaiwanStockInstitutionalInvestorsBuySell", code, start=days_ago(30)))
    if inst is not None and not inst.empty:
        inst["net"] = inst["buy"] - inst["sell"]
        groups = {"外資": ["Foreign_Investor", "Foreign_Dealer_Self"], "投信": ["Investment_Trust"],
                  "自營商": ["Dealer_self", "Dealer_Hedging", "Dealer"]}
        last = inst["date"].max()
        t = inst[inst["date"] == last]
        rows = []
        for g, names in groups.items():
            s = t[t["name"].isin(names)]
            rows.append({"name": g, "buy": num(s["buy"].sum() / 1000, 0),
                         "sell": num(s["sell"].sum() / 1000, 0), "net": num(s["net"].sum() / 1000, 0)})
        fo = inst[inst["name"].isin(groups["外資"])].groupby("date")["net"].sum().sort_index().tail(10)
        ch["inst"] = {"date": last, "rows": rows, "foreign10": [[d, num(v / 1000, 0)] for d, v in fo.items()]}

    m = safe(f"{code} 融資融券", lambda: fm("TaiwanStockMarginPurchaseShortSale", code, start=days_ago(20)))
    if m is not None and not m.empty:
        r = m.sort_values("date").iloc[-1]
        mb, ss = float(r["MarginPurchaseTodayBalance"]), float(r["ShortSaleTodayBalance"])
        ch["margin"] = {"date": r["date"], "mb": mb, "mb_chg": mb - float(r["MarginPurchaseYesterdayBalance"]),
                        "ss": ss, "ss_chg": ss - float(r["ShortSaleYesterdayBalance"]),
                        "ratio": num(ss / mb * 100, 2) if mb else None}

    s = safe(f"{code} 借券", lambda: fm("TaiwanDailyShortSaleBalances", code, start=days_ago(20)))
    if s is not None and not s.empty:
        s = s.sort_values("date")
        cur = float(s.iloc[-1]["SBLShortSalesCurrentDayBalance"])
        prev = float(s.iloc[-1]["SBLShortSalesPreviousDayBalance"])
        ch["sbl"] = {"date": s.iloc[-1]["date"], "bal": num(cur / 1000, 0), "chg": num((cur - prev) / 1000, 0)}

    sh = safe(f"{code} 外資持股", lambda: fm("TaiwanStockShareholding", code, start=days_ago(20)))
    if sh is not None and not sh.empty:
        sh = sh.sort_values("date")
        v = float(sh.iloc[-1]["ForeignInvestmentSharesRatio"])
        p = float(sh.iloc[-2]["ForeignInvestmentSharesRatio"]) if len(sh) > 1 else None
        ch["foreign_ratio"] = {"date": sh.iloc[-1]["date"], "val": num(v, 2),
                               "chg": num(v - p, 2) if p is not None else None}
    return ch


def gnews(query, n=8, lang="zh"):
    """Google 新聞 RSS 搜尋，只取標題、來源、時間、連結。"""
    loc = {"zh": {"hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"},
           "en": {"hl": "en-US", "gl": "US", "ceid": "US:en"}}[lang]
    r = get_retry("https://news.google.com/rss/search", params={"q": query, **loc})
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        when = it.findtext("pubDate") or ""
        try:
            when = parsedate_to_datetime(when).astimezone(TW).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
        title = (it.findtext("title") or "").strip()
        src = (it.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        out.append({"date": when, "title": title, "source": src, "link": it.findtext("link") or ""})
        if len(out) >= n:
            break
    return out


def trusted(src):
    return any(t.lower() in (src or "").lower() for t in TRUSTED_SOURCES)


def _bigrams(t):
    t = re.sub(r"[\W_]+", "", (t or "").lower())
    return {t[i:i + 2] for i in range(len(t) - 1)}


def group_news(items, limit):
    """把不同媒體報導的同一則新聞合併，記錄有幾家媒體報導，作為交叉驗證。"""
    groups = []
    for it in sorted(items, key=lambda x: x["date"], reverse=True):
        bg = _bigrams(it["title"])
        if len(bg) < 4:
            continue
        hit = None
        for g in groups:
            inter = len(bg & g["_bg"])
            if inter / max(1, min(len(bg), len(g["_bg"]))) >= 0.6:
                hit = g
                break
        if hit:
            if it["source"] and it["source"] not in hit["sources"]:
                hit["sources"].append(it["source"])
        else:
            groups.append({**it, "sources": [it["source"]] if it["source"] else [], "_bg": bg})
    for g in groups:
        g.pop("_bg", None)
        g["n"] = len(g["sources"])
    return groups[:limit]


_RSS_CACHE = None


def fetch_tw_rss():
    """抓台灣財經媒體的 RSS，整次執行只抓一次。"""
    global _RSS_CACHE
    if _RSS_CACHE is not None:
        return _RSS_CACHE
    out = []
    for name, url in TW_RSS:
        try:
            root = ET.fromstring(get_retry(url, tries=2).content)
        except Exception as e:
            log(f"  （RSS {name} 沒抓到：{str(e)[:50]}）")
            continue
        for it in root.iter("item"):
            when = it.findtext("pubDate") or ""
            try:
                when = parsedate_to_datetime(when).astimezone(TW).strftime("%Y-%m-%d %H:%M")
            except Exception:
                continue
            out.append({"date": when, "title": (it.findtext("title") or "").strip(),
                        "source": name, "link": (it.findtext("link") or "").strip()})
    _RSS_CACHE = out
    return out


def cnyes_news(q, n=20):
    """鉅亨網公開的新聞關鍵字搜尋。"""
    r = requests.get("https://ess.api.cnyes.com/ess/api/v1/news/keyword",
                     params={"q": q, "limit": n, "page": 1},
                     headers={**UA, "Origin": "https://news.cnyes.com", "Referer": "https://news.cnyes.com/"}, timeout=20)
    r.raise_for_status()
    found = []

    def walk(x):
        if isinstance(x, dict):
            if "title" in x and ("newsId" in x or "id" in x):
                found.append(x)
            else:
                for v in x.values():
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(r.json())
    out = []
    for d in found:
        ts = d.get("publishAt") or d.get("publish_at")
        try:
            when = dt.datetime.fromtimestamp(int(ts), TW).strftime("%Y-%m-%d %H:%M")
        except Exception:
            continue
        nid = d.get("newsId") or d.get("id")
        out.append({"date": when, "title": re.sub(r"<[^>]+>", "", str(d.get("title", ""))).strip(),
                    "source": "鉅亨網", "link": f"https://news.cnyes.com/news/id/{nid}",
                    "tags": [str(t) for t in (d.get("stock") or [])]})
    return out


def mentions(title, code, name):
    """標題是否真的提到這檔：代號（前後不能接數字）、名稱或別名。"""
    t = title or ""
    if re.search(rf"(?<![0-9A-Za-z]){re.escape(code)}(?![0-9A-Za-z])", t, re.I):
        return True
    return any(a.lower() in t.lower() for a in [name] + ALIASES.get(code, []))


def fetch_news(code, name, limit=10):
    """多個來源合併：FinMind、鉅亨網、Google 新聞、台灣財經媒體 RSS。
    驗證方式：只留標題確實提到這檔的新聞、只留可信媒體、只留近 3 天，並標出幾家媒體同時報導。"""
    items = []
    for i in range(3):
        day = (TODAY - dt.timedelta(days=i)).isoformat()
        n = safe(f"{code} FinMind 新聞 {day}", lambda: fm("TaiwanStockNews", code, start=day))
        if n is None or n.empty:
            continue
        for r in n.itertuples():
            items.append({"date": str(r.date)[:16], "title": str(getattr(r, "title", "")).strip(),
                          "source": str(getattr(r, "source", "")), "link": str(getattr(r, "link", ""))})
    for q in dict.fromkeys([name, code]):
        for it in safe(f"{code} 鉅亨網新聞（{q}）", lambda: cnyes_news(q), []) or []:
            if mentions(it["title"], code, name) or code in it.get("tags", []):
                items.append(it)
    items += safe(f"{code} Google 新聞", lambda: gnews(f'"{name}" OR "{code}" when:3d', n=15), []) or []
    items += fetch_tw_rss()

    since = (NOW - dt.timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    ok = [it for it in items if it["title"] and it["date"] >= since
          and (it["source"] == "鉅亨網" or mentions(it["title"], code, name))
          and (trusted(it["source"]) or it["source"] == "")]
    return group_news(ok, limit)


def roc_date(sv):
    """民國日期 1151112 → 2026-11-12"""
    sv = re.sub(r"\D", "", str(sv or ""))
    if len(sv) < 7:
        return str(sv)
    return f"{int(sv[:-4]) + 1911}-{sv[-4:-2]}-{sv[-2:]}"


def fetch_announcements(codes):
    """證交所 OpenAPI：上市公司每日重大訊息（只有最近一個交易日）。"""
    rows = get_retry("https://openapi.twse.com.tw/v1/opendata/t187ap04_L").json()
    out = {}
    for row in rows:
        row = {str(k).strip(): ("" if v is None else str(v).strip()) for k, v in row.items()}
        c = row.get("公司代號")
        if c not in codes:
            continue
        t = re.sub(r"\D", "", row.get("發言時間", ""))
        out.setdefault(c, []).append({
            "date": roc_date(row.get("發言日期")) + (f" {t[:2]}:{t[2:4]}" if len(t) >= 4 else ""),
            "title": row.get("主旨", ""), "rule": row.get("符合條款", ""),
            "desc": row.get("說明", "")[:400]})
    return out


def fetch_dividends(code, close):
    d = {}
    dr = safe(f"{code} 除息紀錄", lambda: fm("TaiwanStockDividendResult", code, start=days_ago(800)))
    if dr is not None and not dr.empty:
        dr = dr.sort_values("date", ascending=False)
        rows = []
        for r in dr.itertuples():
            amt, before = float(r.stock_and_cache_dividend), float(r.before_price)
            rows.append({"date": r.date, "amt": num(amt, 3), "before": num(before, 2),
                         "pct": num(amt / before * 100, 2) if before else None})
        d["history"] = rows[:8]
        ttm = sum(x["amt"] or 0 for x in rows if x["date"] >= days_ago(365))
        d["ttm"] = num(ttm, 3)
        if close:
            d["ttm_yield"] = num(ttm / close * 100, 2)
    dv = safe(f"{code} 股利公告", lambda: fm("TaiwanStockDividend", code, start=days_ago(240)))
    if dv is not None and not dv.empty:
        up = []
        for r in dv.itertuples():
            ex = str(getattr(r, "CashExDividendTradingDate", "") or "")
            if ex and ex >= TODAY.isoformat():
                cash = float(getattr(r, "CashEarningsDistribution", 0) or 0) + float(getattr(r, "CashStatutorySurplus", 0) or 0)
                up.append({"ex": ex, "cash": num(cash, 3), "pay": str(getattr(r, "CashDividendPaymentDate", "") or "")})
        if up:
            d["upcoming"] = sorted(up, key=lambda x: x["ex"])
    return d


def fetch_holdings(code):
    """MoneyDJ ETF 持股明細。網頁格式若改版會抓不到，頁面會顯示「—」。"""
    url = f"https://www.moneydj.com/ETF/X/Basic/Basic0007.xdjhtm?etfid={code.lower()}.tw"
    html = get_retry(url).content.decode("utf-8", errors="replace")
    date = None
    i = html.find("持股明細")
    m = re.search(r"資料日期[：:]\s*(\d{4}/\d{1,2}/\d{1,2})", html[i:] if i >= 0 else html)
    if m:
        date = m.group(1).replace("/", "-")
    rows = []
    for t in pd.read_html(io.StringIO(html)):
        cols = [str(c) for c in t.columns]
        nc = next((c for c in cols if "名稱" in c), None)
        wc = next((c for c in cols if "比例" in c), None)
        if not nc or not wc or "產業" in "".join(cols) or "區域" in "".join(cols):
            continue
        t.columns = cols
        for _, r in t.iterrows():
            nm = str(r[nc]).strip()
            w = pd.to_numeric(str(r[wc]).replace("%", ""), errors="coerce")
            if not nm or pd.isna(w):
                continue
            mm = re.match(r"(.*?)\((\w+)\.TW\)", nm)
            rows.append({"name": (mm.group(1) if mm else nm).strip(), "code": mm.group(2) if mm else nm,
                         "w": round(float(w), 2)})
        if rows:
            break
    if not rows:
        raise RuntimeError("網頁上找不到持股表格")
    rows.sort(key=lambda x: -x["w"])
    return {"date": date, "rows": rows}


# ---------------------------------------------------------------------
# 總經、匯率、美國新聞
# ---------------------------------------------------------------------
def yf_hist(ticker, period="3mo"):
    import yfinance as yf
    h = yf.Ticker(ticker).history(period=period)
    if h.empty:
        raise RuntimeError("沒有資料")
    return h["Close"].dropna()


def last_change(s, nd=2):
    v, p = float(s.iloc[-1]), float(s.iloc[-2])
    return {"value": num(v, nd), "chg": num(v - p, nd), "pct": num((v / p - 1) * 100, 2),
            "date": s.index[-1].strftime("%Y-%m-%d")}


def get_retry(url, tries=3, **kw):
    """連線被中斷時，等幾秒重試。"""
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=30, **kw)
            r.raise_for_status()
            return r
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def first_ok(label, *fns):
    """依序嘗試多個資料來源，第一個成功的就用。"""
    errs = []
    for fn in fns:
        try:
            return fn()
        except Exception as e:
            errs.append(str(e)[:80])
    WARN.append(f"{label}：" + "；".join(errs))
    log(f"  ⚠ {label} 所有來源都失敗：" + "；".join(errs))
    return None


def fred(series_id):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    df = pd.read_csv(io.StringIO(get_retry(url).text))
    df.columns = ["date", "value"]
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna()


def parse_ndc(j):
    """國發會 JSON：line 裡找「景氣對策信號」且單位是分數的那一條。"""
    for ser in (j or {}).get("line", {}).values():
        if ser.get("name") == "景氣對策信號" and "分" in (ser.get("unit") or ""):
            pts = sorted([p for p in ser.get("data", []) if p.get("y") is not None], key=lambda p: str(p["x"]))
            sc = float(pts[-1]["y"])
            lt = "藍燈" if sc <= 16 else "黃藍燈" if sc <= 22 else "綠燈" if sc <= 31 else "黃紅燈" if sc <= 37 else "紅燈"
            x = str(pts[-1]["x"])
            return {"value": f"{lt} {sc:g} 分", "score": sc, "date": f"{x[:4]}-{x[4:6]}",
                    "hist": [[f"{str(p['x'])[:4]}-{str(p['x'])[4:6]}", float(p["y"])] for p in pts[-6:]]}
    raise RuntimeError("回傳資料裡找不到景氣對策信號分數")


_PREV = None


def prev_page_data():
    """讀取目前已經發佈在 GitHub Pages 上的上一版頁面資料，當作抓不到時的備援。"""
    global _PREV
    if _PREV is not None:
        return _PREV
    _PREV = {}
    url = os.environ.get("PAGES_URL", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")          # GitHub Actions 會自動提供，例如 brianlee6168/stock-dashboard
    if not url and "/" in repo:
        owner, name = repo.split("/", 1)
        url = f"https://{owner.lower()}.github.io/{name}/"
    if not url:
        return _PREV
    try:
        html = get_retry(url, tries=2).text
        m = re.search(r"const D = (\{.*?\});\nconst TABS", html, re.S)
        if m:
            _PREV = json.loads(m.group(1))
    except Exception as e:
        log(f"  （讀取上一版頁面失敗：{str(e)[:60]}）")
    return _PREV


def fetch_macro():
    mc = {}
    def twii_finmind():
        df = fm("TaiwanStockPrice", "TAIEX", start=days_ago(30))
        if df.empty or len(df) < 2:
            raise RuntimeError("FinMind 沒有加權指數資料")
        df = df.sort_values("date")
        v, p = float(df["close"].iloc[-1]), float(df["close"].iloc[-2])
        return {"value": num(v, 2), "chg": num(v - p, 2), "pct": num((v / p - 1) * 100, 2),
                "date": df["date"].iloc[-1], "src": "證交所（FinMind）"}

    def twii_yahoo():
        out = last_change(yf_hist("^TWII"))
        out["src"] = "Yahoo"
        return out

    # 兩個來源都抓，用日期比較新的那一個；Yahoo 偶爾會在早上回傳前一天的舊資料
    cands = [x for x in (safe("加權指數（FinMind）", twii_finmind), safe("加權指數（Yahoo）", twii_yahoo)) if x]
    mc["twii"] = max(cands, key=lambda x: (x["date"], x["src"] != "Yahoo")) if cands else None
    mc["sox"] = safe("費城半導體", lambda: last_change(yf_hist("^SOX")))

    def fx():
        s = yf_hist("TWD=X")
        out = last_change(s, 3)
        out["series"] = [num(v, 3) for v in s.tail(30)]
        return out
    mc["fx"] = safe("USD/TWD", fx)

    def fed_fred():
        lo, hi = fred("DFEDTARL").iloc[-1], fred("DFEDTARU").iloc[-1]
        return {"value": f"{lo['value']:.2f}–{hi['value']:.2f}%", "date": hi["date"]}

    def fed_finmind():
        df = fm("InterestRate", "FED", start=days_ago(800)).sort_values("date")
        col = [c for c in df.columns if "rate" in c.lower()][0]
        return {"value": f"{float(df[col].iloc[-1]):.2f}%", "date": df["date"].iloc[-1]}
    mc["fed"] = first_ok("聯準會利率", fed_fred, fed_finmind)

    def cpi_fred():
        df = fred("CPIAUCSL")
        yoy = (df["value"].iloc[-1] / df["value"].iloc[-13] - 1) * 100
        return {"value": f"{yoy:.1f}%", "date": df["date"].iloc[-1][:7]}

    def cpi_bls(ver="v2"):
        j = get_retry(f"https://api.bls.gov/publicAPI/{ver}/timeseries/data/CUUR0000SA0").json()
        if j.get("status") != "REQUEST_SUCCEEDED":
            raise RuntimeError(" ".join(j.get("message") or ["BLS 拒絕請求"]))
        rows = {}
        for d in j["Results"]["series"][0]["data"]:
            if not d["period"].startswith("M") or d["period"] == "M13":
                continue
            try:
                rows[(int(d["year"]), d["period"])] = float(d["value"])
            except ValueError:
                continue  # 某些月份官方沒公布，值是 "-"，跳過
        for (y, p) in sorted(rows, reverse=True):
            if (y - 1, p) in rows:
                yoy = (rows[(y, p)] / rows[(y - 1, p)] - 1) * 100
                return {"value": f"{yoy:.1f}%", "date": f"{y}-{p[1:]}"}
        raise RuntimeError("BLS 資料不足以計算年增率")
    def cpi_manual():
        if not MANUAL.get("美國CPI年增率"):
            raise RuntimeError("MANUAL 裡的美國CPI年增率沒有填")
        return {"value": MANUAL["美國CPI年增率"], "date": "手動填寫"}
    mc["cpi"] = first_ok("美國 CPI", cpi_fred, cpi_bls, lambda: cpi_bls("v1"), cpi_manual)

    def us10_fred():
        df = fred("DGS10")
        v, p = df["value"].iloc[-1], df["value"].iloc[-2]
        return {"value": num(v, 2), "chg": num(v - p, 2), "date": df["date"].iloc[-1]}

    def us10_yahoo():
        s = yf_hist("^TNX")
        s = s / 10 if s.iloc[-1] > 20 else s
        return last_change(s, 2)
    mc["us10y"] = first_ok("美債 10 年", us10_fred, us10_yahoo)

    mc["vix"] = safe("VIX 恐慌指數", lambda: last_change(yf_hist("^VIX")))

    def nfp_fred():
        df = fred("PAYEMS")
        chg = df["value"].iloc[-1] - df["value"].iloc[-2]
        return {"value": f"{chg / 10:+.1f} 萬人", "chg_raw": num(chg, 0), "date": df["date"].iloc[-1][:7]}

    def nfp_bls(ver="v2"):
        j = get_retry(f"https://api.bls.gov/publicAPI/{ver}/timeseries/data/CES0000000001").json()
        if j.get("status") != "REQUEST_SUCCEEDED":
            raise RuntimeError(" ".join(j.get("message") or ["BLS 拒絕請求"]))
        rows = {}
        for d in j["Results"]["series"][0]["data"]:
            if not d["period"].startswith("M") or d["period"] == "M13":
                continue
            try:
                rows[(int(d["year"]), int(d["period"][1:]))] = float(d["value"])
            except ValueError:
                continue  # 官方沒公布的月份是 "-"，跳過
        for (y, m) in sorted(rows, reverse=True):
            prev = (y, m - 1) if m > 1 else (y - 1, 12)
            if prev in rows:
                chg = rows[(y, m)] - rows[prev]
                return {"value": f"{chg / 10:+.1f} 萬人", "chg_raw": num(chg, 0), "date": f"{y}-{m:02d}"}
        raise RuntimeError("BLS 資料不足以計算")

    def nfp_manual():
        if not MANUAL.get("非農新增就業"):
            raise RuntimeError("MANUAL 裡的非農新增就業沒有填")
        return {"value": MANUAL["非農新增就業"], "date": "手動填寫"}
    mc["nfp"] = first_ok("非農就業", nfp_fred, nfp_bls, lambda: nfp_bls("v1"), nfp_manual)

    def light_ndc():
        from urllib.parse import unquote
        base = "https://index.ndc.gov.tw"
        api = base + "/n/json/data/eco/indicators"
        pages = [base + "/n/zh_tw/data/eco", base + "/n/zh_tw/data/eco/signal", base + "/n/zh_tw"]
        errs = []
        for page in pages:
            ss = requests.Session()
            ss.headers.update({**UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                               "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
            try:
                r = ss.get(page, timeout=30)
            except Exception as e:
                errs.append(f"{page[len(base):]} 連線失敗 {str(e)[:60]}")
                continue
            if r.status_code != 200:
                errs.append(f"{page[len(base):]} HTTP {r.status_code}")
                continue
            html = r.text
            m = (re.search(r'<meta[^>]*name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)', html)
                 or re.search(r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']csrf-token', html)
                 or re.search(r'csrf[-_]?token["\']?\s*[:=]\s*["\']([A-Za-z0-9+/=_-]{20,})', html, re.I))
            xsrf = ss.cookies.get("XSRF-TOKEN")
            if not m and not xsrf:
                head = re.sub(r"\s+", " ", html[:120])
                errs.append(f"{page[len(base):]} 沒有 token（{len(html)} 字，開頭：{head}）")
                continue
            hdr = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*",
                   "Referer": page, "Origin": base}
            if m:
                hdr["X-CSRF-TOKEN"] = m.group(1)
            if xsrf:
                hdr["X-XSRF-TOKEN"] = unquote(xsrf)
            resp = ss.post(api, headers=hdr, timeout=60)
            if resp.status_code != 200:
                errs.append(f"POST HTTP {resp.status_code}")
                continue
            try:
                return parse_ndc(resp.json())
            except Exception as e:
                errs.append(str(e))
        raise RuntimeError("國發會：" + "；".join(errs))

    def light_browser():
        """用真的瀏覽器打開國發會網頁，攔截網頁自己去要的資料。需要先安裝 playwright。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("沒有安裝 playwright（GitHub Actions 會自動安裝；自己電腦要用請看說明）")
        got = []
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page(locale="zh-TW", user_agent=UA["User-Agent"])

            def on_resp(r):
                if "/n/json/data/eco/indicators" in r.url and r.status == 200:
                    try:
                        got.append(r.json())
                    except Exception:
                        pass
            pg.on("response", on_resp)
            pg.goto("https://index.ndc.gov.tw/n/zh_tw/data/eco", wait_until="domcontentloaded", timeout=60000)
            for _ in range(40):
                if got:
                    break
                pg.wait_for_timeout(500)
            title = pg.title()
            b.close()
        if not got:
            raise RuntimeError(f"瀏覽器打開了國發會網頁，但沒有等到資料（網頁標題：{title}）")
        return parse_ndc(got[0])

    def light_manual():
        if not MANUAL.get("景氣燈號"):
            raise RuntimeError("MANUAL 裡的景氣燈號沒有填")
        return {"value": MANUAL["景氣燈號"], "date": "手動填寫"}
    def light_prev():
        old = (prev_page_data().get("macro") or {}).get("light")
        if not isinstance(old, dict) or old.get("score") is None:
            raise RuntimeError("上一版頁面也沒有景氣燈號")
        return {**old, "stale": True}

    mc["light"] = first_ok("景氣燈號", light_ndc, light_browser, light_prev, light_manual)
    if (mc["light"] or {}).get("stale"):
        log("  景氣燈號：國發會這次擋住連線，沿用上一次成功抓到的數值（燈號一個月才更新一次）")

    # 每月才更新的總經數字，全部來源都失敗時沿用上一版頁面的數值
    for k in ("fed", "cpi", "nfp"):
        if not mc.get(k):
            old = (prev_page_data().get("macro") or {}).get(k)
            if old:
                mc[k] = {**old, "stale": True}
                log(f"  {k}：沿用上一次成功抓到的數值")
    return mc


US_KEYWORDS = {
    "AI": ["AI", "人工智慧", "輝達", "NVIDIA", "OpenAI", "資料中心", "晶片"],
    "川普": ["川普", "Trump"],
    "經濟": ["聯準會", "Fed", "美國經濟", "美股", "美債", "通膨", "非農", "關稅", "降息", "升息"],
    "熱門": [],
}


def fetch_us_news():
    """台灣媒體報導的美國相關新聞，依主題分組。"""
    rss_us = [it for it in fetch_tw_rss() if re.search(r"美國|美股|川普|聯準會|白宮|華爾街|美債|美元", it["title"])]
    since = (NOW - dt.timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    topics, used = [], set()
    for name, q in US_TOPICS:
        items = safe(f"美國新聞（{name}）", lambda: gnews(f"{q} when:2d", n=25), []) or []
        kws = US_KEYWORDS.get(name, [])
        items += [it for it in rss_us if not kws or any(k.lower() in it["title"].lower() for k in kws)]
        items = [it for it in items if it["date"] >= since and trusted(it["source"])]
        grouped = [g for g in group_news(items, 20) if g["title"] not in used][:6]
        used.update(g["title"] for g in grouped)
        topics.append({"name": name, "items": grouped})
    return {"topics": topics}


# ---------------------------------------------------------------------
# 策略頁
# ---------------------------------------------------------------------
def build_strategy(price_df=None):
    """策略預設值；實際計算在網頁上，依每個人自己新增的策略進行。"""
    st = dict(STRATEGY)
    if "--public" in sys.argv:          # 公開網頁不放金額
        st["base"] = st["monthly_deposit"] = None
        st["public"] = True
    return st


# ---------------------------------------------------------------------
# 示範資料（--demo）
# ---------------------------------------------------------------------
def demo_price(base):
    dates = pd.bdate_range(end=TODAY, periods=280)
    p, rows = base, []
    for d in dates:
        o = p * (1 + random.gauss(0, 0.006))
        c = o * (1 + random.gauss(0.0006, 0.013))
        rows.append([d.strftime("%Y-%m-%d"), o, max(o, c) * (1 + abs(random.gauss(0, .004))),
                     min(o, c) * (1 - abs(random.gauss(0, .004))), c, random.randint(8000, 40000), c - p])
        p = c
    return pd.DataFrame(rows, columns=["date", "open", "max", "min", "close", "vol", "spread"])


def demo_stock(code, name, kind):
    df = demo_price({"0050": 60, "006208": 140, "00981A": 25, "00919": 22, "00918": 24, "00406A": 10,
                     "2330": 1400, "2454": 1300, "2360": 480}.get(code, 100))
    s = {"code": code, "name": name, "kind": kind, "tech": build_tech(df), "_df": df}
    last = df.iloc[-1]
    s["quote"] = {"date": last.date, "close": num(last.close, 2), "chg": num(last.spread, 2),
                  "pct": num(last.spread / (last.close - last.spread) * 100, 2), "vol": num(last.vol, 0)}
    s["fund"] = {"per": {"date": last.date, "per": 18.4, "pbr": 4.1, "yield": 2.3}}
    if kind == "個股":
        s["fund"]["revenue"] = [{"ym": f"2026-{m:02d}", "rev": round(random.uniform(800, 1100), 2),
                                 "yoy": round(random.uniform(-5, 40), 1), "mom": round(random.uniform(-10, 10), 1)}
                                for m in range(10, 4, -1)]
        s["fund"]["quarters"] = [{"date": f"2026-{m:02d}-01", "eps": round(random.uniform(10, 18), 2),
                                  "gm": round(random.uniform(50, 60), 1)} for m in (7, 4, 1)]
    s["chips"] = {
        "inst": {"date": last.date, "rows": [{"name": n, "buy": 12000, "sell": 9000, "net": random.randint(-5000, 5000)}
                                             for n in ("外資", "投信", "自營商")],
                 "foreign10": [[d, random.randint(-8000, 8000)] for d in df.date.tail(10)]},
        "margin": {"date": last.date, "mb": 25000, "mb_chg": 320, "ss": 400, "ss_chg": -12, "ratio": 1.6},
        "sbl": {"date": last.date, "bal": 41000, "chg": -800},
        "foreign_ratio": {"date": last.date, "val": 72.31, "chg": 0.05},
    }
    s["fund"]["div"] = {"ttm": 2.4, "ttm_yield": 3.2, "history": [{"date": f"2026-{m:02d}-15", "amt": 0.6, "before": 70.0, "pct": 0.86} for m in (10, 7, 4, 1)],
                        "upcoming": [{"ex": "2026-12-18", "cash": 0.65, "pay": "2027-01-15"}]}
    if kind == "ETF":
        s["fund"]["holdings"] = {"date": "2026-10-31", "rows": [{"name": n, "code": c, "w": w} for n, c, w in
                                 [("台積電", "2330", 56.4), ("聯發科", "2454", 6.7), ("台達電", "2308", 3.4), ("鴻海", "2317", 2.9),
                                  ("日月光投控", "3711", 2.2), ("聯電", "2303", 1.7), ("欣興", "3037", 1.4), ("台光電", "2383", 1.3),
                                  ("南亞", "1303", 1.2), ("富邦金", "2881", 1.2), ("致茂", "2360", 0.7)]]}
    else:
        s["announce"] = [{"date": f"{last.date} 17:30", "title": "（示範）公告本公司董事會決議股利分派", "rule": "第14款", "desc": "（示範）說明文字"}]
    s["news"] = [{"date": f"{last.date} 1{i}:00", "title": f"（示範）{name} 相關新聞標題 {i + 1}",
                  "source": "鉅亨網", "link": "", "sources": ["鉅亨網", "經濟日報", "中央社"][: 1 + i % 3], "n": 1 + i % 3} for i in range(5)]
    return s


# ---------------------------------------------------------------------
# 主程式
# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# 綜合分析：規則整理
# ---------------------------------------------------------------------
def _last(a, k=1):
    vals = [v for v in (a or []) if v is not None]
    return vals[-k] if len(vals) >= k else None


def build_analysis(s, macro, strategy):
    """把各面向的數字整理成白話重點。tone: up 偏強、down 偏弱、mid 中性。只描述現況，不做預測。"""
    A = {"tech": [], "chips": [], "fund": [], "news": [], "macro": [], "strategy": []}
    add = lambda k, tone, text: A[k].append({"tone": tone, "text": text})
    t, q = s.get("tech") or {}, s.get("quote") or {}
    c = q.get("close")

    # ---- 技術面 ----
    if t.get("ohlc") and c:
        ma20, ma60, ma5 = _last(t["ma20"]), _last(t["ma60"]), _last(t["ma5"])
        if ma20:
            add("tech", "up" if c >= ma20 else "down",
                f"收盤 {c:g} 在 20 日均線（{ma20:g}）之{'上' if c >= ma20 else '下'}，差距 {(c / ma20 - 1) * 100:+.1f}%。")
        if ma60:
            add("tech", "up" if c >= ma60 else "down", f"收盤在 60 日均線（{ma60:g}）之{'上' if c >= ma60 else '下'}。")
        m5, m20 = [v for v in t["ma5"][-4:]], [v for v in t["ma20"][-4:]]
        if all(v is not None for v in m5 + m20):
            if m5[0] < m20[0] and m5[-1] >= m20[-1]:
                add("tech", "up", "近 3 日 5 日均線由下往上穿過 20 日均線（黃金交叉）。")
            elif m5[0] > m20[0] and m5[-1] <= m20[-1]:
                add("tech", "down", "近 3 日 5 日均線由上往下穿過 20 日均線（死亡交叉）。")
        k, d, k0, d0 = _last(t["k"]), _last(t["d"]), _last(t["k"], 2), _last(t["d"], 2)
        if k is not None and d is not None:
            zone = "高檔區（80 以上）" if k >= 80 else "低檔區（20 以下）" if k <= 20 else "中間區"
            cross = ""
            if k0 is not None and d0 is not None:
                if k0 < d0 and k >= d:
                    cross = "，今天 K 向上穿過 D（黃金交叉）"
                elif k0 > d0 and k <= d:
                    cross = "，今天 K 向下穿過 D（死亡交叉）"
            add("tech", "up" if k >= d else "down", f"KD 為 {k:.0f} / {d:.0f}，位於{zone}{cross}。")
        r = _last(t["rsi"])
        if r is not None:
            add("tech", "mid" if 30 < r < 70 else ("up" if r >= 70 else "down"),
                f"RSI {r:.0f}，" + ("超過 70，屬短線偏熱區。" if r >= 70 else "低於 30，屬短線偏冷區。" if r <= 30 else "在 30–70 的中間區。"))
        o1, o0 = _last(t["osc"]), _last(t["osc"], 2)
        if o1 is not None and o0 is not None:
            side = "0 軸上方" if o1 >= 0 else "0 軸下方"
            trend = "變長" if abs(o1) > abs(o0) else "縮短"
            add("tech", "up" if o1 >= 0 else "down", f"MACD 柱狀體在{side}且{trend}，代表{'多' if o1 >= 0 else '空'}方動能{'增強' if trend == '變長' else '減弱'}。")
        bu, bl = _last(t["bbu"]), _last(t["bbl"])
        if bu and bl:
            if c > bu:
                add("tech", "up", f"收盤高於布林上軌（{bu:g}），短線漲幅較大。")
            elif c < bl:
                add("tech", "down", f"收盤低於布林下軌（{bl:g}），短線跌幅較大。")
            widths = [(u - l) / m for u, l, m in zip(t["bbu"], t["bbl"], t["ma20"]) if u and l and m]
            if len(widths) > 40 and widths[-1] <= min(widths[-120:]) * 1.05:
                add("tech", "mid", "布林通道寬度接近近期最窄，代表波動收斂，之後波動可能放大（方向不確定）。")
        vols = [r[5] for r in t["ohlc"][-21:] if r[5]]
        if len(vols) >= 21:
            ratio = vols[-1] / (sum(vols[:-1]) / 20)
            if ratio >= 1.5 or ratio <= 0.6:
                add("tech", "mid", f"今日成交量是近 20 日均量的 {ratio:.1f} 倍，{'明顯放量' if ratio >= 1.5 else '明顯量縮'}。")
        closes = [r[4] for r in t["ohlc"]]
        if len(closes) > 20:
            chg20 = (closes[-1] / closes[-21] - 1) * 100
            add("tech", "up" if chg20 >= 0 else "down", f"近 20 個交易日漲跌 {chg20:+.1f}%。")

    # ---- 籌碼面 ----
    ch = s.get("chips") or {}
    ins = ch.get("inst")
    if ins:
        f10 = [v for _, v in ins.get("foreign10", []) if v is not None]
        if f10:
            sign = 1 if f10[-1] > 0 else -1 if f10[-1] < 0 else 0
            streak = 0
            for v in reversed(f10):
                if sign and (v > 0) == (sign > 0) and v != 0:
                    streak += 1
                else:
                    break
            tot = sum(f10)
            if sign:
                add("chips", "up" if sign > 0 else "down", f"外資連續 {streak} 日{'買超' if sign > 0 else '賣超'}，近 {len(f10)} 日合計{'買' if tot >= 0 else '賣'}超 {abs(tot):,.0f} 張。")
        it = next((r for r in ins.get("rows", []) if r["name"] == "投信"), None)
        if it and it.get("net"):
            add("chips", "up" if it["net"] > 0 else "down", f"投信今日{'買' if it['net'] > 0 else '賣'}超 {abs(it['net']):,.0f} 張。")
    m = ch.get("margin")
    if m and m.get("mb"):
        pct = m["mb_chg"] / (m["mb"] - m["mb_chg"]) * 100 if m["mb"] != m["mb_chg"] else 0
        if abs(pct) >= 1:
            add("chips", "mid", f"融資餘額{'增加' if pct > 0 else '減少'} {abs(pct):.1f}%（{m['mb_chg']:+,.0f} 張），散戶槓桿部位{'上升' if pct > 0 else '下降'}。")
        if m.get("ratio") and m["ratio"] >= 20:
            add("chips", "mid", f"券資比 {m['ratio']:.1f}%，放空比例偏高。")
    b = ch.get("sbl")
    if b and b.get("chg"):
        add("chips", "down" if b["chg"] > 0 else "up", f"借券賣出餘額{'增加' if b['chg'] > 0 else '減少'} {abs(b['chg']):,.0f} 張。")
    fr = ch.get("foreign_ratio")
    if fr and fr.get("val") is not None:
        add("chips", "mid", f"外資持股比例 {fr['val']:.2f}%" + (f"（{fr['chg']:+.2f}）" if fr.get("chg") else "") + "。")

    # ---- 基本面 ----
    fu = s.get("fund") or {}
    dv = fu.get("div") or {}
    if s["kind"] == "個股":
        rev = fu.get("revenue") or []
        if rev and rev[0].get("yoy") is not None:
            y = rev[0]["yoy"]
            n = 0
            for r in rev:
                if r.get("yoy") is not None and (r["yoy"] > 0) == (y > 0):
                    n += 1
                else:
                    break
            add("fund", "up" if y > 0 else "down", f"{rev[0]['ym']} 月營收年增 {y:+.1f}%，已連續 {n} 個月{'成長' if y > 0 else '衰退'}（表內最多 6 個月）。")
        per = (fu.get("per") or {}).get("per")
        if per:
            add("fund", "mid", f"本益比 {per:g} 倍。")
        qs = fu.get("quarters") or []
        if len(qs) >= 2 and qs[0].get("gm") is not None and qs[1].get("gm") is not None:
            dg = qs[0]["gm"] - qs[1]["gm"]
            add("fund", "up" if dg >= 0 else "down", f"最新一季毛利率 {qs[0]['gm']:.1f}%，比前一季{'增加' if dg >= 0 else '減少'} {abs(dg):.1f} 個百分點。")
    else:
        if dv.get("ttm_yield") is not None:
            add("fund", "mid", f"近 12 個月殖利率 {dv['ttm_yield']:.2f}%（配息合計 {dv.get('ttm', 0):g} 元）。")
        rows = (fu.get("holdings") or {}).get("rows") or []
        if rows:
            top5 = sum(r["w"] for r in rows[:5])
            add("fund", "mid", f"前五大持股合計 {top5:.1f}%，最大持股 {rows[0]['name']} 占 {rows[0]['w']:.1f}%。")
            if rows[0]["w"] >= 40:
                add("fund", "down", f"單一持股超過 40%，走勢高度受 {rows[0]['name']} 影響。")
        ov = sorted(fu.get("overlap") or [], key=lambda x: -x["pct"])
        if ov and ov[0]["pct"] >= 30:
            add("fund", "mid", f"和 {ov[0]['code']} 的持股重疊度 {ov[0]['pct']:.0f}%，兩檔分散效果有限。")
    for u in dv.get("upcoming") or []:
        days = (dt.date.fromisoformat(u["ex"]) - TODAY).days
        if 0 <= days <= 30:
            add("fund", "mid", f"{u['ex']} 除息（{days} 天後），每單位 {u['cash']:g} 元；除息日股價會機械性下調，計算策略高點時要留意。")

    # ---- 消息面 ----
    if s.get("announce"):
        add("news", "mid", f"最近一個交易日有 {len(s['announce'])} 則重大訊息：" + "；".join(a["title"][:30] for a in s["announce"][:2]) + "。")
    n = len(s.get("news") or [])
    add("news", "mid", f"近 3 天有 {n} 則相關新聞（標題見消息面）。" if n else "近 3 天沒有找到相關新聞。")

    # ---- 總經背景（每檔相同）----
    v = (macro.get("vix") or {}).get("value")
    if v is not None:
        add("macro", "down" if v >= 30 else "mid" if v >= 20 else "up",
            f"VIX {v:.1f}，" + ("30 以上，市場明顯恐慌。" if v >= 30 else "20–30 之間，市場偏緊張。" if v >= 20 else "20 以下，市場相對平靜。"))
    u10 = (macro.get("us10y") or {}).get("value")
    if u10 is not None:
        add("macro", "down" if u10 >= 4.8 else "mid", f"美債 10 年殖利率 {u10:.2f}%" + ("，處於偏高水準，對股票評價較不利。" if u10 >= 4.8 else "。"))
    lt = macro.get("light")
    if isinstance(lt, dict) and lt.get("score") is not None:
        add("macro", "up" if lt["score"] >= 32 else "down" if lt["score"] <= 22 else "mid", f"台灣景氣燈號 {lt['value']}（{lt['date']}）。")
    fx = macro.get("fx")
    if fx and fx.get("series"):
        ser = [x for x in fx["series"] if x]
        if len(ser) > 5:
            ch30 = (ser[-1] / ser[0] - 1) * 100
            add("macro", "mid", f"新台幣近 30 天{'貶值' if ch30 > 0 else '升值'} {abs(ch30):.1f}%（USD/TWD {ser[-1]:.2f}）。")

    up = sum(1 for k in ("tech", "chips") for x in A[k] if x["tone"] == "up")
    dn = sum(1 for k in ("tech", "chips") for x in A[k] if x["tone"] == "down")
    A["summary"] = f"技術面與籌碼面共 {up} 項偏強、{dn} 項偏弱。"
    return A


def pick_gemini_models():
    """查詢這把金鑰能用的模型，依 GEMINI_PREFER 排出最多 3 個。"""
    names = []
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    token = None
    for _ in range(5):
        r = requests.get(url, headers={"x-goog-api-key": GEMINI_API_KEY},
                         params={"pageSize": 1000, **({"pageToken": token} if token else {})}, timeout=30)
        if r.status_code != 200:
            try:
                msg = r.json().get("error", {}).get("message", "")
            except Exception:
                msg = r.text
            raise RuntimeError(f"查詢模型清單失敗 HTTP {r.status_code}：{msg[:200]}")
        j = r.json()
        for m in j.get("models", []):
            if "generateContent" in (m.get("supportedGenerationMethods") or []):
                names.append(m["name"].split("/", 1)[-1])
        token = j.get("nextPageToken")
        if not token:
            break
    picked = []
    for key in GEMINI_PREFER:
        cands = [n for n in names if key in n and not any(x in n for x in ("tts", "image", "audio", "live", "embedding"))]
        if key == "2.5-flash":
            cands = [n for n in cands if "lite" not in n]
        cands.sort(key=lambda n: ("preview" in n or "exp" in n, len(n)))   # 正式版、名字短的優先
        for n in cands:
            if n not in picked:
                picked.append(n)
                break
        if len(picked) >= 4:
            break
    if not picked:
        raise RuntimeError("金鑰可用的模型裡找不到合適的文字模型：" + ", ".join(names[:15]))
    return picked


# ---------------------------------------------------------------------
# AI 文字的保險：把中文數字轉成阿拉伯數字
# ---------------------------------------------------------------------
_CN_D = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_U = {"十": 10, "百": 100, "千": 1000}
_CN_NUM = "[零〇一二兩三四五六七八九十百千萬]+(?:點[零〇一二三四五六七八九]+)?"
_CN_UNITS = "個百分點|個交易日|個月|日|天|月|年|季|分|倍|張|元|億|檔|次|項|成|週|周"


def cn2num(t):
    """中文數字字串 → 數字；例如 二十 → 20、五十七點三 → 57.3、二〇二六 → 2026。"""
    dec = None
    if "點" in t:
        t, dec = t.split("點", 1)
    if t and all(c in _CN_D for c in t):            # 像 二〇二六 這種逐位數字
        val = int("".join(str(_CN_D[c]) for c in t))
    else:
        total = section = n = 0
        for c in t:
            if c in _CN_D:
                n = _CN_D[c]
            elif c in _CN_U:
                section += (n or 1) * _CN_U[c]
                n = 0
            elif c == "萬":
                total += (section + n) * 10000
                section = n = 0
        val = total + section + n
    if dec:
        return float(f"{val}.{''.join(str(_CN_D[c]) for c in dec)}")
    return val


def _fmt(v):
    return f"{v:g}" if isinstance(v, float) else str(v)


def arabic_numbers(text):
    # 負百分之六點五 → -6.5%
    text = re.sub(rf"(負)?百分之({_CN_NUM})",
                  lambda m: ("-" if m.group(1) else "") + _fmt(cn2num(m.group(2))) + "%", text)

    # 二十日均線 → 20 日均線；六成 → 60%
    def unit_sub(m):
        n, u = m.group(1), m.group(2)
        if (n == "十" and u == "分") or (n == "一" and u in ("次", "項", "分")):   # 「十分強勁」「一次」等慣用語不轉
            return m.group(0)
        v = cn2num(n)
        if u == "成":
            return _fmt(v * 10) + "%"
        return f"{_fmt(v)} {u}"
    text = re.sub(rf"({_CN_NUM})({_CN_UNITS})", unit_sub, text)
    text = text.replace("零軸", "0 軸")
    text = re.sub(r"(\d) (\d)", r"\1\2", text)
    return text


def ai_comment(s, A):
    """把規則整理的重點送給 Gemini，請它寫成一段白話解讀。失敗回傳 None。"""
    if not GEMINI_API_KEY:
        return None
    facts = {k: [x["text"] for x in A[k]] for k in ("tech", "chips", "fund", "news", "macro", "strategy")}
    prompt = (
        "你是協助長期投資人閱讀資料的助理。以下是台股標的 "
        f"{s['code']} {s['name']}（{s['kind']}）今天整理好的客觀數據，JSON 格式：\n"
        + json.dumps(facts, ensure_ascii=False) +
        "\n\n請用繁體中文寫 150 到 250 字的白話解讀，分成三小段，每段開頭用「整體：」「值得留意：」「對長期投資的意義：」。"
        "規則：只能根據上面的資料，不可加入資料以外的事實或新聞；不預測明天或未來漲跌；"
        "不給買進、賣出、加碼、減碼建議，也不提目標價；資料不足就直接說資料不足。"
        "數字一律用阿拉伯數字，並照資料原樣寫出，例如寫「20 日均線」「-6.0%」「年增 57.3%」「41 分」，"
        "不要改寫成中文數字（如二十日、百分之六），也不要改成「近六成」這類約略說法；提到某個指標時要附上它的數值。"
        "這位投資人採定期定額加回檔加碼的長期策略，分析目的是確認有沒有結構性風險，不是短線進出。只輸出文字，不要用 Markdown 符號。"
    )
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048}}
    errs = []
    url = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
    hdr = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    for model in GEMINI_MODELS:
        for attempt in range(3):          # 503 伺服器忙碌、500 內部錯誤通常是暫時的，等一下再試
            try:
                r = requests.post(url.format(model), headers=hdr, json=body, timeout=60)
            except Exception as e:
                errs.append(f"{model} 連線失敗 {str(e)[:60]}")
                break
            if r.status_code == 200:
                try:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    text = arabic_numbers(re.sub(r"[*#`]+", "", "".join(p.get("text", "") for p in parts).strip()))
                except Exception as e:
                    errs.append(f"{model} 回傳格式異常 {str(e)[:60]}")
                    break
                if text:
                    return {"text": text, "model": model}
                errs.append(f"{model} 回傳空白")
                break
            try:
                msg = r.json().get("error", {}).get("message", "")
            except Exception:
                msg = r.text
            msg = re.sub(r"\s+", " ", msg)[:160]
            if r.status_code in (500, 502, 503, 504) and attempt < 2:
                time.sleep(10 * (attempt + 1))   # 等 10 秒、20 秒
                continue
            if r.status_code == 429 and "limit: 0" not in msg and attempt < 1:
                time.sleep(20)                    # 每分鐘次數超過，等一下再試一次
                continue
            errs.append(f"{model} HTTP {r.status_code}：{msg}")
            break
    WARN.append(f"{s['code']} AI 解讀：" + "；".join(errs))
    log(f"  ⚠ {s['code']} AI 解讀失敗：" + "；".join(errs))
    return None


def add_analysis(data):
    global GEMINI_API_KEY
    if GEMINI_API_KEY and not data.get("demo"):
        models = safe("Gemini 模型清單", pick_gemini_models)
        if models:
            GEMINI_MODELS[:] = models
            log("  AI 模型：" + "、".join(models))
        else:
            GEMINI_API_KEY = ""   # 查不到模型就不呼叫 AI，只顯示規則整理
            data["ai_error"] = True
    for i, s in enumerate(data["stocks"]):
        s["analysis"] = build_analysis(s, data.get("macro") or {}, data.get("strategy") or {})
        if not data.get("demo"):
            s["ai"] = ai_comment(s, s["analysis"])
            if GEMINI_API_KEY and i < len(data["stocks"]) - 1:
                time.sleep(5)   # 免費額度每分鐘最多 15 次，間隔 5 秒＝每分鐘最多 12 次
    data["ai_enabled"] = bool(GEMINI_API_KEY) or bool(data.get("ai_error"))


def add_overlap(stocks):
    """ETF 之間的持股重疊度：共同持股取兩邊權重較小者加總。"""
    hs = {x["code"]: {r["code"]: r["w"] for r in x["fund"]["holdings"]["rows"]}
          for x in stocks if (x.get("fund") or {}).get("holdings")}
    for x in stocks:
        if x["code"] not in hs:
            continue
        a = hs[x["code"]]
        x["fund"]["overlap"] = [{"code": c, "pct": round(sum(min(a[k], b[k]) for k in a.keys() & b.keys()), 1)}
                                for c, b in hs.items() if c != x["code"]]


def main():
    demo = "--demo" in sys.argv
    data = {"demo": demo, "generated": NOW.strftime("%Y-%m-%d %H:%M"), "stocks": []}
    strat_df = None

    if demo:
        log("產生示範資料……")
        for code, name, kind in STOCKS:
            s = demo_stock(code, name, kind)
            if code == STRATEGY["target"]:
                strat_df = s["_df"]
            s.pop("_df")
            data["stocks"].append(s)
        add_overlap(data["stocks"])
        data["macro"] = {"twii": {"value": 27850.12, "chg": 152.3, "pct": 0.55, "date": TODAY.isoformat()},
                         "sox": {"value": 7012.4, "chg": -48.2, "pct": -0.68, "date": TODAY.isoformat()},
                         "fx": {"value": 31.42, "chg": -0.05, "pct": -0.16, "date": TODAY.isoformat(),
                                "series": [round(31.6 + random.gauss(0, .12), 3) for _ in range(30)]},
                         "fed": {"value": "3.75–4.00%", "date": "示範"}, "cpi": {"value": "2.9%", "date": "示範"},
                         "us10y": {"value": 4.12, "chg": 0.03, "date": "示範"},
                         "vix": {"value": 18.42, "chg": 1.35, "pct": 7.91, "date": TODAY.isoformat()},
                         "nfp": {"value": "+11.9 萬人", "chg_raw": 119, "date": "示範"},
                         "light": {"value": "黃紅燈 35 分", "score": 35, "date": "示範",
                                   "hist": [["2026-04", 33], ["2026-05", 34], ["2026-06", 36], ["2026-07", 35], ["2026-08", 37], ["2026-09", 35]]}}
        data["us_news"] = {"topics": [{"name": t, "items": [{"date": "2026-11-12 08:00", "title": f"（示範）{t} 相關新聞 {i + 1}",
                                                              "source": "中央社", "link": "", "sources": ["中央社", "經濟日報"][: 1 + i % 2], "n": 1 + i % 2}
                                                             for i in range(4)]} for t, _ in US_TOPICS]}
    else:
        for code, name, kind in STOCKS:
            log(f"抓取 {code} {name}……")
            s = {"code": code, "name": name, "kind": kind}
            df = safe(f"{code} 股價", lambda: fetch_price(code))
            if df is not None:
                s["tech"] = build_tech(df)
                last = df.iloc[-1]
                prev_close = float(last.close - last.spread)
                s["quote"] = {"date": last.date, "close": num(last.close, 2), "chg": num(last.spread, 2),
                              "pct": num(last.spread / prev_close * 100, 2) if prev_close else None,
                              "vol": num(last.vol, 0)}
                if code == STRATEGY["target"]:
                    strat_df = df
            s["fund"] = fetch_fund(code, kind)
            s["fund"]["div"] = fetch_dividends(code, s.get("quote", {}).get("close"))
            if kind == "ETF":
                s["fund"]["holdings"] = safe(f"{code} ETF 持股", lambda: fetch_holdings(code))
            s["chips"] = fetch_chips(code)
            s["news"] = fetch_news(code, name)
            data["stocks"].append(s)
        log("抓取重大訊息……")
        ann = safe("重大訊息", lambda: fetch_announcements({c for c, _, k in STOCKS if k == "個股"}), {}) or {}
        for s in data["stocks"]:
            if s["kind"] == "個股":
                s["announce"] = ann.get(s["code"], [])
        add_overlap(data["stocks"])
        log("抓取總經與匯率……")
        data["macro"] = fetch_macro()
        log("抓取美國相關新聞……")
        data["us_news"] = safe("美國相關新聞", fetch_us_news, {"topics": []})

    data["strategy"] = build_strategy(strat_df)
    log("整理綜合分析……" + ("（含 AI 解讀）" if GEMINI_API_KEY and not demo else ""))
    add_analysis(data)
    if demo:
        for s in data["stocks"]:
            s["ai"] = {"model": "示範", "text": "整體：（示範）這是 AI 解讀的示範文字，實際內容會由 Gemini 根據上方規則整理的數據撰寫。\n值得留意：（示範）……\n對長期投資的意義：（示範）……"}
        data["ai_enabled"] = True
    data["warnings"] = WARN
    if FIREBASE.get("apiKey") and FIREBASE.get("projectId"):
        data["firebase"] = FIREBASE
        data["auth"] = {"admin": ADMIN_USERNAME, "domain": USER_EMAIL_DOMAIN, "max": MAX_USERS}
    elif LOCAL_USERS.strip():
        # 網頁上只放「帳號＋密碼」的雜湊值，不放明碼
        hs = []
        for pair in LOCAL_USERS.split(","):
            if ":" in pair:
                u, pw = pair.strip().split(":", 1)
                hs.append(hashlib.sha256(f"{LOCAL_SALT}|{u.strip()}|{pw.strip()}".encode()).hexdigest())
        data["local_login"] = {"salt": LOCAL_SALT, "hashes": hs}

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "dashboard_template.html"), encoding="utf-8") as f:
        tpl = f.read()
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    out_path = os.path.join(here, OUTPUT)
    if "--out" in sys.argv:
        out_path = os.path.abspath(sys.argv[sys.argv.index("--out") + 1])
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(tpl.replace("__DATA__", blob))

    ok = sum(1 for x in data["stocks"] if x.get("tech"))
    log(f"\n股價有抓到的標的：{ok} / {len(data['stocks'])}")
    log(f"完成：{out_path}")
    if WARN:
        log(f"有 {len(WARN)} 項資料沒抓到，頁面上會顯示「—」。")
    if "--no-open" not in sys.argv:
        webbrowser.open(pathlib.Path(out_path).resolve().as_uri())


if __name__ == "__main__":
    main()
