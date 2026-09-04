# -*- coding: utf-8 -*-
"""
监管预警系统 v2.2 —— 技术触发 × 交易所重点监控名单 交叉验证
============================================================================
【关键口径修正】“在监管/重点监控”分两层，绝不能混为一谈：
  A类·交易重点监控（真正的“被交易所盯上”）：交易所把证券列入重点监控并下发券商，
     券商在投资者教育栏目公示“风险提示标的名单”，带【监控起止日期】，通常10个交易日。
     ——本模块用券商公示接口结构化获取，作为第三层四分类的“真实名单”。
  B类·上市公司监管公告（信披层面，不等于交易被监控）：异常波动公告、监管函、警示函等，
     ——只在报告里单独列示，不参与四分类，避免把“公司信披违规”误判成“交易被重点监控”。

第一层：技术触发（纯计算）
  3日累计偏离：主板(60/00)±20%，创业板(30)/科创板(68)±30%；10日+100%；30日+200%。
第二层：真实名单 = A类券商重点监控（主）；B类公告仅补充列示。
第三层：四分类（以A类为准）
  🔴 技术触线 且 在重点监控名单 / 🟡 技术触线 但 暂不在名单（补录风险）
  🔵 未触线 但 在重点监控名单 / 🟢 安全（不输出）
第四层：🟡按 超标幅度/连板/换手/板块地位/板块效应 打分降序。
状态机：monitor_state.json 出监管倒计时；monitor_tech_cache.json 支撑晚间黄转红。
数据源：新浪个股/指数日K（技术层）+ 券商重点监控公示接口（真实名单，免key、GET返回JSON）。
"""
import os
import json
import time
import requests
import akshare as ak
from datetime import datetime, timezone, timedelta

# ============================================================ 可配置参数
INDEX_MAP = [
    ("68", "sh000688", "科创50"),
    ("60", "sh000001", "上证指数"),
    ("30", "sz399006", "创业板指"),
    ("00", "sz399001", "深证成指"),
]
THR_3_MAIN, THR_3_GEM = 20.0, 30.0
THR_10, THR_30 = 100.0, 200.0
WINDOWS = (3, 10, 30)
MONITOR_DAYS = 10
NEAR_RATIO, WATCH_RATIO = 0.8, 0.5
FETCH_RETRY = 2
_CST = timezone(timedelta(hours=8))

# A类：券商“风险提示标的名单（重点监控证券）”结构化接口（返回JSON，免key，按顺序尝试）
BROKER_MONITOR_APIS = [
    ("野村东方国际证券", "https://www.nomuraoi-sec.com/api/RiskDisclousure/list"),
]
# B类：上市公司信披监管公告类型（仅补充列示，不参与四分类）
ACTION_TYPES = {"深交所股票监管函", "上交所股票监管关注", "警示函公告", "处罚",
                "上交所股票公开谴责", "上交所股票通报批评"}
ABNORMAL_TYPE = "股票交易异常波动"
NEG_WORDS = ("不存在", "未受到", "未被", "没有受到", "最近五年", "近五年", "无被")
_index_cache = {}
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


# ============================================================ 基础工具
def match_index(code):
    for pre, sym, name in INDEX_MAP:
        if str(code).startswith(pre):
            return sym, name
    return None


def _sina_symbol(code):
    code = str(code).zfill(6)
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("0", "2", "3")):
        return "sz" + code
    return None


def _thr3(code):
    return THR_3_GEM if str(code).startswith(("30", "68")) else THR_3_MAIN


def _is_a_stock(code):
    # 沪深A股/北交所白名单，自动剔除基金(5/15/16/51/52/56/58)、可转债(11/12)等
    return str(code).startswith(("60", "00", "30", "68", "83", "87", "92", "43"))


def _ms_to_date(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=_CST).date()


def _get_index_closes(sym):
    if sym in _index_cache:
        return _index_cache[sym]
    last = None
    for _ in range(FETCH_RETRY + 1):
        try:
            df = ak.stock_zh_index_daily(symbol=sym)[["date", "close"]]
            closes = df.sort_values("date")["close"].astype(float).tolist()
            _index_cache[sym] = closes
            return closes
        except Exception as e:
            last = e; time.sleep(0.4)
    print(f"  [监管] 指数{sym}获取失败:{last}")
    return None


def _get_stock_closes(code):
    sym = _sina_symbol(code)
    if not sym:
        return None
    last = None
    for _ in range(FETCH_RETRY + 1):
        try:
            df = ak.stock_zh_a_daily(symbol=sym, adjust="")[["date", "close"]]
            return df.sort_values("date")["close"].astype(float).tolist()
        except Exception as e:
            last = e; time.sleep(0.4)
    print(f"  [监管] 个股{code}日K失败:{last}")
    return None


# ============================================================ 第一层：技术触发
def _calc_windows(code, sc, ic):
    res = []
    for win in WINDOWS:
        if len(sc) < win + 1 or len(ic) < win + 1:
            res.append({"win": win, "ok": False}); continue
        thr = _thr3(code) if win == 3 else (THR_10 if win == 10 else THR_30)
        bidir = win == 3
        s_pct = (sc[-1] / sc[-win - 1] - 1) * 100
        i_pct = (ic[-1] / ic[-win - 1] - 1) * 100
        dev = s_pct - i_pct
        hit = abs(dev) >= thr if bidir else dev >= thr
        res.append({"win": win, "thr": thr, "bidirectional": bidir, "ok": True,
                    "stock_pct": s_pct, "index_pct": i_pct, "dev": dev,
                    "ratio": (abs(dev) if bidir else dev) / thr, "hit": hit})
    return res


def analyze_one(code, stock_info=None):
    code = str(code).zfill(6)
    matched = match_index(code)
    if matched is None:
        return None
    idx_sym, idx_name = matched
    sc, ic = _get_stock_closes(code), _get_index_closes(idx_sym)
    if not sc or not ic:
        return None
    wins = [w for w in _calc_windows(code, sc, ic) if w.get("ok")]
    if not wins:
        return None
    max_dev_win = max(wins, key=lambda w: abs(w["dev"]))
    return {"code": code, "index_name": idx_name, "windows": wins,
            "max_ratio": max(w["ratio"] for w in wins), "hit": any(w["hit"] for w in wins),
            "max_dev": max_dev_win["dev"], "max_dev_win": max_dev_win["win"],
            "stock_info": stock_info or {}}


# ============================================================ 第二层A：券商重点监控名单（真实名单）
def fetch_broker_monitor_list(today=None):
    """
    返回 (book:{code:{name,start,end,active}}, source:str, ok:bool)
    active=当前仍在监控期内（结束日≥今天）。
    """
    today = today or datetime.now(_CST).date()
    if isinstance(today, str):
        today = datetime.strptime(today, "%Y%m%d").date()
    for src_name, url in BROKER_MONITOR_APIS:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15)
            raw = resp.json()["data"]["list"]
            book = {}
            for x in raw:
                code = str(x.get("securityCode", "")).zfill(6)
                if not code or not _is_a_stock(code):
                    continue  # 剔除ETF/基金/债
                s, e = _ms_to_date(x["startDate"]), _ms_to_date(x["referenceEndDate"])
                book[code] = {"name": str(x.get("securityName", "")).strip(),
                              "start": str(s), "end": str(e), "active": e >= today}
            active_n = sum(v["active"] for v in book.values())
            print(f"  [名单·A类] {src_name}：重点监控A股{len(book)}只，当前在监控期{active_n}只")
            return book, src_name, True
        except Exception as ex:
            print(f"  [名单·A类] {src_name} 获取失败:{ex}")
    return {}, "", False


# ============================================================ 第二层B：上市公司监管公告（仅补充）
def fetch_public_notices(today):
    detail, abnormal, action, name_map = {}, set(), set(), {}
    try:
        df = ak.stock_notice_report(symbol="全部", date=today)
    except Exception as e:
        print(f"  [名单·B类] 公告接口失败:{e}")
        return abnormal, action, detail, name_map
    if df is None or df.empty:
        return abnormal, action, detail, name_map
    for _, r in df.iterrows():
        code = str(r.get("代码", "")).zfill(6)
        if not code or code == "000000":
            continue
        atype, title = str(r.get("公告类型", "")), str(r.get("公告标题", ""))
        nm = str(r.get("名称", "") or "").strip()
        if nm:
            name_map[code] = nm
        is_abn = (atype == ABNORMAL_TYPE) or ("严重异常波动" in title)
        is_act = atype in ACTION_TYPES and not any(w in title for w in NEG_WORDS)
        if is_abn or is_act:
            detail.setdefault(code, []).append(f"[{atype}] {title[:38]}")
            if is_abn: abnormal.add(code)
            if is_act: action.add(code)
    print(f"  [名单·B类] 信披公告：异常波动{len(abnormal)}只、监管措施{len(action)}只（仅补充列示，不算交易重点监控）")
    return abnormal, action, detail, name_map


# ============================================================ 第三层：分类
def classify_stock(analysis, in_real):
    hit = analysis["hit"] if analysis else False
    if hit and in_real:
        return "已确认监控", "🔴"
    if hit:
        return "触线未监控", "🟡"
    if in_real:
        return "未触线但被监控", "🔵"
    return "安全", "🟢"


# ============================================================ 第四层：🟡风险分
def calc_priority_score(analysis, industry_total=0):
    score = min(analysis["max_ratio"], 3.0) / 3.0 * 50
    info = analysis.get("stock_info", {})
    score += min(int(info.get("连板数", 1) or 1), 5) / 5.0 * 20
    turnover = float(info.get("换手率", 0) or 0)
    score += 15 if turnover > 20 else (10 if turnover > 10 else (5 if turnover > 5 else 0))
    pos = str(info.get("板块地位", ""))
    score += 10 if "龙头" in pos else (5 if "跟风" in pos else (2 if "补涨" in pos else 0))
    if industry_total >= 3:
        score += 5
    return round(min(score, 100.0), 1)


def load_state(path, default=None):
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default if default is not None else {}


def save_state(path, obj):
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [监管] 写文件失败:{e}")


def _fmt_row(r, name, code, book, pub_detail, industry_total):
    detail = []
    for w in r["windows"]:
        if not w.get("ok"):
            continue
        sign = "±" if w["bidirectional"] else "+"
        detail.append(f"{w['win']}日偏离{w['dev']:+.2f}%/{sign}{w['thr']:.0f}%(进度{w['ratio']*100:.0f}%)")
    info = r.get("stock_info", {})
    mb = book.get(code)
    return {"code": code, "name": name, "index_name": r["index_name"],
            "max_ratio": r["max_ratio"], "max_dev": r["max_dev"], "max_dev_win": r["max_dev_win"],
            "detail": "；".join(detail), "pub_notices": pub_detail.get(code, []),
            "monitor_period": f"{mb['start']}~{mb['end']}" if mb else "",
            "lianban": int(info.get("连板数", 1) or 1),
            "turnover": float(info.get("换手率", 0) or 0),
            "industry": str(info.get("所属行业", "")),
            "position": str(info.get("板块地位", "")),
            "industry_total": industry_total}


# ============================================================ 主入口
def build_monitor_alert(zt_df, today, state_path="monitor_state.json",
                        extra_codes=None, fetch_real_list=True,
                        cache_path="monitor_tech_cache.json", enable_web_crawl=False):
    print("=" * 60); print("监管预警 v2.2 — 技术触发 × 交易所重点监控名单"); print("=" * 60)
    state = load_state(state_path, {})
    prev_cache = load_state(cache_path, {})

    # 第二层A类：交易所重点监控（真实名单）
    print("\n【第二层】获取交易所重点监控名单（券商公示，带起止日期）...")
    book, broker_src, broker_ok = {}, "", False
    if fetch_real_list:
        book, broker_src, broker_ok = fetch_broker_monitor_list(today)
    real_codes = {c for c, v in book.items() if v.get("active")}
    if not broker_ok:
        print("  ⚠ A类重点监控名单获取失败：本次四分类缺真实名单，🔴/🔵会缺失，仅技术层结果可信")

    # 第二层B类：上市公司信披公告（补充，不参与四分类）
    print("\n【第二层·补充】上市公司异动/监管公告（信披层面，单独列示）...")
    abn_set, act_set, pub_detail, pub_name = fetch_public_notices(today)
    pub_codes = abn_set | act_set

    industry_counts = {}
    if zt_df is not None and not zt_df.empty:
        for ind in zt_df["所属行业"].astype(str):
            industry_counts[ind] = industry_counts.get(ind, 0) + 1

    todo = {}
    if zt_df is not None and not zt_df.empty:
        for _, r in zt_df.iterrows():
            c = str(r.get("代码", "")).zfill(6)
            if c:
                todo[c] = (str(r.get("名称", c)), {
                    "连板数": int(r.get("连板数", 1) or 1), "换手率": float(r.get("换手率", 0) or 0),
                    "所属行业": str(r.get("所属行业", "")), "板块地位": str(r.get("板块地位", ""))})
    for c, info in state.items():
        todo.setdefault(c, (info.get("name", c), {}))
    for c in real_codes | pub_codes:
        nm = book.get(c, {}).get("name") or pub_name.get(c) or c
        todo.setdefault(c, (nm, {}))
    for c in (extra_codes or []):
        todo.setdefault(str(c).zfill(6), (str(c).zfill(6), {}))

    print(f"\n【第一层】技术触发判断，共{len(todo)}只...")
    analyzed = {}
    for i, (code, (name, sinfo)) in enumerate(todo.items()):
        r = analyze_one(code, sinfo)
        if r:
            r["name"] = name; analyzed[code] = r
        if (i + 1) % 10 == 0:
            print(f"  已分析 {i+1}/{len(todo)}")
        time.sleep(0.05)

    print("\n【第三层】四分类（以A类交易所重点监控名单为准）...")
    confirmed, triggered, monitored = [], [], []
    near, watching, safe = [], [], []
    for code, r in analyzed.items():
        status, icon = classify_stock(r, code in real_codes)
        ind_total = industry_counts.get(r.get("stock_info", {}).get("所属行业", ""), 0)
        row = _fmt_row(r, r["name"], code, book, pub_detail, ind_total)
        row.update({"status": status, "icon": icon, "in_real": code in real_codes,
                    "priority_score": calc_priority_score(r, ind_total) if status == "触线未监控" else 0})
        if status == "已确认监控":
            confirmed.append(row)
        elif status == "触线未监控":
            triggered.append(row)
        elif status == "未触线但被监控":
            monitored.append(row)
        elif r["max_ratio"] >= NEAR_RATIO:
            near.append(row)
        elif r["max_ratio"] >= WATCH_RATIO:
            watching.append(row)
        else:
            safe.append(row)

    print("【第四层】🟡补录风险排序...")
    triggered.sort(key=lambda x: -x["priority_score"])
    confirmed.sort(key=lambda x: -(x["priority_score"] or x["max_ratio"]))
    monitored.sort(key=lambda x: x["monitor_period"])
    near.sort(key=lambda x: -x["max_ratio"]); watching.sort(key=lambda x: -x["max_ratio"])

    # 状态机
    new_state = {}
    for code, r in analyzed.items():
        if r["hit"]:
            rec = state.get(code, {})
            new_state[code] = {"name": r["name"], "enter_date": rec.get("enter_date", today),
                               "left_days": MONITOR_DAYS, "index_name": r["index_name"],
                               "last_dev": round(r["max_dev"], 2), "last_win": r["max_dev_win"]}
        elif code in state:
            left = int(state[code].get("left_days", MONITOR_DAYS)) - 1
            if left > 0:
                new_state[code] = dict(state[code]); new_state[code]["left_days"] = left
                new_state[code]["last_dev"] = round(r["max_dev"], 2)
    for code, rec in state.items():
        if code not in analyzed:
            new_state[code] = rec
    exited = [{"code": c, "name": state[c].get("name", c)} for c in state if c not in new_state and c in analyzed]
    monitoring_list = sorted([
        {"code": c, "name": rec.get("name", c), "enter_date": rec.get("enter_date", ""),
         "left_days": int(rec.get("left_days", MONITOR_DAYS)), "index_name": rec.get("index_name", ""),
         "last_dev": rec.get("last_dev", 0), "last_win": rec.get("last_win", ""),
         "leaving": int(rec.get("left_days", MONITOR_DAYS)) <= 3}
        for c, rec in new_state.items()], key=lambda x: (x["left_days"], -abs(x["last_dev"] or 0)))
    save_state(state_path, new_state)

    upgraded = [row for row in confirmed if prev_cache.get(row["code"]) == "yellow"]
    cache = {r["code"]: "yellow" for r in triggered}; cache.update({r["code"]: "red" for r in confirmed})
    save_state(cache_path, cache)

    # 当前在监控期的完整A类名单（无论是否涨停/触线）
    watch_book = [{"code": c, "name": v["name"], "start": v["start"], "end": v["end"]}
                  for c, v in sorted(book.items(), key=lambda kv: kv[1]["end"]) if v.get("active")]

    print(f"\n【汇总】A类重点监控{len(real_codes)}只 | 🔴{len(confirmed)} 🟡{len(triggered)} "
          f"🔵{len(monitored)} 临近{len(near)} 倒计时{len(monitoring_list)} 黄转红{len(upgraded)}")
    return {"confirmed": confirmed, "triggered": triggered, "monitored": monitored,
            "near": near, "watching": watching, "safe": safe, "monitoring_list": monitoring_list,
            "exited": exited, "upgraded": upgraded, "watch_book": watch_book,
            "broker_ok": broker_ok, "broker_src": broker_src,
            "pub_abnormal": abn_set, "pub_action": act_set, "pub_detail": pub_detail,
            "real_codes": real_codes, "state": new_state}


# ============================================================ 报告
def generate_report(a, today):
    L = []
    if a.get("broker_ok"):
        L.append(f"真实名单来源：交易所重点监控证券（券商公示·{a['broker_src']}，带起止日期）；当前在监控{len(a['watch_book'])}只。")
    else:
        L.append("⚠ 交易所重点监控名单本次获取失败，🔴/🔵分类缺失，以下仅技术触发结果可信，请检查网络或稍后重跑。")
    L.append("")
    # 完整在监控清单（到底哪些在监管、何时结束）
    if a.get("watch_book"):
        L.append("■ 📋 当前交易所重点监控证券全名单（监控起止）")
        for x in a["watch_book"]:
            L.append(f"  {x['name']}({x['code']}) {x['start']} ~ {x['end']}")
        L.append("")
    if a.get("upgraded"):
        L.append("■ 🔴🔴 状态升级（白天🟡 → 现已进入交易所重点监控名单）")
        for r in a["upgraded"]:
            L.append(f"  ⚡{r['name']}({r['code']}) 监控期{r['monitor_period']} 风险分{r['priority_score'] or '—'}")
        L.append("")
    if a["confirmed"]:
        L.append(f"■ 🔴 已确认（技术触线 且 在重点监控名单，共{len(a['confirmed'])}只）")
        for r in a["confirmed"]:
            L.append(f"  {r['name']}({r['code']}) 监控期{r['monitor_period']} {r['lianban']}连板｜基准{r['index_name']}")
            L.append(f"     {r['detail']}")
        L.append("")
    if a["triggered"]:
        L.append(f"■ 🟡 技术触线但暂不在重点监控名单（按补录概率降序，共{len(a['triggered'])}只）")
        for i, r in enumerate(a["triggered"][:20], 1):
            pos = f" {r['position']}" if r.get("position") else ""
            L.append(f"  {i}. {r['name']}({r['code']}) 风险分{r['priority_score']} {r['lianban']}连板 换手{r['turnover']:.0f}%{pos}")
            L.append(f"     {r['detail']}")
        L.append("")
    if a["monitored"]:
        L.append(f"■ 🔵 未技术触线 但 已在重点监控名单（共{len(a['monitored'])}只）")
        for r in a["monitored"][:15]:
            L.append(f"  {r['name']}({r['code']}) 监控期{r['monitor_period']}")
        L.append("")
    if a["near"]:
        L.append(f"■ 🟠 高度临近技术红线≥80%（共{len(a['near'])}只）")
        for r in a["near"][:10]:
            mark = "【已在监控】" if r["in_real"] else ""
            L.append(f"  {r['name']}({r['code']}) 进度{r['max_ratio']*100:.0f}% {mark}｜{r['detail']}")
        L.append("")
    if a["watching"]:
        L.append("■ 🟢 观察区(50%-80%)：" + "、".join(f"{r['name']}({r['max_ratio']*100:.0f}%)" for r in a["watching"][:15]))
        L.append("")
    if a["monitoring_list"]:
        L.append(f"■ 出监管倒计时（技术触线观察期，共{len(a['monitoring_list'])}只）")
        for r in a["monitoring_list"][:20]:
            L.append(f"  {r['name']}({r['code']}) 自{r['enter_date']}起 剩余{r['left_days']}交易日"
                     f"{' 🟢即将退出' if r['leaving'] else ''}，最近{r['last_win']}日偏离{r['last_dev']:+.2f}%")
        L.append("")
    if a["exited"]:
        L.append("■ 今日退出观察期：" + "、".join(f"{r['name']}({r['code']})" for r in a["exited"]))
        L.append("")
    pub_rows = []
    for code in sorted(a.get("pub_abnormal", set()) | a.get("pub_action", set())):
        for t in a.get("pub_detail", {}).get(code, [])[:1]:
            pub_rows.append(f"  {code} {t}")
    if pub_rows:
        L.append(f"■ 📄 今日上市公司异动/监管公告（信披层面，共{len(pub_rows)}条，不等同于被交易重点监控）")
        L.extend(pub_rows[:20]); L.append("")
    L.append("■ 说明：技术红线=3日偏离(主板±20%/创科±30%)、10日+100%、30日+200%（相对基准指数）。")
    L.append("  交易所对账户的部分后台监控不对外公示，本名单以券商公开转发为准；本提醒不构成投资建议。")
    return "\n".join(L)
