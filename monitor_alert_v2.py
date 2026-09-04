# -*- coding: utf-8 -*-
"""
监管预警系统 v2 —— 四层逻辑完整实现
============================================================================
第一层：技术触发判断（纯计算）
  - 3日累计偏离 ±20%
  - 10日累计偏离 +100%
  - 30日累计偏离 +200%

第二层：真实监控名单抓取（数据源）
  - 上交所监管公告
  - 深交所监管公告
  - 抓取失败时降级为仅技术判断

第三层：交叉验证与分类输出
  - 🔴 已确认监控（技术触发 + 在真实名单）
  - 🟡 触线未监控（技术触发 + 不在名单）
  - 🔵 未触线但被监控（未触发 + 在名单）
  - 🟢 安全（未触发 + 不在名单）

第四层：预警分级
  - 对"触线未监控"按超标幅度、连板数、换手率、板块地位排序
  - 预测被补录监控的概率

状态持久化：monitor_state.json
  - 出监管倒计时（纳入后10个交易日）
  - 归零自动退出，再度触线重新纳入

数据源：新浪个股日K + 新浪指数日K（GitHub Actions可达、免key）
"""
import os
import json
import time
import re
import requests
import akshare as ak
import pandas as pd
from datetime import datetime
from html.parser import HTMLParser

# ============================================================
# 可配置参数
# ============================================================
# 股票代码前缀 -> 基准指数（新浪代码，注意前缀！）
INDEX_MAP = [
    ("68", "sh000688", "科创50"),    # 科创板（必须排在60前面）
    ("60", "sh000001", "上证指数"),   # 沪市主板
    ("30", "sz399006", "创业板指"),   # 创业板
    ("00", "sz399001", "深证成指"),   # 深市主板
]

# (窗口交易日, 阈值%，是否双向)
DEV_WINDOWS = [(3, 20, True), (10, 100, False), (30, 200, False)]

MONITOR_DAYS = 10        # 纳入重点监控后的监控期（交易日）
NEAR_RATIO = 0.8         # 进度≥80% 列为"高度临近"
WATCH_RATIO = 0.5        # 进度≥50% 列为"监控中"
FETCH_RETRY = 2          # 单只股票拉取失败重试次数

_index_cache = {}        # 指数收盘价缓存


# ============================================================
# 工具函数
# ============================================================
def match_index(code):
    """按代码前缀匹配基准指数，返回(新浪代码, 名称)"""
    code = str(code)
    for pre, sym, name in INDEX_MAP:
        if code.startswith(pre):
            return sym, name
    return None


def _sina_symbol(code):
    """个股代码转新浪行情代码；北交所返回None"""
    code = str(code).zfill(6)
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("0", "2", "3")):
        return "sz" + code
    return None


def _get_index_closes(sym):
    """指数日K收盘价（升序list），带缓存与重试"""
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
    """个股日K收盘价（升序list），带重试；用新浪接口（GitHub可达）"""
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
def _calc_windows(stock_closes, index_closes):
    """
    按交易日位置对齐，算三个窗口偏离。
    关键修正：用 N+1 个数据点（期初=-N-1，期末=-1）
    返回 [{win,thr,bidirectional,stock_pct,index_pct,dev,ratio,hit}]
    """
    res = []
    for win, thr, bidir in DEV_WINDOWS:
        # 关键修正：需要 win+1 个数据点
        if len(stock_closes) < win + 1 or len(index_closes) < win + 1:
            res.append({"win": win, "thr": thr, "ok": False})
            continue
        # 期初 = 第 -win-1 个，期末 = 第 -1 个
        s0, s1 = stock_closes[-win - 1], stock_closes[-1]
        i0, i1 = index_closes[-win - 1], index_closes[-1]
        s_pct = (s1 / s0 - 1) * 100
        i_pct = (i1 / i0 - 1) * 100
        dev = s_pct - i_pct
        # 是否触线：双向看绝对值，单向只看正向上限
        hit = abs(dev) >= thr if bidir else dev >= thr
        ratio = (abs(dev) if bidir else dev) / thr
        res.append({
            "win": win, "thr": thr, "bidirectional": bidir, "ok": True,
            "stock_pct": s_pct, "index_pct": i_pct, "dev": dev,
            "ratio": ratio, "hit": hit
        })
    return res


def analyze_one(code, stock_info=None):
    """
    对单只股票计算三窗口偏离，返回分析结果dict；数据不足返回None。
    stock_info: 可选，传入涨停池的额外信息（连板数、换手率、所属行业等）
    """
    code = str(code).zfill(6)
    matched = match_index(code)
    if matched is None:
        return None
    idx_sym, idx_name = matched
    sc = _get_stock_closes(code)
    ic = _get_index_closes(idx_sym)
    if not sc or not ic:
        return None
    wins = _calc_windows(sc, ic)
    valid = [w for w in wins if w.get("ok")]
    if not valid:
        return None
    max_ratio = max(w["ratio"] for w in valid)
    hit_wins = [w for w in valid if w["hit"]]
    max_dev_win = max(valid, key=lambda w: abs(w["dev"]))
    return {
        "code": code,
        "index_name": idx_name,
        "index_sym": idx_sym,
        "windows": valid,
        "max_ratio": max_ratio,
        "hit": len(hit_wins) > 0,
        "max_dev": max_dev_win["dev"],
        "max_dev_win": max_dev_win["win"],
        "stock_info": stock_info or {},
    }


# ============================================================
# 第二层：真实监控名单抓取
# ============================================================
class MonitorListFetcher:
    """从交易所官网抓取真实重点监控名单"""

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

    def fetch_sse(self, date_str=None):
        """
        抓取上交所重点监控名单
        date_str: 日期字符串 YYYYMMDD，None表示今天
        返回: {code: name} 字典
        """
        print("  [名单] 尝试抓取上交所监管公告...")
        result = {}
        try:
            # 上交所监管措施页面
            url = "http://www.sse.com.cn/disclosure/credibility/supervision/measures/"
            resp = requests.get(url, headers=self.headers, timeout=15)
            if resp.status_code != 200:
                print(f"  [名单] 上交所页面状态码: {resp.status_code}")
                return result

            # 从页面中提取股票代码和名称（6位数字+中文名称）
            # 匹配模式：6位数字代码 + 后面跟着的中文名称
            text = resp.text
            # 提取所有6位数字代码
            codes = re.findall(r'\b(6\d{5}|0\d{5}|3\d{5})\b', text)
            # 提取中文名称（2-6个中文字符，紧跟在代码附近）
            # 简化处理：只返回代码列表，名称留空
            for code in set(codes):
                result[code] = ""
            print(f"  [名单] 上交所提取到 {len(result)} 个代码")
        except Exception as e:
            print(f"  [名单] 上交所抓取失败: {e}")
        return result

    def fetch_szse(self, date_str=None):
        """
        抓取深交所重点监控名单
        返回: {code: name} 字典
        """
        print("  [名单] 尝试抓取深交所监管公告...")
        result = {}
        try:
            url = "http://www.szse.cn/disclosure/supervision/inquire/index.html"
            resp = requests.get(url, headers=self.headers, timeout=15)
            if resp.status_code != 200:
                print(f"  [名单] 深交所页面状态码: {resp.status_code}")
                return result
            text = resp.text
            codes = re.findall(r'\b(6\d{5}|0\d{5}|3\d{5})\b', text)
            for code in set(codes):
                result[code] = ""
            print(f"  [名单] 深交所提取到 {len(result)} 个代码")
        except Exception as e:
            print(f"  [名单] 深交所抓取失败: {e}")
        return result

    def get_full_list(self, date_str=None):
        """获取两市完整监控名单，返回 {code: name}"""
        sse = self.fetch_sse(date_str)
        szse = self.fetch_szse(date_str)
        full = {**sse, **szse}
        print(f"  [名单] 真实监控名单共 {len(full)} 只")
        return full


# ============================================================
# 第三层：交叉验证与分类
# ============================================================
def classify_stock(analysis, in_real_list):
    """
    交叉验证分类
    返回: status, risk_level, status_icon
    """
    is_triggered = analysis["hit"] if analysis else False

    if is_triggered and in_real_list:
        return "已确认监控", "高", "🔴"
    elif is_triggered and not in_real_list:
        return "触线未监控", "中", "🟡"
    elif not is_triggered and in_real_list:
        return "未触线但被监控", "高", "🔵"
    else:
        return "安全", "低", "🟢"


# ============================================================
# 第四层：预警分级（对"触线未监控"排序）
# ============================================================
def calc_priority_score(analysis):
    """
    计算"触线未监控"股票的优先级分数
    分数越高，被补录监控的概率越大
    维度：超标幅度、连板数、换手率、板块地位
    """
    score = 0
    # 1. 超标幅度（最大进度 * 50分）
    score += min(analysis["max_ratio"], 3.0) / 3.0 * 50

    # 2. 连板数（最多20分）
    stock_info = analysis.get("stock_info", {})
    lianban = stock_info.get("连板数", 1)
    score += min(lianban, 5) / 5.0 * 20

    # 3. 换手率异常（最多15分）
    turnover = stock_info.get("换手率", 0)
    if turnover > 20:
        score += 15
    elif turnover > 10:
        score += 10
    elif turnover > 5:
        score += 5

    # 4. 板块地位（最多15分）
    position = stock_info.get("板块地位", "")
    if "龙头" in position:
        score += 15
    elif "跟风" in position:
        score += 8
    elif "补涨" in position:
        score += 3

    return round(score, 1)


# ============================================================
# 状态持久化
# ============================================================
def load_state(path):
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"  [监管] 状态文件读取失败({e})，按空状态开始")
    return {}


def save_state(path, state):
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [监管] 状态文件保存失败:{e}")


# ============================================================
# 格式化输出
# ============================================================
def _fmt_row(r, name, code):
    """格式化单只股票的三窗口明细"""
    detail = []
    for w in r["windows"]:
        if not w.get("ok"):
            continue
        sign = "±" if w["bidirectional"] else "+"
        detail.append(
            f"{w['win']}日偏离{w['dev']:+.2f}%/{sign}{w['thr']}%"
            f"(进度{w['ratio']*100:.0f}%)"
        )
    return {
        "code": code, "name": name, "index_name": r["index_name"],
        "max_ratio": r["max_ratio"], "max_dev": r["max_dev"],
        "max_dev_win": r["max_dev_win"], "detail": "；".join(detail),
    }


# ============================================================
# 主入口
# ============================================================
def build_monitor_alert(zt_df, today, state_path="monitor_state.json",
                        extra_codes=None, fetch_real_list=True):
    """
    主入口：四层监管预警系统
    参数:
        zt_df: 当日涨停池DataFrame，需含"代码""名称"列
        today: 日期字符串 YYYYMMDD
        state_path: 状态文件路径
        extra_codes: 额外需要分析的股票代码列表
        fetch_real_list: 是否抓取真实监控名单（第二层）
    返回: dict，包含各分类列表和状态
    """
    print("=" * 60)
    print("监管预警系统 v2 — 四层逻辑")
    print("=" * 60)

    state = load_state(state_path)

    # ---- 第二层：抓取真实监控名单 ----
    real_list = {}
    if fetch_real_list:
        print("\n【第二层】抓取真实监控名单...")
        try:
            fetcher = MonitorListFetcher()
            real_list = fetcher.get_full_list(today)
        except Exception as e:
            print(f"  [名单] 抓取异常: {e}，降级为仅技术判断")
    else:
        print("\n【第二层】跳过真实名单抓取（用户指定）")

    # ---- 待分析股票池 ----
    todo = {}
    if zt_df is not None and not zt_df.empty:
        for _, r in zt_df.iterrows():
            c = str(r.get("代码", "")).zfill(6)
            if c:
                # 保存涨停池的额外信息
                stock_info = {
                    "连板数": int(r.get("连板数", 1) or 1),
                    "换手率": float(r.get("换手率", 0) or 0),
                    "所属行业": str(r.get("所属行业", "")),
                    "板块地位": str(r.get("板块地位", "")),
                    "封板时间": str(r.get("封板时间", "")),
                }
                todo[c] = (str(r.get("名称", c)), stock_info)
    # 已在监控中的股票，即使今天没涨停也要更新
    for c, info in state.items():
        if c not in todo:
            todo[c] = (info.get("name", c), {})
    for c in (extra_codes or []):
        c = str(c).zfill(6)
        if c not in todo:
            todo[c] = (c, {})

    print(f"\n【第一层】技术触发判断，共 {len(todo)} 只股票...")

    # ---- 第一层：技术触发判断 ----
    analyzed = {}
    for i, (code, (name, stock_info)) in enumerate(todo.items()):
        r = analyze_one(code, stock_info)
        if r:
            r["name"] = name
            analyzed[code] = r
        if (i + 1) % 10 == 0:
            print(f"  已分析 {i+1}/{len(todo)}")
        time.sleep(0.1)

    print(f"  技术分析完成，有效 {len(analyzed)} 只")

    # ---- 第三层：交叉验证与分类 ----
    print("\n【第三层】交叉验证与分类...")
    confirmed = []   # 🔴 已确认监控
    triggered = []   # 🟡 触线未监控
    monitored = []   # 🔵 未触线但被监控
    near = []        # 高度临近（未触线但进度≥80%）
    watching = []    # 监控中（未触线但进度≥50%）
    safe = []        # 🟢 安全

    for code, r in analyzed.items():
        name = r["name"]
        in_real = code in real_list
        status, risk, icon = classify_stock(r, in_real)
        row = _fmt_row(r, name, code)
        row["status"] = status
        row["risk_level"] = risk
        row["icon"] = icon
        row["in_real_list"] = in_real
        row["priority_score"] = calc_priority_score(r) if status == "触线未监控" else 0

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

    # ---- 第四层：预警分级排序 ----
    print("\n【第四层】预警分级排序...")
    triggered.sort(key=lambda x: -x["priority_score"])
    near.sort(key=lambda x: -x["max_ratio"])
    watching.sort(key=lambda x: -x["max_ratio"])
    confirmed.sort(key=lambda x: -x["max_ratio"])

    # ---- 状态机推进（出监管倒计时） ----
    print("\n【状态机】推进出监管倒计时...")
    new_state = {}
    for code, r in analyzed.items():
        name = r["name"]
        # 触线：新纳入或续期
        if r["hit"]:
            is_new = code not in state
            state_rec = state.get(code, {})
            new_state[code] = {
                "name": name,
                "enter_date": state_rec.get("enter_date", today),
                "left_days": MONITOR_DAYS,
                "index_name": r["index_name"],
                "last_dev": round(r["max_dev"], 2),
                "last_win": r["max_dev_win"],
            }
        elif code in state:
            # 已在监控、今天未触线：倒计时-1
            left = int(state[code].get("left_days", MONITOR_DAYS)) - 1
            if left <= 0:
                continue  # 归零，退出
            new_state[code] = dict(state[code])
            new_state[code]["left_days"] = left
            new_state[code]["last_dev"] = round(r["max_dev"], 2)
            new_state[code]["last_win"] = r["max_dev_win"]

    # 今天没分析到的（停牌/拉取失败），保留不递减
    for code, rec in state.items():
        if code not in analyzed:
            new_state[code] = rec

    exited = [
        {"code": c, "name": state[c].get("name", c)}
        for c in state if c not in new_state and c in analyzed
    ]

    # 监控中列表
    monitoring_list = []
    for code, rec in new_state.items():
        monitoring_list.append({
            "code": code,
            "name": rec.get("name", code),
            "enter_date": rec.get("enter_date", ""),
            "left_days": int(rec.get("left_days", MONITOR_DAYS)),
            "index_name": rec.get("index_name", ""),
            "last_dev": rec.get("last_dev", 0),
            "last_win": rec.get("last_win", ""),
            "leaving": int(rec.get("left_days", MONITOR_DAYS)) <= 3,
        })
    monitoring_list.sort(key=lambda x: (x["left_days"], -abs(x["last_dev"] or 0)))

    save_state(state_path, new_state)

    # ---- 汇总输出 ----
    print(f"\n【汇总】")
    print(f"  🔴 已确认监控: {len(confirmed)}")
    print(f"  🟡 触线未监控: {len(triggered)}")
    print(f"  🔵 未触线但被监控: {len(monitored)}")
    print(f"  ⚠️  高度临近: {len(near)}")
    print(f"  👀 监控中: {len(watching)}")
    print(f"  🟢 安全: {len(safe)}")
    print(f"  📋 状态机监控中: {len(monitoring_list)}")
    print(f"  🚪 今日退出: {len(exited)}")
    print(f"  📋 真实名单: {len(real_list)}")

    return {
        "confirmed": confirmed,       # 🔴 已确认监控
        "triggered": triggered,       # 🟡 触线未监控（按优先级排序）
        "monitored": monitored,       # 🔵 未触线但被监控
        "near": near,                 # ⚠️ 高度临近
        "watching": watching,         # 👀 监控中
        "safe": safe,                 # 🟢 安全
        "monitoring_list": monitoring_list,  # 📋 状态机监控中
        "exited": exited,             # 🚪 今日退出
        "real_list": real_list,       # 📋 真实监控名单
        "state": new_state,
    }


# ============================================================
# 生成报告文本
# ============================================================
def generate_report(alert_result, today):
    """生成监管预警报告文本"""
    lines = []
    lines.append(f"【监管预警】{today}")
    lines.append("")

    # 🔴 已确认监控
    if alert_result["confirmed"]:
        lines.append(f"■ 🔴 已确认监控（触线+被交易所盯上）共{len(alert_result['confirmed'])}只")
        for r in alert_result["confirmed"][:20]:
            lines.append(f"  {r['icon']} {r['name']}({r['code']}) | {r['index_name']} | {r['detail']}")
        lines.append("")

    # 🟡 触线未监控（重点！按补录概率排序）
    if alert_result["triggered"]:
        lines.append(f"■ 🟡 触线未监控（触线但暂未被盯上，存在补录风险）共{len(alert_result['triggered'])}只")
        lines.append("  （按被补录监控概率从高到低排序）")
        for i, r in enumerate(alert_result["triggered"][:20], 1):
            lines.append(f"  {i}. {r['name']}({r['code']}) 优先级:{r['priority_score']}分 | {r['index_name']} | {r['detail']}")
        lines.append("")

    # 🔵 未触线但被监控
    if alert_result["monitored"]:
        lines.append(f"■ 🔵 未触线但被监控（可能因其他原因被盯上）共{len(alert_result['monitored'])}只")
        for r in alert_result["monitored"][:10]:
            lines.append(f"  {r['icon']} {r['name']}({r['code']}) | {r['detail']}")
        lines.append("")

    # ⚠️ 高度临近
    if alert_result["near"]:
        lines.append(f"■ ⚠️  高度临近（偏离进度≥80%，随时可能触线）共{len(alert_result['near'])}只")
        for r in alert_result["near"][:10]:
            lines.append(f"  {r['name']}({r['code']}) 最大进度:{r['max_ratio']*100:.0f}% | {r['detail']}")
        lines.append("")

    # 📋 状态机监控中
    if alert_result["monitoring_list"]:
        lines.append(f"■ 📋 重点监控名单（状态机，含出监管倒计时）共{len(alert_result['monitoring_list'])}只")
        for r in alert_result["monitoring_list"][:20]:
            leave_mark = " ⚠️即将退出" if r["leaving"] else ""
            lines.append(f"  {r['name']}({r['code']}) 纳入:{r['enter_date']} 剩余{r['left_days']}天{leave_mark} | 基准:{r['index_name']} | 最近偏离:{r['last_dev']:+.2f}%({r['last_win']}日)")
        lines.append("")

    # 🚪 今日退出
    if alert_result["exited"]:
        lines.append(f"■ 🚪 今日退出监控（倒计时归零）共{len(alert_result['exited'])}只")
        for r in alert_result["exited"]:
            lines.append(f"  {r['name']}({r['code']})")
        lines.append("")

    # 数据来源说明
    lines.append(f"■ 数据说明")
    lines.append(f"  真实监控名单: {len(alert_result['real_list'])}只（来自交易所公告抓取，可能有延迟）")
    lines.append(f"  技术判断: 3日±20% / 10日+100% / 30日+200%")
    lines.append(f"  注: 触线≠被监控，被监控≠因触线。两层数据交叉验证后结论更可靠。")

    return "\n".join(lines)
