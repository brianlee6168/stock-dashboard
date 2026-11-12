# -*- coding: utf-8 -*-
"""
選股分析儀表板：抓資料，產生 dashboard.html

用法：
    python dashboard.py            抓真實資料，產生 dashboard.html 並自動打開
    python dashboard.py --demo     用假資料測試版面（不連網）
    python dashboard.py --no-open  產生後不要自動打開瀏覽器（給排程用）
    python dashboard.py --no-open --public --out site/index.html
                                   給 GitHub Actions 用：輸出到 site 資料夾，並隱藏加碼金等個人數字

需要先安裝套件（只要做一次）：
    pip install requests pandas yfinance
"""
import sys, os, io, re, json, random, time, webbrowser, pathlib
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
    ("00919",  "群益台灣精選高息", "ETF"),
    ("00918",  "大華優利高填息30", "ETF"),
    ("00406A", "主動中信台灣收益", "ETF"),
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

# 「我的策略」頁的參數。計算方式請對照你的策略文件，不符合的話再告訴我改。
STRATEGY = {
    "target": "0050",          # 套用策略的標的
    "reserve": 40000,          # 目前加碼金
    "batches_done": 0,         # 已完成批數
    "batches_total": 6,        # 總批數
    "pullback_pct": 5,         # 回檔幾 % 觸發加碼
    "high_mode": "prev_month", # 參考高點 H：prev_month＝前一個日曆月份最高收盤價；lookback＝最近 N 個交易日
    "high_lookback_days": 60,  # high_mode 為 lookback 時才用
    "last_buy_date": "",       # 上次買進日期，例如 "2026-11-03"；沒有就留空
    "max_gap_days": 20,        # 距上次買進的交易日上限
}

# 美國 AI 政策新聞的搜尋關鍵字（Google 新聞）
US_AI_QUERY = '"data center" AI (Trump OR Democrats OR Congress) policy'

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


def fetch_news(code, name, limit=8):
    items = []
    for i in range(3):
        day = (TODAY - dt.timedelta(days=i)).isoformat()
        n = safe(f"{code} FinMind 新聞 {day}", lambda: fm("TaiwanStockNews", code, start=day))
        if n is None or n.empty:
            continue
        for r in n.itertuples():
            items.append({"date": str(r.date)[:16], "title": str(getattr(r, "title", "")).strip(),
                          "source": str(getattr(r, "source", "")), "link": str(getattr(r, "link", ""))})
    items += safe(f"{code} Google 新聞", lambda: gnews(f'"{name}" OR "{code}" when:3d', n=10), []) or []
    seen, out = set(), []
    for it in sorted(items, key=lambda x: x["date"], reverse=True):
        key = re.sub(r"\W", "", it["title"])[:30]
        if not it["title"] or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out[:limit]


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


def fetch_macro():
    mc = {}
    mc["twii"] = safe("加權指數", lambda: last_change(yf_hist("^TWII")))
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
    mc["light"] = first_ok("景氣燈號", light_ndc, light_browser, light_manual)
    return mc


def fetch_us_ai_news():
    out = gnews(US_AI_QUERY + " when:3d", n=8, lang="en")
    for o in out:
        o["date"] = o["date"][5:]
    return out


# ---------------------------------------------------------------------
# 策略頁
# ---------------------------------------------------------------------
def build_strategy(price_df):
    st = dict(STRATEGY)
    if price_df is None or price_df.empty:
        return st
    c = price_df.set_index("date")["close"]
    if st.get("high_mode") == "prev_month":
        first_this = TODAY.replace(day=1)
        first_prev = (first_this - dt.timedelta(days=1)).replace(day=1)
        look = c[(c.index >= first_prev.isoformat()) & (c.index < first_this.isoformat())]
        st["H_label"] = f"{first_prev.year} 年 {first_prev.month} 月最高收盤"
    else:
        look = c.tail(int(st["high_lookback_days"]))
        st["H_label"] = f"近 {st['high_lookback_days']} 日最高收盤"
    if look.empty:
        return st
    H = float(look.max())
    trig = H * (1 - st["pullback_pct"] / 100)
    st.update({"H": num(H, 2), "H_date": look.idxmax(), "trigger": num(trig, 2),
               "close": num(c.iloc[-1], 2), "close_date": c.index[-1],
               "gap_pct": num((c.iloc[-1] / trig - 1) * 100, 2)})
    if "--public" in sys.argv:
        st["reserve"] = st["batches_done"] = None
        st["public"] = True
    if st.get("last_buy_date"):
        st["days_since"] = int((c.index > st["last_buy_date"]).sum())
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
    df = demo_price({"0050": 60, "00919": 22, "00918": 24, "00406A": 10,
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
                  "source": "示範來源", "link": ""} for i in range(5)]
    return s


# ---------------------------------------------------------------------
# 主程式
# ---------------------------------------------------------------------
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
        data["us_ai_news"] = [{"date": "11-11 08:00", "title": f"（示範）美國 AI 資料中心政策新聞 {i + 1}",
                               "source": "示範來源", "link": ""} for i in range(5)]
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
        log("抓取美國 AI 政策新聞……")
        data["us_ai_news"] = safe("美國 AI 新聞", fetch_us_ai_news, [])

    data["strategy"] = build_strategy(strat_df)
    data["warnings"] = WARN

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
