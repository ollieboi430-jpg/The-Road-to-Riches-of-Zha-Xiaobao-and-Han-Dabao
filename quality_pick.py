# -*- coding: utf-8 -*-
"""
资金潜伏板块 · 优质股筛选器  quality_pick.py
============================================================================
策略逻辑（和"追涨停"相反，做资金先于涨停的潜伏）：
  1) 找【主力资金大面积流入、但涨停极少(0~少量)】的行业板块；
  2) 取每个板块的成分股，剔除 ST/停牌/已涨停/亏损/现金流为负/负债过高等一票否决项；
  3) 按"矛(盈利) / 盾(健康) / 股东回报 / 估值 / 资金共振"五维百分制打分；
  4) 每个板块每天选 N 支(默认3支)。

数据可得性原则（不编分）：
  - ROE、毛利率、净利率、营收/净利增速、负债率、经营现金流、股息率、PE/PB/主力资金
    → 有日频/定期公开数据，程序自动算分；
  - 护城河、行业地位(仅给市值排名弱参考)、管理层治理、审计意见、大股东质押/减持、
    分红融资比、注销式回购 → 没有可靠的日频批量数据，【只在结果里标"人工复核"，绝不瞎打分】。

数据源：东方财富(与 sector_fund_flow 同源；push2delay 延时节点 + 多 host 容错，海外可跑)
        涨停池用 akshare ak.stock_zt_pool_em。
依赖：  pip install akshare pandas requests py_mini_racer lxml numpy  (与现有 requirements 一致)
输出：  控制台简报 + quality_pick_候选全量.csv + quality_pick_每日精选.txt（可直接贴进复盘报告）
============================================================================
"""
import os
import sys
import time
import argparse
import datetime as dt
from dataclasses import dataclass

import requests
import pandas as pd

# 复用现有板块资金流（东财同源 + FundLookup 名称容错）
from sector_fund_flow import get_sector_fund_flow, FundLookup

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# 延时节点优先（海外友好，收盘后跑无影响），主站与编号节点兜底
_EM_HOSTS = (["push2delay.eastmoney.com", "push2.eastmoney.com"]
             + [f"{i}.push2.eastmoney.com" for i in range(1, 21)])
_DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# 天然高负债、不套用普通行业负债率红线的行业（金融地产）
_HIGH_DEBT_INDUSTRY = ("银行", "保险", "证券", "信托", "金融", "房地产", "地产", "开发")


# ============================ 可调参数（命令行可覆盖）============================
@dataclass
class Cfg:
    min_inflow_yi: float = 1.5      # 板块主力净流入下限(亿)
    max_zt: int = 0                 # 板块允许的涨停只数（0=必须无涨停，"极少"可设1）
    min_breadth: float = 0.5        # 板块内主力净流入为正的个股占比下限（"大面积流入"）
    top_sectors: int = 8            # 最多入选板块数
    per_sector: int = 3             # 每板块选几支
    min_member_inflow_wan: float = 0.0   # 个股主力净流入下限(万)，>0 要求资金也流入个股
    annual_years: int = 3           # ROE 连续性看最近几个年报
    min_score: float = 50.0         # 入选最低总分，达不到宁缺毋滥（避免小板块硬凑数）
    keep_nodata: bool = False       # 财务缺失是否保留为观察项（默认剔除：无法证明优质）
    date: str = ""                  # 交易日 YYYYMMDD，空=自动取最近交易日
    outdir: str = "."


# ============================ 通用工具 ============================
def _num(x, default=None):
    """东财缺数用 '-' / None，统一安全转 float。"""
    try:
        if x is None or x == "" or x == "-":
            return default
        v = float(x)
        return v if v == v else default  # NaN 判定
    except (TypeError, ValueError):
        return default


def market_suffix(code):
    """6位代码 -> 交易所后缀，与 market_enrich.market_suffix 保持一致。"""
    d = "".join(c for c in str(code) if c.isdigit()).zfill(6)
    if d.startswith(("920", "43", "83", "87", "4", "8")):
        return ".BJ"
    if d.startswith(("5", "6", "9")):
        return ".SH"
    return ".SZ"


def _em_clist(fs, fields, fid="f62", pz=500, timeout=12, tries=2):
    """东财 clist 通用查询，多 host + 重试容错，返回 diff 列表。"""
    last = None
    for host in _EM_HOSTS:
        for _ in range(tries):
            try:
                params = {"pn": 1, "pz": pz, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                          "fid": fid, "fs": fs, "fields": fields}
                r = requests.get(f"https://{host}/api/qt/clist/get", params=params,
                                 headers={"User-Agent": _UA,
                                          "Referer": "https://data.eastmoney.com/"},
                                 timeout=timeout)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                data = (r.json().get("data") or {})
                diff = data.get("diff") or []
                if diff:
                    return diff
                last = "空返回"
            except Exception as e:  # 换下一个 host
                last = e
                time.sleep(0.15)
    raise RuntimeError(f"东财 clist 全部节点失败(fs={fs})，最后错误：{last}")


# ============================ 第1步：板块层（资金流入 + 涨停极少 + 广度）============================
def load_sector_fund(cfg):
    """板块资金流（必须带板块代码，故只用东财/CF，不用无代码的同花顺兜底）。"""
    df = get_sector_fund_flow(sources=("em", "cf"))
    if "板块代码" not in df.columns or df["板块代码"].astype(str).str.len().max() < 4:
        raise RuntimeError("板块资金流缺少有效板块代码，无法取成分股；请确认东财源可用")
    return df


def load_zt(trade_date):
    """当日涨停池：返回 (行业->涨停数, 涨停代码集合)。失败直接报错——本策略前提就是'涨停极少'。"""
    import akshare as ak
    last = None
    for d in [trade_date] + _prev_dates(trade_date, 6):
        try:
            z = ak.stock_zt_pool_em(date=d)
            if z is not None and not z.empty:
                ind = z["所属行业"].fillna("").astype(str).value_counts().to_dict()
                codes = set(str(c).zfill(6) for c in z["代码"])
                print(f"[涨停池] {d}：{len(z)}只涨停")
                return ind, codes, d
        except Exception as e:
            last = e
            continue
    raise RuntimeError(f"涨停池连续多日取不到，无法判定'无涨停'，终止（最后错误：{last}）")


def _prev_dates(yyyymmdd, n):
    d0 = dt.datetime.strptime(yyyymmdd, "%Y%m%d")
    return [(d0 - dt.timedelta(days=i)).strftime("%Y%m%d") for i in range(1, n + 1)]


def latest_trade_date():
    return _now_bj().strftime("%Y%m%d")


def select_sectors(df_fund, zt_industry, lookup, cfg):
    """选出 资金流入大 + 涨停极少 的候选板块（板块广度在取到成分股后再二次过滤）。"""
    out = []
    for _, r in df_fund.iterrows():
        name = str(r["板块名称"]).strip()
        inflow_yi = _num(r.get("主力净流入(亿)"), 0.0)
        ratio = _num(r.get("主力净占比%"), 0.0)
        chg = _num(r.get("涨跌幅%"), 0.0)
        code = str(r.get("板块代码", ""))
        # 该板块对应的涨停数（涨停池行业名 → 资金板块名，三级容错匹配）
        rec = lookup.get(name)
        zt_n = 0
        for zname, cnt in zt_industry.items():
            if lookup.get(zname) is rec and rec is not None:
                zt_n += cnt
        if inflow_yi < cfg.min_inflow_yi:
            continue
        if zt_n > cfg.max_zt:
            continue
        out.append({"板块": name, "代码": code, "主力净流入亿": inflow_yi,
                    "主力净占比%": ratio, "板块涨跌幅%": chg, "涨停数": zt_n})
    out.sort(key=lambda x: -x["主力净流入亿"])
    return out[: cfg.top_sectors]


# ============================ 第2步：板块成分股（一次带出 价/涨跌/PE/PB/资金）============================
_MEMBER_FIELDS = "f12,f14,f2,f3,f8,f9,f20,f23,f62,f115,f184"

def board_members(bk_code):
    diff = _em_clist(fs=f"b:{bk_code}", fields=_MEMBER_FIELDS, fid="f62")
    rows = []
    for x in diff:
        rows.append({
            "code": str(x.get("f12", "")).zfill(6),
            "name": str(x.get("f14", "")),
            "price": _num(x.get("f2")),
            "chg": _num(x.get("f3"), 0.0),          # 涨跌幅%
            "turnover": _num(x.get("f8"), 0.0),     # 换手%
            "pe_ttm": _num(x.get("f115")) or _num(x.get("f9")),  # 优先TTM，退回动态
            "pb": _num(x.get("f23")),
            "mktcap_yi": (_num(x.get("f20"), 0.0) or 0.0) / 1e8,  # 总市值→亿
            "main_in": _num(x.get("f62"), 0.0),     # 主力净流入(元)
            "main_ratio": _num(x.get("f184"), 0.0),
        })
    return rows


def _zt_threshold(code):
    """各板涨停幅度阈值，用于识别个股当日是否已涨停。"""
    if code.startswith(("30", "688")):
        return 19.5      # 创业板/科创板 20%
    if code.startswith(("4", "8", "920")):
        return 29.5      # 北交所 30%
    return 9.8           # 主板 10%


def _is_st(name):
    n = str(name).upper()
    return ("ST" in n) or ("退" in n)


# ============================ 第3步：批量财务（业绩 + 负债 + 历年ROE/增速）============================
def _dc_query(report, columns, filters, page_size=500, timeout=20, single=False):
    """datacenter 通用查询，自动翻页取全；single=True 只取第一页（探测用）。"""
    out, pn = [], 1
    while True:
        p = {"sortColumns": "SECURITY_CODE", "sortTypes": "1", "pageSize": page_size,
             "pageNumber": pn, "reportName": report, "columns": ",".join(columns),
             "filter": filters}
        r = requests.get(_DC_URL, params=p,
                         headers={"User-Agent": _UA, "Referer": "https://data.eastmoney.com/"},
                         timeout=timeout)
        j = r.json()
        res = j.get("result")
        if not res or not res.get("data"):
            break
        out.extend(res["data"])
        if single or len(out) >= (res.get("count") or 0):
            break
        pn += 1
    return out


def _now_bj():
    """当前北京时间（timezone-aware，避免 utcnow 弃用告警）。"""
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))


def _chunks(seq, k):
    for i in range(0, len(seq), k):
        yield seq[i:i + k]


def detect_latest_period():
    """从最近的季报截止日里，自动探测第一个有数据的报告期。"""
    now = _now_bj()
    cands = []
    for back in range(0, 8):
        d = now - dt.timedelta(days=back * 31)
        for md in ("12-31", "09-30", "06-30", "03-31"):
            cands.append(f"{d.year}-{md}")
    seen, uniq = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c); uniq.append(c)
    for rd in uniq[:10]:
        try:
            rows = _dc_query("RPT_LICO_FN_CPD", ["SECURITY_CODE"],
                             f"(REPORTDATE='{rd}')", page_size=1, single=True)
            if rows:
                return rd
        except Exception:
            continue
    raise RuntimeError("自动探测最新报告期失败")


def load_fundamentals(codes, cfg, latest_period):
    """返回 code -> 财务dict。latest=最新期，annual=近N年报(ROE/增速连续性)。"""
    fund = {c: {} for c in codes}
    if not codes:
        return fund
    code_str = lambda cs: "(SECURITY_CODE in (\"%s\"))" % '","'.join(cs)

    # 最新期 · 业绩
    cpd_cols = ["SECURITY_CODE", "WEIGHTAVG_ROE", "XSMLL", "TOTAL_OPERATE_INCOME",
                "YSTZ", "PARENT_NETPROFIT", "SJLTZ", "MGJYXJJE", "ZXGXL", "ASSIGNDSCRPT"]
    for cs in _chunks(codes, 100):
        try:
            for x in _dc_query("RPT_LICO_FN_CPD", cpd_cols,
                               f"(REPORTDATE='{latest_period}')" + code_str(cs)):
                c = str(x["SECURITY_CODE"]).zfill(6)
                fund[c] = {
                    "roe": _num(x.get("WEIGHTAVG_ROE")),
                    "gross_margin": _num(x.get("XSMLL")),
                    "revenue": _num(x.get("TOTAL_OPERATE_INCOME")),
                    "rev_yoy": _num(x.get("YSTZ")),
                    "netprofit": _num(x.get("PARENT_NETPROFIT")),
                    "np_yoy": _num(x.get("SJLTZ")),
                    "ocfps": _num(x.get("MGJYXJJE")),            # 每股经营现金流
                    "div_yield": _num(x.get("ZXGXL")),          # 股息率%
                    "div_plan": str(x.get("ASSIGNDSCRPT") or ""),
                }
        except Exception as e:
            print(f"[财务] 业绩报表分块失败，跳过：{e}")

    # 最新期 · 资产负债率
    for cs in _chunks(codes, 100):
        try:
            for x in _dc_query("RPT_DMSK_FN_BALANCE", ["SECURITY_CODE", "DEBT_ASSET_RATIO"],
                               f"(REPORT_DATE='{latest_period}')" + code_str(cs)):
                c = str(x["SECURITY_CODE"]).zfill(6)
                fund.setdefault(c, {})["debt_ratio"] = _num(x.get("DEBT_ASSET_RATIO"))
        except Exception as e:
            print(f"[财务] 资产负债分块失败，跳过：{e}")

    # 近 N 个年报 · ROE / 净利增速 / 营收增速（连续性、是否连续下滑）
    this_year = _now_bj().year
    years = [this_year - 1 - i for i in range(cfg.annual_years)]  # 已披露完整年报
    for y in years:
        rd = f"{y}-12-31"
        for cs in _chunks(codes, 100):
            try:
                for x in _dc_query("RPT_LICO_FN_CPD",
                                   ["SECURITY_CODE", "WEIGHTAVG_ROE", "SJLTZ", "YSTZ"],
                                   f"(REPORTDATE='{rd}')" + code_str(cs)):
                    c = str(x["SECURITY_CODE"]).zfill(6)
                    fund.setdefault(c, {}).setdefault("ann", []).append(
                        {"year": y, "roe": _num(x.get("WEIGHTAVG_ROE")),
                         "np_yoy": _num(x.get("SJLTZ")), "rev_yoy": _num(x.get("YSTZ"))})
            except Exception:
                continue
    return fund


# ============================ 第4步：五维打分 + 一票否决 ============================
def _veto(m, f, is_high_debt_industry, cfg):
    """返回否决原因字符串；None=通过。"""
    if _is_st(m["name"]):
        return "ST/退市风险"
    if m["price"] is None:
        return "停牌/无报价"
    if m["chg"] is not None and m["chg"] >= _zt_threshold(m["code"]):
        return "当日已涨停(不追高)"
    if not f:
        return "无财务数据" if not cfg.keep_nodata else None
    roe, np_, ocf = f.get("roe"), f.get("netprofit"), f.get("ocfps")
    if roe is None and not cfg.keep_nodata:
        return "财务数据缺失"
    if np_ is not None and np_ < 0:
        return "最新期亏损"
    if ocf is not None and ocf < 0:
        return "经营现金流为负"
    debt = f.get("debt_ratio")
    if debt is not None and debt > 70 and not is_high_debt_industry:
        return f"资产负债率{debt:.0f}%过高"
    pe = m.get("pe_ttm")
    if pe is not None and pe <= 0:
        return "PE为负/异常(亏损或失真)"
    # 连续两个年报净利同比为负 = 业绩连续下滑
    ann = f.get("ann") or []
    neg = [a for a in ann if a.get("np_yoy") is not None and a["np_yoy"] < 0]
    if len(neg) >= 2:
        return "连续年报净利下滑"
    if m["main_in"] < cfg.min_member_inflow_wan * 1e4:
        return "个股主力资金未净流入"
    return None


def score(m, f, sector_rank_pct, is_high_debt_industry):
    """五维百分制。返回 (总分, 明细dict)。sector_rank_pct=该股主力净流入在板块内的分位(0~1)。"""
    s = {"矛_盈利": 0, "盾_健康": 0, "股东回报": 0, "估值": 0, "资金共振": 0}
    f = f or {}
    # —— 矛：盈利能力(40) ——
    roe = f.get("roe")
    if roe is not None:
        s["矛_盈利"] += 14 if roe >= 20 else 11 if roe >= 15 else 7 if roe >= 10 else 3 if roe >= 5 else 0
    ann = f.get("ann") or []
    good_years = sum(1 for a in ann if (a.get("roe") or -999) >= 15)
    s["矛_盈利"] += min(6, good_years * 2)
    gm = f.get("gross_margin")
    if gm is not None:
        s["矛_盈利"] += 8 if gm >= 40 else 6 if gm >= 25 else 4 if gm >= 15 else 2 if gm > 0 else 0
    rev, np_ = f.get("revenue"), f.get("netprofit")
    if rev and np_:
        nm = np_ / rev * 100
        s["矛_盈利"] += 6 if nm >= 20 else 4 if nm >= 10 else 2 if nm >= 5 else 0
    ry, py = f.get("rev_yoy"), f.get("np_yoy")
    if ry is not None and py is not None:
        if ry >= 15 and py >= 15:
            s["矛_盈利"] += 6
        elif ry > 0 and py > 0:
            s["矛_盈利"] += 3
        if py is not None and ry is not None and py >= ry:
            s["矛_盈利"] += 0  # 已含在上档，主营质量在"盾"里给分，避免重复
    # —— 盾：财务健康(20) ——
    debt = f.get("debt_ratio")
    if is_high_debt_industry:
        s["盾_健康"] += 6
    elif debt is not None:
        s["盾_健康"] += 8 if debt <= 40 else 6 if debt <= 55 else 3 if debt <= 70 else 0
    ocf = f.get("ocfps")
    if ocf is not None:
        s["盾_健康"] += 6 if ocf >= 0.5 else 3 if ocf > 0 else 0
    if rev:
        s["盾_健康"] += 3 if rev >= 1e10 else 2 if rev >= 2e9 else 1
    if ry is not None and py is not None and ry > 0 and py >= ry:
        s["盾_健康"] += 3
    elif ry is not None and py is not None and ry > 0 and py > 0:
        s["盾_健康"] += 1
    # —— 股东回报(12) ——
    dy = f.get("div_yield")
    if dy is not None:
        s["股东回报"] += 6 if dy >= 3 else 4 if dy >= 1 else 2 if dy > 0 else 0
    if any(k in f.get("div_plan", "") for k in ("派", "送", "转")):
        s["股东回报"] += 3
    # （分红融资比/注销回购无日频数据，留人工复核，不打分）
    # —— 估值(16) ——
    pe = m.get("pe_ttm")
    if pe is not None and pe > 0:
        s["估值"] += 8 if pe <= 15 else 6 if pe <= 30 else 3 if pe <= 50 else 1
    pb = m.get("pb")
    if pb is not None and pb > 0:
        s["估值"] += 4 if pb <= 2 else 3 if pb <= 4 else 1 if pb <= 8 else 0
    if pe is not None and pe > 0 and py and py > 0:
        peg = pe / py
        s["估值"] += 4 if peg <= 1 else 2 if peg <= 1.5 else 0
    # —— 资金共振(12) ——
    s["资金共振"] += round(6 * sector_rank_pct, 1)
    mr = m.get("main_ratio", 0)
    s["资金共振"] += 4 if mr >= 10 else 3 if mr >= 5 else 1 if mr > 0 else 0
    chg = m.get("chg", 0)
    s["资金共振"] += 2 if 0 <= chg <= 6 else 1 if -2 <= chg < 0 else 0  # 温和上涨最佳，不追高
    total = round(sum(s.values()), 1)
    return total, s


# ============================ 主流程 ============================
def run(cfg: Cfg):
    t0 = time.time()
    trade_date = cfg.date or latest_trade_date()
    print(f"=== 资金潜伏板块优质股筛选 · 交易日 {trade_date} ===")

    # 1) 板块资金 + 涨停池
    df_fund = load_sector_fund(cfg)
    lookup = FundLookup(df_fund)
    zt_industry, zt_codes, used_date = load_zt(trade_date)

    # 2) 候选板块（流入大、涨停少）
    sectors = select_sectors(df_fund, zt_industry, lookup, cfg)
    print(f"[选板块] 主力净流入≥{cfg.min_inflow_yi}亿 且 涨停≤{cfg.max_zt} 的板块：{len(sectors)}个")

    picks, all_pass, chosen = [], [], set()  # chosen：跨重叠板块全局去重，保证精选名单不重复
    for sec in sectors:
        try:
            members = board_members(sec["代码"])
        except Exception as e:
            print(f"  × {sec['板块']} 成分股取失败：{e}")
            continue
        pos = sum(1 for x in members if x["main_in"] > 0)
        breadth = pos / len(members) if members else 0
        sec["成分股数"] = len(members)
        sec["资金流入广度"] = round(breadth, 2)
        if breadth < cfg.min_breadth:
            print(f"  · {sec['板块']} 流入广度仅{breadth:.0%}（<{cfg.min_breadth:.0%}），不算'大面积'，跳过")
            continue

        # 板块内主力净流入排名分位
        order = sorted(range(len(members)), key=lambda i: -members[i]["main_in"])
        rank = {idx: 1 - (r / max(1, len(members) - 1)) for r, idx in enumerate(order)}
        is_hd = any(k in sec["板块"] for k in _HIGH_DEBT_INDUSTRY)

        codes = [x["code"] for x in members]
        fund = load_fundamentals(codes, cfg, LATEST)
        cand = []
        veto_cnt = {}
        for i, m in enumerate(members):
            f = fund.get(m["code"], {})
            why = _veto(m, f, is_hd, cfg)
            if why:
                veto_cnt[why] = veto_cnt.get(why, 0) + 1
                continue
            tot, det = score(m, f, rank[i], is_hd)
            cand.append({**m, **{f"fin_{k}": v for k, v in f.items() if k != "ann"},
                         "总分": tot, "评分明细": det,
                         "市值板块排名": sorted(order, key=lambda i: -members[i]["mktcap_yi"]).index(i) + 1,
                         "年报序列": f.get("ann", [])})
        cand.sort(key=lambda x: (-x["总分"], -x["main_in"]))
        # 跨板块去重：已被资金流入更大的前序板块选走的个股，这里顺延给下一只
        qualified = [x for x in cand
                     if x["总分"] >= cfg.min_score and x["code"] not in chosen]
        top = qualified[: cfg.per_sector]
        chosen.update(x["code"] for x in top)
        if len(top) < cfg.per_sector:
            print(f"  · {sec['板块']}：达{cfg.min_score:.0f}分且未重复的仅{len(qualified)}只，"
                  f"不足{cfg.per_sector}只，宁缺毋滥只给{len(top)}只（其余见CSV）")
        for x in top:
            x["所属板块"] = sec["板块"]
            picks.append(x)
        for x in cand:
            x["所属板块"] = sec["板块"]; all_pass.append(x)
        print(f"  ✓ {sec['板块']}：{len(members)}只，广度{breadth:.0%}，"
              f"否决{sum(veto_cnt.values())}只，入选{len(top)}只 "
              f"（否决分布：{veto_cnt}）")

    _output(sectors, picks, all_pass, used_date, cfg, t0)
    return sectors, picks, all_pass


def _output(sectors_kept, picks, all_pass, trade_date, cfg, t0):
    os.makedirs(cfg.outdir, exist_ok=True)
    # —— 控制台简报 ——
    print("\n" + "=" * 72)
    print(f"每日精选（每板块前{cfg.per_sector}，报告期 {LATEST}，交易日 {trade_date}）")
    print("=" * 72)
    cur = None
    lines = []
    for x in picks:
        if x["所属板块"] != cur:
            cur = x["所属板块"]
            sec = next(s for s in sectors_kept if s["板块"] == cur)
            head = (f"\n■ {cur}｜主力净流入{sec['主力净流入亿']:.2f}亿、"
                    f"涨停{sec['涨停数']}只、流入广度{sec.get('资金流入广度', 0):.0%}")
            print(head); lines.append(head)
        code_suf = f"{x['code']}{market_suffix(x['code'])}"
        roe = x.get("fin_roe"); gm = x.get("fin_gross_margin"); pe = x.get("pe_ttm")
        row = (f"  {x['name']}({code_suf}) 总分{x['总分']}｜现价{x['price']} 涨{x['chg']}%｜"
               f"主力净流入{x['main_in']/1e8:.2f}亿(占比{x['main_ratio']}%)｜"
               f"ROE {roe if roe is None else round(roe,1)}% 毛利{gm if gm is None else round(gm,1)}% "
               f"PE {pe if pe is None else round(pe,1)} PB "
               f"{x['pb'] if x['pb'] is None else round(x['pb'],2)}")
        print(row); lines.append(row)
    note = ("\n【人工复核清单（程序无日频数据、不打分，买入前必看）】每个入选股请人工确认："
            "①护城河/行业地位(程序仅给板块内市值排名) ②管理层是否违规/频繁减持 ③审计意见是否标准无保留、"
            "是否频繁换所 ④大股东质押比例 ⑤累计分红是否超过累计融资 ⑥有无注销式回购。")
    print(note); lines.append(note)
    warn = ("\n⚠ 仅为量化初筛，不构成投资建议；'资金流入但未涨停'是潜伏思路，同样可能不涨或补跌，"
            "请结合大盘与自身风险承受能力决策。")
    print(warn); lines.append(warn)

    # —— TXT（可贴复盘报告）——
    txt_path = os.path.join(cfg.outdir, f"quality_pick_每日精选_{trade_date}.txt")
    with open(txt_path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines))
    # —— CSV（通过否决的全部候选，不止3只，便于自己再挑）——
    if all_pass:
        df = pd.DataFrame(all_pass)
        show = ["所属板块", "code", "name", "总分", "price", "chg", "main_in", "main_ratio",
                "turnover", "pe_ttm", "pb", "mktcap_yi", "市值板块排名",
                "fin_roe", "fin_gross_margin", "fin_revenue", "fin_rev_yoy", "fin_netprofit",
                "fin_np_yoy", "fin_ocfps", "fin_debt_ratio", "fin_div_yield", "fin_div_plan",
                "评分明细"]
        df = df[[c for c in show if c in df.columns]].rename(columns={
            "code": "代码", "name": "名称", "price": "现价", "chg": "涨跌幅%",
            "main_in": "主力净流入元", "main_ratio": "主力净占比%", "turnover": "换手%",
            "pe_ttm": "PE_TTM", "pb": "PB", "mktcap_yi": "总市值亿"})
        df["主力净流入元"] = (df["主力净流入元"] / 1e8).round(3)
        df = df.rename(columns={"主力净流入元": "主力净流入亿"})
        csv_path = os.path.join(cfg.outdir, f"quality_pick_候选全量_{trade_date}.csv")
        df.sort_values(["所属板块", "总分"], ascending=[True, False]).to_csv(
            csv_path, index=False, encoding="utf-8-sig")
        print(f"\n[输出] 精选文本：{txt_path}\n[输出] 候选全量CSV：{csv_path}（{len(df)}只过否决）")
    print(f"[耗时] {time.time()-t0:.1f}s")


LATEST = ""  # 运行时填充最新报告期


def main():
    global LATEST
    ap = argparse.ArgumentParser(description="资金潜伏板块·优质股筛选")
    ap.add_argument("--min-inflow", type=float, default=1.5, help="板块主力净流入下限(亿)，默认1.5")
    ap.add_argument("--max-zt", type=int, default=0, help="板块允许涨停只数，默认0(无涨停)")
    ap.add_argument("--min-breadth", type=float, default=0.5, help="板块资金流入个股占比下限，默认0.5")
    ap.add_argument("--top-sectors", type=int, default=8, help="最多板块数，默认8")
    ap.add_argument("--per-sector", type=int, default=3, help="每板块选股数，默认3")
    ap.add_argument("--min-member-inflow-wan", type=float, default=0.0, help="个股主力净流入下限(万)")
    ap.add_argument("--annual-years", type=int, default=3, help="ROE连续性看几年年报，默认3")
    ap.add_argument("--min-score", type=float, default=50.0, help="入选最低总分，默认50，宁缺毋滥")
    ap.add_argument("--keep-nodata", action="store_true", help="财务缺失也保留为观察项")
    ap.add_argument("--date", default="", help="交易日YYYYMMDD，默认自动")
    ap.add_argument("--outdir", default=".", help="输出目录")
    a = ap.parse_args()
    cfg = Cfg(min_inflow_yi=a.min_inflow, max_zt=a.max_zt, min_breadth=a.min_breadth,
              top_sectors=a.top_sectors, per_sector=a.per_sector,
              min_member_inflow_wan=a.min_member_inflow_wan, annual_years=a.annual_years,
              min_score=a.min_score, keep_nodata=a.keep_nodata, date=a.date, outdir=a.outdir)
    LATEST = detect_latest_period()
    print(f"[财务] 采用最新报告期：{LATEST}")
    run(cfg)


if __name__ == "__main__":
    main()
