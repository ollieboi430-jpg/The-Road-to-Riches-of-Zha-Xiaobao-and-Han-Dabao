# -*- coding: utf-8 -*-
"""
监管提醒模块 —— 严重异常波动（重点监控）进/出预判
============================================================================
【进监管预判】每天收盘后，对股票算三个时间窗口相对基准指数的累计偏离值：
    偏离值 = (股票期末/期初 - 1)*100% - (指数期末/期初 - 1)*100%
    窗口与阈值（主板口径，可在 DEV_WINDOWS 调整）：
      最近 3 个交易日  累计偏离 ±20%
      最近10 个交易日  累计偏离 +100%
      最近30 个交易日  累计偏离 +200%
    进度 = 当前偏离 / 阈值 *100%，据此分级。
【出监管倒计时】被纳入重点监控后，每个交易日 left_days-1，归零自动退出；
    退出后若再度触线，重新纳入。状态持久化在 monitor_state.json（随仓库保存）。
数据源：新浪个股日K stock_zh_a_daily + 新浪指数日K stock_zh_index_daily（海外可达、免key）
"""
import os
import json
import time
import akshare as ak
import pandas as pd

# ===== 可配置参数 =====
# 股票代码前缀 -> 基准指数（新浪代码）
INDEX_MAP = [
    ("68", "sh000688", "科创50"),    # 科创板（需排在60前面，因为68也以6开头）
    ("60", "sh000001", "上证指数"),   # 沪市主板
    ("30", "sz399006", "创业板指"),   # 创业板
    ("00", "sz399001", "深证成指"),   # 深市主板
]
# (窗口交易日, 阈值%，是否双向)；3日双向±，10/30日按用户规则只看上涨方向
DEV_WINDOWS_MAIN = [(3, 20, True), (10, 100, False), (30, 200, False)]
DEV_WINDOWS_GROWTH_TECH = [(3, 30, True), (10, 100, False), (30, 200, False)]


def _windows_for_code(code):
    """主板3日±20%；创业板(30)和科创板(68) 3日±30%。"""
    code = str(code).zfill(6)
    if code.startswith(("30", "68")):
        return DEV_WINDOWS_GROWTH_TECH
    return DEV_WINDOWS_MAIN


MONITOR_DAYS = 10        # 纳入重点监控后的监控期（交易日），可调
NEAR_RATIO = 0.8         # 进度≥80% 列为“高度临近”
WATCH_RATIO = 0.5        # 进度≥50% 列为“监控中”
FETCH_RETRY = 2          # 单只股票拉取失败重试次数

_index_cache = {}        # 指数收盘价缓存，一次运行只拉一次


def match_index(code):
    """按代码前缀匹配基准指数，返回(新浪代码, 名称)；北交所等规则未覆盖的返回None。"""
    code = str(code)
    for pre, sym, name in INDEX_MAP:
        if code.startswith(pre):
            return sym, name
    return None  # 北交所(4/8/920)等：用户规则未给基准，不参与计算


def _sina_symbol(code):
    """个股代码转新浪行情代码（沪深）；北交所返回None。"""
    code = str(code).zfill(6)
    if code.startswith(("6", "9")):   # 沪市主板/科创板/沪B
        return "sh" + code
    if code.startswith(("0", "2", "3")):  # 深主板/深B/创业板
        return "sz" + code
    return None                      # 北交所 4/8/920 新浪日线不支持


def _get_index_closes(sym):
    """指数日K收盘价（升序list），带缓存与重试。"""
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
    """个股日K收盘价（升序list），带重试；北交所等不支持的返回None。"""
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


def _calc_windows(stock_closes, index_closes, windows):
    """
    按交易日位置对齐，算三个窗口偏离。
    返回 [{win,thr,bidirectional,stock_pct,index_pct,dev,ratio,hit}]，ratio=进度(取正)。
    """
    res = []
    for win, thr, bidir in windows:
        if len(stock_closes) < win + 1 or len(index_closes) < win + 1:
            res.append({"win": win, "thr": thr, "ok": False})
            continue
        s0, s1 = stock_closes[-win - 1], stock_closes[-1]
        i0, i1 = index_closes[-win - 1], index_closes[-1]
        s_pct = (s1 / s0 - 1) * 100
        i_pct = (i1 / i0 - 1) * 100
        dev = s_pct - i_pct
        # 是否触线：双向看绝对值，单向只看正向上限
        hit = abs(dev) >= thr if bidir else dev >= thr
        ratio = (abs(dev) if bidir else dev) / thr
        res.append({"win": win, "thr": thr, "bidirectional": bidir, "ok": True,
                    "stock_pct": s_pct, "index_pct": i_pct, "dev": dev,
                    "ratio": ratio, "hit": hit})
    return res


def analyze_one(code):
    """对单只股票计算三窗口偏离，返回分析结果dict；数据不足返回None。"""
    code = str(code).zfill(6)
    matched = match_index(code)
    if matched is None:
        return None  # 北交所等无对应基准指数，规则未覆盖，跳过
    idx_sym, idx_name = matched
    sc = _get_stock_closes(code)
    ic = _get_index_closes(idx_sym)
    if not sc or not ic:
        return None
    wins = _calc_windows(sc, ic, _windows_for_code(code))
    valid = [w for w in wins if w.get("ok")]
    if not valid:
        return None
    max_ratio = max(w["ratio"] for w in valid)          # 最大进度
    hit_wins = [w for w in valid if w["hit"]]           # 触线窗口
    max_dev_win = max(valid, key=lambda w: abs(w["dev"]))
    return {
        "code": code, "index_name": idx_name, "index_sym": idx_sym,
        "windows": valid, "max_ratio": max_ratio,
        "hit": len(hit_wins) > 0,
        "max_dev": max_dev_win["dev"], "max_dev_win": max_dev_win["win"],
    }


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


def build_monitor_alert(zt_df, today, state_path="monitor_state.json", extra_codes=None):
    """
    主入口：对涨停池股票做进监管预判，并推进出监管倒计时状态机。
    参数 zt_df：当日涨停池DataFrame，需含“代码”“名称”列（东财涨停池）。
    返回 dict：new_hits / near / watching / monitoring / exited / state
    """
    state = load_state(state_path)
    # 待分析：今日涨停股 + 已在监控中的股票（后者即使今天没涨停也要更新倒计时）
    todo = {}
    if zt_df is not None and not zt_df.empty:
        for _, r in zt_df.iterrows():
            c = str(r.get("代码", "")).zfill(6)
            if c:
                todo[c] = str(r.get("名称", c))
    for c, info in state.items():
        todo.setdefault(c, info.get("name", c))
    for c in (extra_codes or []):
        todo.setdefault(str(c).zfill(6), str(c))

    today_hit_codes = set()
    analyzed = {}
    for i, (code, name) in enumerate(todo.items()):
        r = analyze_one(code)
        if r:
            r["name"] = name
            analyzed[code] = r
            if r["hit"]:
                today_hit_codes.add(code)
        time.sleep(0.15)

    new_hits, near, watching = [], [], []
    # ===== 状态机推进 =====
    new_state = {}
    for code, r in analyzed.items():
        name = r["name"]
        if r["hit"]:
            # 触线：新纳入 或 已在监控则续期（重置倒计时）
            is_new = code not in state
            state_rec = state.get(code, {})
            new_state[code] = {
                "name": name, "enter_date": state_rec.get("enter_date", today),
                "left_days": MONITOR_DAYS, "index_name": r["index_name"],
                "last_dev": round(r["max_dev"], 2), "last_win": r["max_dev_win"],
            }
            row = _fmt_row(r, name, code)
            if is_new:
                row["enter_date"] = new_state[code]["enter_date"]
                new_hits.append(row)
        elif code in state:
            # 已在监控、今天未触线：倒计时-1
            left = int(state[code].get("left_days", MONITOR_DAYS)) - 1
            if left <= 0:
                continue  # 归零，退出监控（不进new_state）
            new_state[code] = dict(state[code])
            new_state[code]["left_days"] = left
            new_state[code]["last_dev"] = round(r["max_dev"], 2)
            new_state[code]["last_win"] = r["max_dev_win"]
        else:
            # 未触线、未在监控：按进度给临近/观察提示
            row = _fmt_row(r, name, code)
            if r["max_ratio"] >= NEAR_RATIO:
                near.append(row)
            elif r["max_ratio"] >= WATCH_RATIO:
                watching.append(row)

    # 今天在旧state、但本次没分析到（停牌/拉取失败）的，保留不递减，避免误退出
    for code, rec in state.items():
        if code not in analyzed:
            new_state[code] = rec

    exited = [{"code": c, "name": state[c].get("name", c)}
              for c in state if c not in new_state and c in analyzed]

    monitoring = []
    for code, rec in new_state.items():
        monitoring.append({
            "code": code, "name": rec.get("name", code),
            "enter_date": rec.get("enter_date", ""),
            "left_days": int(rec.get("left_days", MONITOR_DAYS)),
            "index_name": rec.get("index_name", ""),
            "last_dev": rec.get("last_dev", 0), "last_win": rec.get("last_win", ""),
            "leaving": int(rec.get("left_days", MONITOR_DAYS)) <= 3,
        })
    monitoring.sort(key=lambda x: (x["left_days"], -abs(x["last_dev"] or 0)))
    near.sort(key=lambda x: -x["max_ratio"])
    watching.sort(key=lambda x: -x["max_ratio"])
    new_hits.sort(key=lambda x: -x["max_ratio"])

    save_state(state_path, new_state)
    print(f"[监管] 分析{len(analyzed)}只：新触线{len(new_hits)}、临近{len(near)}、"
          f"监控中{len(monitoring)}、今日退出{len(exited)}")
    return {"new_hits": new_hits, "near": near, "watching": watching,
            "monitoring": monitoring, "exited": exited, "state": new_state}


def _fmt_row(r, name, code):
    """格式化单只股票的三窗口明细。"""
    detail = []
    for w in r["windows"]:
        if not w.get("ok"):
            continue
        sign = "±" if w["bidirectional"] else "+"
        detail.append(f"{w['win']}日偏离{w['dev']:+.2f}%/{sign}{w['thr']}%(进度{w['ratio']*100:.0f}%)")
    return {
        "code": code, "name": name, "index_name": r["index_name"],
        "max_ratio": r["max_ratio"], "max_dev": r["max_dev"],
        "max_dev_win": r["max_dev_win"], "detail": "；".join(detail),
    }
