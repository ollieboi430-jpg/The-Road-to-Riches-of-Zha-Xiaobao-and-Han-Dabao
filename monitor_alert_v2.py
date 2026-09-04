# -*- coding: utf-8 -*-
"""
监管预警系统 v2.1 —— 四层逻辑完整实现（三层交叉 + 风险分级 + 进/出状态机）
============================================================================
第一层：技术触发判断（纯计算）
  - 3日累计偏离：主板(60/00)±20%，创业板(30)/科创板(68)±30%
  - 10日累计偏离 +100%；30日累计偏离 +200%（任一达标即技术触线）
第二层：真实监控名单（主源=交易所公告接口，海外可用免key；辅源=交易所官网，默认关）
  - 异常波动名单：公告类型=“股票交易异常波动” 或 标题含“严重异常波动”（与价格触线对应）
  - 监管措施名单：监管函/监管关注/警示函/公开谴责/通报批评/处罚（非价格原因）
  - 自动过滤“最近五年不存在被处罚”等合规声明噪音
第三层：交叉验证四分类
  - 🔴 已确认监控（技术触发 + 在真实名单）
  - 🟡 触线未监控（技术触发 + 不在名单，存在补录风险）
  - 🔵 未触线但被监控（未触发 + 在名单，其他原因）
  - 🟢 安全（不输出）
第四层：预警分级——🟡按 超标幅度/连板/换手/板块地位/板块效应 打分降序
状态持久化：monitor_state.json（纳入后10交易日倒计时，归零退出，再触线重新纳入）
晚间更新：monitor_tech_cache.json 记录白天触线状态，19点公告到位后🟡自动升级🔴
数据源：新浪个股/指数日K + akshare东财公告接口（GitHub Actions 可达、免key）
"""
import os
import json
import time
import re
import requests
import akshare as ak

# ============================================================
# 可配置参数
# ============================================================
INDEX_MAP = [
    ("68", "sh000688", "科创50"),    # 科创板（必须排在60前面）
    ("60", "sh000001", "上证指数"),   # 沪市主板
    ("30", "sz399006", "创业板指"),   # 创业板
    ("00", "sz399001", "深证成指"),   # 深市主板
]
# 3日阈值按板块：创业板/科创板30%，其余主板20%
THR_3_MAIN, THR_3_GEM = 20.0, 30.0
THR_10, THR_30 = 100.0, 200.0
WINDOWS = (3, 10, 30)
MONITOR_DAYS = 10
NEAR_RATIO = 0.8
WATCH_RATIO = 0.5
FETCH_RETRY = 2
# 第二层：监管措施类公告（非价格异动原因被点名）
ACTION_TYPES = {"深交所股票监管函", "上交所股票监管关注", "警示函公告", "处罚",
                "上交所股票公开谴责", "上交所股票通报批评"}
ABNORMAL_TYPE = "股票交易异常波动"
# 合规声明否定词（并非真被监管，需排除）
NEG_WORDS = ("不存在", "未受到", "未被", "没有受到", "最近五年", "近五年", "无被")
_index_cache = {}


# ============================================================
# 工具函数
# ============================================================
def match_index(code):
    code = str(code)
    for pre, sym, name in INDEX_MAP:
        if code.startswith(pre):
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
    """3日阈值按板块：创业板/科创板30%，主板20%"""
    return THR_3_GEM if str(code).startswith(("30", "68")) else THR_3_MAIN


def _get_index_closes(sym):
    if sym in _index_cache:
        return _index_cache[sym]
    last_err = None
    for _ in range(FETCH_RETRY + 1):
        try:
            df = ak.stock_zh_index_daily(symbol=sym)[["date", "close"]]
            closes = df.sort_values("date")["close"].astype(float).tolist()
            _index_cache[sym] = closes
            return closes
        except Exception as e:
            last_err = e
            time.sleep(0.4)
    print(f"  [监管] 指数{sym}获取失败:{last_err}")
    return None


def _get_stock_closes(code):
    sina_code = _sina_symbol(code)
    if not sina_code:
        return None
    last_err = None
    for _ in range(FETCH_RETRY + 1):
        try:
            df = ak.stock_zh_a_daily(symbol=sina_code, adjust="")[["date", "close"]]
            return df.sort_values("date")["close"].astype(float).tolist()
        except Exception as e:
            last_err = e
            time.sleep(0.4)
    print(f"  [监管] 个股{code}日K获取失败:{last_err}")
    return None


# ============================================================
# 第一层：技术触发判断
# ============================================================
def _calc_windows(code, stock_closes, index_closes):
    """按交易日位置对齐算三窗口偏离，期初=-win-1、期末=-1。3日阈值按板块。"""
    res = []
    for win in WINDOWS:
        if len(stock_closes) < win + 1 or len(index_closes) < win + 1:
            res.append({"win": win, "ok": False})
            continue
        if win == 3:
            thr, bidir = _thr3(code), True
        elif win == 10:
            thr, bidir = THR_10, False
        else:
            thr, bidir = THR_30, False
        s_pct = (stock_closes[-1] / stock_closes[-win - 1] - 1) * 100
        i_pct = (index_closes[-1] / index_closes[-win - 1] - 1) * 100
        dev = s_pct - i_pct
        hit = abs(dev) >= thr if bidir else dev >= thr
        ratio = (abs(dev) if bidir else dev) / thr
        res.append({"win": win, "thr": thr, "bidirectional": bidir, "ok": True,
                    "stock_pct": s_pct, "index_pct": i_pct, "dev": dev,
                    "ratio": ratio, "hit": hit})
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
    wins = _calc_windows(code, sc, ic)
    valid = [w for w in wins if w.get("ok")]
    if not valid:
        return None
    max_ratio = max(w["ratio"] for w in valid)
    max_dev_win = max(valid, key=lambda w: abs(w["dev"]))
    return {"code": code, "index_name": idx_name, "index_sym": idx_sym, "windows": valid,
            "max_ratio": max_ratio, "hit": any(w["hit"] for w in valid),
            "max_dev": max_dev_win["dev"], "max_dev_win": max_dev_win["win"],
            "stock_info": stock_info or {}}


# ============================================================
# 第二层：真实监控名单
# ============================================================
def fetch_notice_real_list(today):
    """
    主源：akshare 东财公告接口（GitHub 海外可达、免key）。
    返回 (abnormal_set 异常波动, action_set 监管措施, detail 代码->[公告], name_map 代码->名称)
    """
    detail, abnormal, action, name_map = {}, set(), set(), {}
    try:
        df = ak.stock_notice_report(symbol="全部", date=today)
    except Exception as e:
        print(f"  [名单] 公告接口获取失败:{e}（本次仅按技术层判断）")
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
            if is_abn:
                abnormal.add(code)
            if is_act:
                action.add(code)
    print(f"  [名单] 公告主源：异常波动{len(abnormal)}只、监管措施{len(action)}只")
    return abnormal, action, detail, name_map


class MonitorListFetcher:
    """辅源：交易所官网页面抓取（海外IP可能不可达/JS动态页提不到代码，失败即返回空，不影响主源）"""
    def __init__(self):
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

    def _extract(self, url):
        out = {}
        try:
            resp = requests.get(url, headers=self.headers, timeout=8)
            if resp.status_code == 200:
                for code in set(re.findall(r'\b(6\d{5}|0\d{5}|3\d{5})\b', resp.text)):
                    out[code] = ""
        except Exception as e:
            print(f"  [名单·辅源] {url} 抓取失败:{e}")
        return out

    def get_full_list(self, date_str=None):
        sse = self._extract("http://www.sse.com.cn/disclosure/credibility/supervision/measures/")
        szse = self._extract("http://www.szse.cn/disclosure/supervision/inquire/index.html")
        return {**sse, **szse}


# ============================================================
# 第三层：交叉验证分类
# ============================================================
def classify_stock(analysis, in_real_list):
    is_triggered = analysis["hit"] if analysis else False
    if is_triggered and in_real_list:
        return "已确认监控", "高", "🔴"
    elif is_triggered:
        return "触线未监控", "中", "🟡"
    elif in_real_list:
        return "未触线但被监控", "高", "🔵"
    return "安全", "低", "🟢"


# ============================================================
# 第四层：预警分级（🟡补录概率打分，0-100）
# ============================================================
def calc_priority_score(analysis, industry_total=0):
    score = 0.0
    score += min(analysis["max_ratio"], 3.0) / 3.0 * 50      # 超标幅度，最多50
    info = analysis.get("stock_info", {})
    lianban = int(info.get("连板数", 1) or 1)
    score += min(lianban, 5) / 5.0 * 20                       # 连板，最多20
    turnover = float(info.get("换手率", 0) or 0)
    if turnover > 20:
        score += 15
    elif turnover > 10:
        score += 10
    elif turnover > 5:
        score += 5                                            # 换手，最多15
    position = str(info.get("板块地位", ""))
    if "龙头" in position:
        score += 10
    elif "跟风" in position:
        score += 5
    elif "补涨" in position:
        score += 2                                            # 板块地位，最多10
    if industry_total >= 3:
        score += 5                                            # 板块效应（同行业涨停≥3）
    return round(min(score, 100.0), 1)


# ============================================================
# 状态持久化
# ============================================================
def load_state(path, default=None):
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default if default is not None else {}
    return default if default is not None else {}


def save_state(path, state):
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [监管] 状态文件保存失败:{e}")


def _fmt_row(r, name, code, notices, industry_total):
    detail = []
    for w in r["windows"]:
        if not w.get("ok"):
            continue
        sign = "±" if w["bidirectional"] else "+"
        detail.append(f"{w['win']}日偏离{w['dev']:+.2f}%/{sign}{w['thr']:.0f}%(进度{w['ratio']*100:.0f}%)")
    info = r.get("stock_info", {})
    return {"code": code, "name": name, "index_name": r["index_name"],
            "max_ratio": r["max_ratio"], "max_dev": r["max_dev"], "max_dev_win": r["max_dev_win"],
            "detail": "；".join(detail), "notices": notices,
            "lianban": int(info.get("连板数", 1) or 1),
            "turnover": float(info.get("换手率", 0) or 0),
            "industry": str(info.get("所属行业", "")),
            "position": str(info.get("板块地位", "")),
            "industry_total": industry_total}


# ============================================================
# 主入口
# ============================================================
def build_monitor_alert(zt_df, today, state_path="monitor_state.json",
                        extra_codes=None, fetch_real_list=True,
                        cache_path="monitor_tech_cache.json", enable_web_crawl=False):
    print("=" * 60)
    print("监管预警系统 v2.1 — 三层交叉 + 风险分级")
    print("=" * 60)
    state = load_state(path=state_path, default={})
    prev_cache = load_state(path=cache_path, default={})

    # ---- 第二层：真实监控名单（公告主源 + 官网辅源）----
    print("\n【第二层】获取真实监控名单...")
    abnormal_set, action_set, notice_detail, notice_name = fetch_notice_real_list(today)
    web_list = {}
    if fetch_real_list and enable_web_crawl:
        try:
            web_list = MonitorListFetcher().get_full_list(today)
        except Exception as e:
            print(f"  [名单·辅源] 官网抓取异常:{e}")
    real_codes = abnormal_set | action_set | set(web_list.keys())
    print(f"  真实名单合计 {len(real_codes)} 只（异常波动{len(abnormal_set)}/监管措施{len(action_set)}/官网辅源{len(web_list)}）")

    # 行业涨停家数（板块效应）
    industry_counts = {}
    if zt_df is not None and not zt_df.empty:
        for ind in zt_df["所属行业"].astype(str):
            industry_counts[ind] = industry_counts.get(ind, 0) + 1

    # ---- 待分析股票池 ----
    todo = {}
    if zt_df is not None and not zt_df.empty:
        for _, r in zt_df.iterrows():
            c = str(r.get("代码", "")).zfill(6)
            if c:
                todo[c] = (str(r.get("名称", c)), {
                    "连板数": int(r.get("连板数", 1) or 1),
                    "换手率": float(r.get("换手率", 0) or 0),
                    "所属行业": str(r.get("所属行业", "")),
                    "板块地位": str(r.get("板块地位", "")),
                })
    for c, info in state.items():
        todo.setdefault(c, (info.get("name", c), {}))
    for c in real_codes:
        todo.setdefault(c, (notice_name.get(c, c), {}))
    for c in (extra_codes or []):
        todo.setdefault(str(c).zfill(6), (str(c).zfill(6), {}))

    print(f"\n【第一层】技术触发判断，共 {len(todo)} 只...")
    analyzed = {}
    for i, (code, (name, stock_info)) in enumerate(todo.items()):
        r = analyze_one(code, stock_info)
        if r:
            r["name"] = name
            analyzed[code] = r
        if (i + 1) % 10 == 0:
            print(f"  已分析 {i+1}/{len(todo)}")
        time.sleep(0.05)
    print(f"  技术分析完成，有效 {len(analyzed)} 只")

    # ---- 第三层：交叉分类 ----
    print("\n【第三层】交叉验证与分类...")
    confirmed, triggered, monitored = [], [], []
    near, watching, safe = [], [], []
    for code, r in analyzed.items():
        name = r["name"]
        in_real = code in real_codes
        status, risk, icon = classify_stock(r, in_real)
        ind_total = industry_counts.get(r.get("stock_info", {}).get("所属行业", ""), 0)
        row = _fmt_row(r, name, code, notice_detail.get(code, []), ind_total)
        row.update({"status": status, "risk_level": risk, "icon": icon,
                    "in_real_list": in_real, "in_abnormal": code in abnormal_set,
                    "in_action": code in action_set,
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

    # ---- 第四层：排序 ----
    print("\n【第四层】预警分级排序...")
    triggered.sort(key=lambda x: -x["priority_score"])
    confirmed.sort(key=lambda x: -x["priority_score"] if x["priority_score"] else -x["max_ratio"])
    near.sort(key=lambda x: -x["max_ratio"])
    watching.sort(key=lambda x: -x["max_ratio"])

    # ---- 状态机：出监管倒计时 ----
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
                new_state[code] = dict(state[code])
                new_state[code]["left_days"] = left
                new_state[code]["last_dev"] = round(r["max_dev"], 2)
    for code, rec in state.items():       # 停牌/拉取失败保留，不误退出
        if code not in analyzed:
            new_state[code] = rec
    exited = [{"code": c, "name": state[c].get("name", c)}
              for c in state if c not in new_state and c in analyzed]
    monitoring_list = sorted([
        {"code": c, "name": rec.get("name", c), "enter_date": rec.get("enter_date", ""),
         "left_days": int(rec.get("left_days", MONITOR_DAYS)), "index_name": rec.get("index_name", ""),
         "last_dev": rec.get("last_dev", 0), "last_win": rec.get("last_win", ""),
         "leaving": int(rec.get("left_days", MONITOR_DAYS)) <= 3}
        for c, rec in new_state.items()], key=lambda x: (x["left_days"], -abs(x["last_dev"] or 0)))
    save_state(state_path, new_state)

    # ---- 晚间黄转红：白天🟡(cache=yellow)、现在进入真实名单变🔴 ----
    upgraded = [row for row in confirmed if prev_cache.get(row["code"]) == "yellow"]
    cache = {row["code"]: "yellow" for row in triggered}
    cache.update({row["code"]: "red" for row in confirmed})
    save_state(cache_path, cache)

    print(f"\n【汇总】🔴确认{len(confirmed)} 🟡触线未监控{len(triggered)} 🔵未触线被监管{len(monitored)} "
          f"临近{len(near)} 观察{len(watching)} 倒计时{len(monitoring_list)} 退出{len(exited)} 黄转红{len(upgraded)}")
    return {"confirmed": confirmed, "triggered": triggered, "monitored": monitored,
            "near": near, "watching": watching, "safe": safe,
            "monitoring_list": monitoring_list, "exited": exited, "upgraded": upgraded,
            "real_list": real_codes, "abnormal_set": abnormal_set, "action_set": action_set,
            "state": new_state}


# ============================================================
# 生成报告文本
# ============================================================
def generate_report(alert_result, today):
    lines = []
    # 晚间黄转红置顶
    if alert_result.get("upgraded"):
        lines.append("■ 🔴🔴 状态升级（白天🟡触线未公告 → 现已被交易所公告确认）")
        for r in alert_result["upgraded"]:
            lines.append(f"  ⚡{r['name']}({r['code']}) {r['lianban']}连板 补录风险分:{r['priority_score'] or '—'}")
            for n in r.get("notices", [])[:2]:
                lines.append(f"     📢{n}")
        lines.append("")
    if alert_result["confirmed"]:
        lines.append(f"■ 🔴 已确认监控（技术触线+交易所公告，共{len(alert_result['confirmed'])}只）")
        for r in alert_result["confirmed"][:20]:
            lines.append(f"  {r['name']}({r['code']}) {r['lianban']}连板 换手{r['turnover']:.0f}%｜基准{r['index_name']}")
            lines.append(f"     {r['detail']}")
            for n in r.get("notices", [])[:2]:
                lines.append(f"     📢{n}")
        lines.append("")
    if alert_result["triggered"]:
        lines.append(f"■ 🟡 触线未监控（暂无公告，按被补录概率从高到低，共{len(alert_result['triggered'])}只）")
        for i, r in enumerate(alert_result["triggered"][:20], 1):
            pos = f" {r['position']}" if r.get("position") else ""
            lines.append(f"  {i}. {r['name']}({r['code']}) 风险分:{r['priority_score']} "
                         f"{r['lianban']}连板 换手{r['turnover']:.0f}%{pos}")
            lines.append(f"     {r['detail']}")
        lines.append("")
    if alert_result["monitored"]:
        lines.append(f"■ 🔵 未触线但被监管（警示函/处罚等非价格原因，共{len(alert_result['monitored'])}只）")
        for r in alert_result["monitored"][:12]:
            one = r.get("notices", [""])[0]
            lines.append(f"  {r['name']}({r['code']}) {one}")
        lines.append("")
    if alert_result["near"]:
        lines.append(f"■ 🟠 高度临近红线（进度≥80%，共{len(alert_result['near'])}只）")
        for r in alert_result["near"][:10]:
            lines.append(f"  {r['name']}({r['code']}) 进度{r['max_ratio']*100:.0f}% {r['lianban']}连板｜{r['detail']}")
        lines.append("")
    if alert_result["watching"]:
        lines.append("■ 🟢 观察区（进度50%-80%）：" +
                     "、".join(f"{r['name']}({r['max_ratio']*100:.0f}%)" for r in alert_result["watching"][:15]))
        lines.append("")
    if alert_result["monitoring_list"]:
        lines.append(f"■ 🔵 出监管倒计时（触线观察期，每交易日递减，归零退出，共{len(alert_result['monitoring_list'])}只）")
        for r in alert_result["monitoring_list"][:20]:
            mark = " 🟢即将退出" if r["leaving"] else ""
            lines.append(f"  {r['name']}({r['code']}) 自{r['enter_date']}起 剩余{r['left_days']}交易日{mark}，"
                         f"最近{r['last_win']}日偏离{r['last_dev']:+.2f}%")
        lines.append("")
    if alert_result["exited"]:
        lines.append("■ 🟢 今日退出观察期：" + "、".join(f"{r['name']}({r['code']})" for r in alert_result["exited"]))
        lines.append("")
    if not any([alert_result["confirmed"], alert_result["triggered"], alert_result["near"], alert_result["monitoring_list"]]):
        lines.append("今日涨停股未出现技术触线，暂无监管预警。")
        lines.append("")
    lines.append("■ 数据与规则说明")
    lines.append("  技术红线：3日偏离(主板±20%/创业板科创板±30%)、10日+100%、30日+200%，相对所属基准指数。")
    lines.append(f"  真实名单：{len(alert_result['real_list'])}只（交易所公告接口，异常波动公告多在17-19点发布，晚间会再更新一次）。")
    lines.append("  触线≠被监控，被监控≠因触线；两层交叉后结论更可靠。本提醒为公开规则演算，不构成投资建议，最终以交易所文件为准。")
    return "\n".join(lines)
