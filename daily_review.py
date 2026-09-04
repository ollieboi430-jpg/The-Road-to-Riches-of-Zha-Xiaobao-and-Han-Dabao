import akshare as ak
import pandas as pd
import os
import smtplib
import time
import requests
import json
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime

today = datetime.now().strftime("%Y%m%d")
today_str = datetime.now().strftime("%Y-%m-%d")

# 1. 获取当日涨停池
try:
    df_zt = ak.stock_zt_pool_em(date=today)
    print(f"当日涨停池: {len(df_zt)}只")
except Exception as e:
    print(f"涨停池失败: {e}")
    df_zt = pd.DataFrame()

# 2. 获取昨日涨停今日表现
try:
    df_prev = ak.stock_zt_pool_previous_em(date=today)
    print(f"昨日涨停池: {len(df_prev)}只")
except Exception as e:
    print(f"昨日涨停失败: {e}")
    df_prev = pd.DataFrame()

# 3. 获取当日公告
try:
    df_notice = ak.stock_notice_report(symbol="全部", date=today)
    good_kw = ["预增", "预盈", "业绩增长", "扭亏", "中标", "合同", "并购", "重组",
               "定增", "股权激励", "摘帽", "分红", "回购", "增持", "战略合作", "获批", "通过"]
    notice_codes = set()
    for _, row in df_notice.iterrows():
        if any(kw in str(row.get("公告标题", "")) for kw in good_kw):
            notice_codes.add(str(row.get("代码", "")).zfill(6))
    print(f"利好公告: {len(notice_codes)}只")
except Exception as e:
    print(f"公告失败: {e}")
    notice_codes = set()

# 4. 获取大盘分时数据
try:
    df_index = ak.index_zh_a_hist_min_em(symbol="000001", period="1",
        start_date=f"{today_str} 09:30:00", end_date=f"{today_str} 15:00:00")
    index_map = {}
    for _, row in df_index.iterrows():
        t = str(row["时间"])
        hm = t.split(" ")[1][:5] if " " in t else t[:5]
        index_map[hm] = float(row["收盘"])
    print(f"大盘分时: {len(index_map)}个点")
except Exception as e:
    print(f"大盘分时失败: {e}")
    index_map = {}

# 5. 获取板块资金流向（直接调用东方财富原始API）
sector_fund_map = {}
try:
    print("正在获取板块资金流向（东方财富原始API）...")
    url = "http://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1,
        "pz": 100,
        "po": 1,
        "np": 1,
        "fltt": 2,
        "invt": 2,
        "fid": "f62",
        "fs": "m:90+t:2",
        "fields": "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87"
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "http://data.eastmoney.com/"
    }
    resp = requests.get(url, params=params, headers=headers, timeout=15)
    print(f"API响应状态码: {resp.status_code}")
    print(f"API响应前200字符: {resp.text[:200]}")

    data = resp.json()
    if data.get("data") and data["data"].get("diff"):
        fund_list = data["data"]["diff"]
        print(f"板块资金流向获取成功，共{len(fund_list)}个板块")

        for item in fund_list:
            name = str(item.get("f14", "")).strip()
            try:
                main_inflow = float(item.get("f62", 0))
                main_ratio = float(item.get("f184", 0))
                super_inflow = float(item.get("f66", 0))
                big_inflow = float(item.get("f72", 0))
                change_pct = float(item.get("f3", 0))
                sector_fund_map[name] = {
                    "main_inflow": main_inflow,
                    "main_ratio": main_ratio,
                    "super_inflow": super_inflow,
                    "big_inflow": big_inflow,
                    "change_pct": change_pct
                }
            except:
                pass
        print(f"板块资金流向解析完成，共{len(sector_fund_map)}个板块")
        print(f"前5个板块: {list(sector_fund_map.keys())[:5]}")
    else:
        print(f"API返回数据为空: {data}")
except Exception as e:
    print(f"板块资金流向获取失败: {e}")
    import traceback
    traceback.print_exc()

# 6. 获取全市场行情（用新浪财经接口）
spot_map = {}

try:
    print("正在获取新浪全市场行情...")
    df_spot = ak.stock_zh_a_spot()
    print(f"新浪全市场行情获取成功，共{len(df_spot)}行")

    for _, row in df_spot.iterrows():
        code_raw = str(row.get("代码", "")).strip()
        code = code_raw[-6:] if len(code_raw) >= 6 else code_raw.zfill(6)
        try:
            open_p = float(row.get("今开", 0))
            high_p = float(row.get("最高", 0))
            low_p = float(row.get("最低", 0))
            if open_p > 0:
                spot_map[code] = {"open": open_p, "high": high_p, "low": low_p}
        except:
            pass
    print(f"新浪行情解析完成，spot_map共{len(spot_map)}只股票")
except Exception as e:
    print(f"新浪全市场行情获取失败: {e}")
    import traceback
    traceback.print_exc()

if len(spot_map) == 0 and not df_prev.empty:
    print("新浪全市场行情失败，改用逐个获取日K线...")
    for idx, row in df_prev.iterrows():
        code = str(row["代码"]).zfill(6)
        if code.startswith(("8", "9", "4")):
            continue
        if code.startswith("6"):
            sina_code = f"sh{code}"
        else:
            sina_code = f"sz{code}"
        try:
            df_daily = ak.stock_zh_a_daily(symbol=sina_code, start_date=today, end_date=today, adjust="")
            if df_daily is not None and len(df_daily) > 0:
                latest = df_daily.iloc[-1]
                spot_map[code] = {
                    "open": float(latest["open"]),
                    "high": float(latest["high"]),
                    "low": float(latest["low"])
                }
                print(f"  {code} {row['名称']} 日K线获取成功")
            time.sleep(0.3)
        except Exception as e:
            print(f"  {code} {row['名称']} 日K线获取失败: {e}")
            time.sleep(0.3)
    print(f"逐个获取完成，spot_map共{len(spot_map)}只股票")

print(f"最终spot_map共{len(spot_map)}只股票")

# ===== 工具函数 =====
def parse_hm(t):
    if pd.isna(t): return None
    text = str(t).strip()
    if ":" in text:
        return f"{int(text.split(':')[0]):02d}:{int(text.split(':')[1]):02d}"
    digits = "".join(c for c in text if c.isdigit()).zfill(6)
    return f"{digits[:2]}:{digits[2:4]}"

def time_to_minutes(hm):
    if hm is None: return 999
    h, m = int(hm[:2]), int(hm[3:5])
    return h * 60 + m

def index_drop_rebound(start_hm, end_hm):
    if not index_map or not start_hm or not end_hm: return 0, 0
    try:
        ts = sorted(index_map.keys())
        si = next((i for i,t in enumerate(ts) if t >= start_hm), None)
        ei = next((i for i,t in enumerate(ts) if t <= end_hm), None)
        if si is None or ei is None or si >= ei: return 0, 0
        seg = [index_map[ts[i]] for i in range(si, ei+1)]
        drop = (min(seg) - seg[0]) / seg[0] * 100
        rebound = (seg[-1] - min(seg)) / min(seg) * 100
        return drop, rebound
    except: return 0, 0

def format_amount(val):
    abs_val = abs(val)
    if abs_val >= 1e8:
        return f"{val/1e8:.2f}亿"
    elif abs_val >= 1e4:
        return f"{val/1e4:.2f}万"
    else:
        return f"{val:.0f}"

# ===== 当日涨停多维度分类 =====
if not df_zt.empty:
    def cls_time(t):
        hm = parse_hm(t)
        if hm is None: return "未知"
        h, m = int(hm[:2]), int(hm[3:5])
        if h == 9 and m <= 25: return "竞价封板"
        if h == 9 and m <= 30: return "开盘秒板"
        if h <= 10: return "早盘封板(9:30-10:00)"
        if h <= 11: return "上午盘封板"
        if h <= 14 and m <= 30: return "午后盘封板"
        return "尾盘偷袭封板"

    def cls_struct(row):
        n = row.get("炸板次数", 0)
        if pd.isna(n): return "未知"
        if n == 0: return "一次封板型"
        if n == 1:
            drop, rebound = index_drop_rebound(parse_hm(row.get("首次封板时间")), parse_hm(row.get("最后封板时间")))
            if drop <= -0.3 and rebound >= 0.2: return "单次开板-大盘回落回封型"
            return "单次开板-自然换手回封型"
        if n <= 3: return "多次开板型"
        return "烂板型"

    def cls_cap(v):
        if pd.isna(v): return "未知"
        c = v / 1e8
        if c < 50: return "小盘股(<50亿)"
        if c < 200: return "中盘股(50-200亿)"
        return "大盘股(>200亿)"

    def cls_price(v):
        if pd.isna(v): return "未知"
        if v < 10: return "低价股(<10元)"
        if v < 50: return "中价股(10-50元)"
        return "高价股(>50元)"

    def cls_active(row):
        lb = row.get("连板数", 1)
        if lb >= 3: return "高位连板(>=3板)"
        if lb == 2: return "2连板"
        zs = str(row.get("涨停统计", ""))
        if "/" in zs:
            try:
                if int(zs.split("/")[1]) >= 3: return "近期活跃(近5天>=3板)"
            except: pass
        return "首板(近期不活跃)"

    industry_counts = df_zt["所属行业"].value_counts().to_dict()

    def cls_reason(row):
        code = str(row.get("代码", "")).zfill(6)
        ind = row.get("所属行业", "")
        lb = row.get("连板数", 1)
        if code in notice_codes: return "公告驱动型"
        if industry_counts.get(ind, 0) >= 3: return "板块驱动型"
        if lb >= 2: return "连板龙头/资金驱动型"
        return "个股独立/消息驱动型"

    def cls_position(row):
        ind = row.get("所属行业", "")
        if industry_counts.get(ind, 0) < 3: return "独立个股(无板块)"
        ind_df = df_zt[df_zt["所属行业"] == ind].sort_values("首次封板时间")
        rank = list(ind_df["代码"]).index(row["代码"]) + 1
        if rank == 1: return "板块领涨龙头"
        if rank <= 3: return "板块跟风"
        return "板块补涨后排"

    df_zt["封板时间"] = df_zt["首次封板时间"].apply(cls_time)
    df_zt["封板结构"] = df_zt.apply(cls_struct, axis=1)
    df_zt["市值"] = df_zt["总市值"].apply(cls_cap)
    df_zt["价格"] = df_zt["最新价"].apply(cls_price)
    df_zt["股性"] = df_zt.apply(cls_active, axis=1)
    df_zt["驱动原因"] = df_zt.apply(cls_reason, axis=1)
    df_zt["板块地位"] = df_zt.apply(cls_position, axis=1)
    df_zt["封板分钟"] = df_zt["首次封板时间"].apply(lambda x: time_to_minutes(parse_hm(x)))
    print("当日涨停分类完成")

# ===== 板块强弱分析 =====
sector_analysis = []
if not df_zt.empty:
    for ind in df_zt["所属行业"].unique():
        ind_df = df_zt[df_zt["所属行业"] == ind]
        count = len(ind_df)
        leader = ind_df.sort_values("首次封板时间").iloc[0]
        leader_name = leader["名称"]
        leader_time = parse_hm(leader["首次封板时间"])
        avg_time = ind_df["封板分钟"].mean()
        one_board_rate = (ind_df["封板结构"] == "一次封板型").sum() / count * 100
        bad_board_rate = (ind_df["封板结构"] == "烂板型").sum() / count * 100
        lianban_count = (ind_df["连板数"] >= 2).sum()
        late_count = (ind_df["封板时间"] == "尾盘偷袭封板").sum()

        fund_info = sector_fund_map.get(ind, None)
        main_inflow = fund_info["main_inflow"] if fund_info else 0
        main_ratio = fund_info["main_ratio"] if fund_info else 0
        fund_status = "无数据"
        if fund_info:
            fund_status = "主力流入" if main_inflow > 0 else "主力流出"

        score = 0
        score += count * 2
        if count >= 3: score += 3
        if avg_time <= 600: score += 2
        elif avg_time >= 870: score -= 2
        if one_board_rate >= 60: score += 2
        if bad_board_rate >= 30: score -= 2
        if lianban_count >= 1: score += 1
        if late_count >= count * 0.5: score -= 2
        if fund_info and main_inflow > 0: score += 2
        if fund_info and main_inflow < 0: score -= 2

        if score >= 8: level = "强势板块"
        elif score >= 3: level = "一般板块"
        else: level = "弱势板块"

        sector_analysis.append({
            "行业": ind, "涨停数": count, "评分": score, "等级": level,
            "领涨龙头": f"{leader_name}({leader_time})",
            "平均封板": f"{int(avg_time//60)}:{int(avg_time%60):02d}",
            "一次封板率": f"{one_board_rate:.0f}%",
            "烂板率": f"{bad_board_rate:.0f}%",
            "连板数": lianban_count, "尾盘数": late_count,
            "主力净流入": main_inflow, "主力净占比": main_ratio, "资金状态": fund_status
        })

    sector_analysis.sort(key=lambda x: -x["评分"])
    strong_sectors = [s for s in sector_analysis if s["等级"] == "强势板块"]
    weak_sectors = [s for s in sector_analysis if s["等级"] == "弱势板块"]
    print(f"板块分析: 强势{len(strong_sectors)}个, 弱势{len(weak_sectors)}个")

# ===== 全市场板块资金流向排名 =====
fund_ranking_in = []
fund_ranking_out = []
if sector_fund_map:
    sorted_fund = sorted(sector_fund_map.items(), key=lambda x: -x[1]["main_inflow"])
    for name, info in sorted_fund[:15]:
        fund_ranking_in.append({
            "板块": name, "主力净流入": info["main_inflow"],
            "主力净占比": info["main_ratio"], "涨跌幅": info["change_pct"],
            "涨停数": industry_counts.get(name, 0) if not df_zt.empty else 0
        })
    for name, info in sorted_fund[-15:]:
        fund_ranking_out.append({
            "板块": name, "主力净流入": info["main_inflow"],
            "主力净占比": info["main_ratio"], "涨跌幅": info["change_pct"],
            "涨停数": industry_counts.get(name, 0) if not df_zt.empty else 0
        })
    fund_ranking_out.reverse()

# ===== 资金流向与涨停股交叉分析 =====
cross_analysis = {"both": [], "zt_no_fund": [], "fund_no_zt": []}
if sector_fund_map and not df_zt.empty:
    for name, info in sector_fund_map.items():
        zt_count = industry_counts.get(name, 0)
        if zt_count >= 2 and info["main_inflow"] > 0:
            cross_analysis["both"].append({"板块": name, "涨停数": zt_count, "主力净流入": info["main_inflow"]})
        elif zt_count >= 2 and info["main_inflow"] < 0:
            cross_analysis["zt_no_fund"].append({"板块": name, "涨停数": zt_count, "主力净流入": info["main_inflow"]})
        elif zt_count == 0 and info["main_inflow"] > 5e7:
            cross_analysis["fund_no_zt"].append({"板块": name, "涨停数": 0, "主力净流入": info["main_inflow"]})
    cross_analysis["both"].sort(key=lambda x: -x["主力净流入"])
    cross_analysis["zt_no_fund"].sort(key=lambda x: x["主力净流入"])
    cross_analysis["fund_no_zt"].sort(key=lambda x: -x["主力净流入"])

# ===== 昨日涨停表现分类 =====
if not df_prev.empty:
    print(f"开始匹配昨日涨停数据，spot_map有{len(spot_map)}只，df_prev有{len(df_prev)}只")
    match_count = 0
    open_list, high_list, low_list = [], [], []
    for idx, row in df_prev.iterrows():
        code_raw = row.get("代码", "")
        code = str(code_raw).zfill(6)
        data = spot_map.get(code, None)
        if data:
            open_list.append(data["open"])
            high_list.append(data["high"])
            low_list.append(data["low"])
            match_count += 1
        else:
            open_list.append(None)
            high_list.append(None)
            low_list.append(None)
            if idx < 5:
                print(f"  未匹配: 代码原始值={code_raw}, 转换后={code}")
    print(f"昨日涨停数据匹配完成: 匹配{match_count}只, 未匹配{len(df_prev)-match_count}只")

    df_prev["今开"] = open_list
    df_prev["最高"] = high_list
    df_prev["最低"] = low_list
    df_prev["昨收"] = df_prev["最新价"] / (1 + df_prev["涨跌幅"] / 100)
    df_prev["开盘涨幅"] = (df_prev["今开"] - df_prev["昨收"]) / df_prev["昨收"] * 100
    df_prev["最高涨幅"] = (df_prev["最高"] - df_prev["昨收"]) / df_prev["昨收"] * 100
    df_prev["最低涨幅"] = (df_prev["最低"] - df_prev["昨收"]) / df_prev["昨收"] * 100
    df_prev["收盘涨幅"] = df_prev["涨跌幅"]

    def cls_sell(row):
        op = row.get("开盘涨幅", 0); cp = row.get("收盘涨幅", 0)
        hp = row.get("最高涨幅", 0); lp = row.get("最低涨幅", 0)
        if pd.isna(op) or pd.isna(hp): return "数据缺失"
        if op >= 3 and cp >= 2 and hp >= op: return "好卖-高开溢价型"
        if cp >= 4 and hp - cp <= 1.5 and cp > op: return "好卖-全天向上型"
        if op >= 2 and cp < 0 and lp < -2: return "坑人-高开下杀型"
        if op < 0 and cp < -2 and hp < 1: return "坑人-水下闷杀型"
        if hp >= 2 and cp < -1 and hp - cp >= 5: return "坑人-冲高闷杀型"
        return "一般可卖型"

    df_prev["卖出类型"] = df_prev.apply(cls_sell, axis=1)
    print("昨日涨停分类完成")

# ===== 打板红黑榜 =====
red_list, black_list = [], []
if not df_zt.empty:
    for _, row in df_zt.iterrows():
        score, reasons = 0, []
        if row["封板时间"] in ["竞价封板", "开盘秒板", "早盘封板(9:30-10:00)"]: score += 2; reasons.append("早盘封板")
        if "回封" in row["封板结构"] or row["封板结构"] == "一次封板型": score += 1; reasons.append("封板结构好")
        if row["板块地位"] == "板块领涨龙头": score += 2; reasons.append("板块龙头")
        if row["连板数"] >= 2: score += 1; reasons.append(f"{int(row['连板数'])}连板")
        if row["驱动原因"] == "板块驱动型": score += 1; reasons.append("有板块效应")
        if row["驱动原因"] == "公告驱动型": score += 1; reasons.append("公告利好")
        if row["市值"] == "小盘股(<50亿)": score += 1; reasons.append("小盘股")
        ind = row.get("所属行业", "")
        fund_info = sector_fund_map.get(ind, None)
        if fund_info and fund_info["main_inflow"] > 0: score += 1; reasons.append("板块资金流入")
        if fund_info and fund_info["main_inflow"] < 0: score -= 1; reasons.append("板块资金流出")
        if row["封板时间"] == "尾盘偷袭封板": score -= 2; reasons.append("尾盘偷袭")
        if row["封板结构"] == "烂板型": score -= 2; reasons.append("烂板")
        if row["板块地位"] == "板块补涨后排": score -= 1; reasons.append("后排跟风")
        if row["驱动原因"] == "个股独立/消息驱动型": score -= 1; reasons.append("无板块支撑")
        if score >= 4: red_list.append((row["名称"], row["代码"], row["所属行业"], score, "、".join(reasons)))
        elif score <= -2: black_list.append((row["名称"], row["代码"], row["所属行业"], score, "、".join(reasons)))
    red_list.sort(key=lambda x: -x[3]); black_list.sort(key=lambda x: x[3])

# ===== 生成报告 =====
lines = []
lines.append(f"===== 每日涨停复盘报告 {today} =====")
lines.append("")

lines.append("【一、当日涨停总览】")
if not df_zt.empty:
    lines.append(f"当日共涨停 {len(df_zt)} 只")
    lines.append("")
    for col, title in [("封板时间","1.按封板时间"),("封板结构","2.按封板结构"),
                        ("驱动原因","3.按驱动原因"),("板块地位","4.按板块地位"),
                        ("市值","5.按市值"),("股性","6.按股性活跃度"),("价格","7.按价格区间")]:
        lines.append(f"[{title}]")
        for k, v in df_zt[col].value_counts().items():
            lines.append(f"  {k}: {v}只")
        lines.append("")
else:
    lines.append("当日暂无涨停数据")
lines.append("")

lines.append("【二、板块强弱分析（结合涨停股+资金流向）】")
lines.append("")
if sector_analysis:
    lines.append(f"共涉及 {len(sector_analysis)} 个行业板块")
    lines.append("")
    if strong_sectors:
        lines.append("■ 强势板块（重点关注）")
        for s in strong_sectors[:8]:
            lines.append(f"  [{s['行业']}] 评分:{s['评分']} 涨停{s['涨停数']}只")
            lines.append(f"    领涨龙头: {s['领涨龙头']}")
            lines.append(f"    平均封板:{s['平均封板']} 一次封板率:{s['一次封板率']} 连板:{s['连板数']}只")
            lines.append(f"    主力净流入: {format_amount(s['主力净流入'])} ({s['资金状态']})")
        lines.append("")
    if weak_sectors:
        lines.append("■ 弱势板块（谨慎参与）")
        for s in weak_sectors[:8]:
            lines.append(f"  [{s['行业']}] 评分:{s['评分']} 涨停{s['涨停数']}只")
            lines.append(f"    领涨龙头: {s['领涨龙头']}")
            lines.append(f"    平均封板:{s['平均封板']} 烂板率:{s['烂板率']} 尾盘封板:{s['尾盘数']}只")
            lines.append(f"    主力净流入: {format_amount(s['主力净流入'])} ({s['资金状态']})")
        lines.append("")
    lines.append("■ 板块强弱总览（按评分排序）")
    for s in sector_analysis:
        mark = "★" if s["等级"] == "强势板块" else ("☆" if s["等级"] == "弱势板块" else "○")
        fund_mark = "↑" if s["主力净流入"] > 0 else ("↓" if s["主力净流入"] < 0 else "-")
        lines.append(f"  {mark} {s['行业']}: {s['涨停数']}只涨停, 评分{s['评分']}, 主力{fund_mark}{format_amount(abs(s['主力净流入']))} ({s['等级']})")
else:
    lines.append("暂无板块数据")
lines.append("")

lines.append("【三、全市场板块资金流向排名】")
lines.append("")
if fund_ranking_in:
    lines.append("■ 主力净流入TOP15（资金抢筹）")
    for i, s in enumerate(fund_ranking_in[:15], 1):
        zt_mark = f" 涨停{s['涨停数']}只" if s['涨停数'] > 0 else ""
        lines.append(f"  {i:2d}. {s['板块']}: 主力净流入{format_amount(s['主力净流入'])}, 占比{s['主力净占比']:.2f}%, 涨跌{s['涨跌幅']:.2f}%{zt_mark}")
    lines.append("")
if fund_ranking_out:
    lines.append("■ 主力净流出TOP15（资金出逃）")
    for i, s in enumerate(fund_ranking_out[:15], 1):
        zt_mark = f" 涨停{s['涨停数']}只" if s['涨停数'] > 0 else ""
        lines.append(f"  {i:2d}. {s['板块']}: 主力净流出{format_amount(abs(s['主力净流入']))}, 占比{s['主力净占比']:.2f}%, 涨跌{s['涨跌幅']:.2f}%{zt_mark}")
    lines.append("")
if not fund_ranking_in and not fund_ranking_out:
    lines.append("板块资金流向数据暂未获取到")
    lines.append("")

lines.append("【四、资金流向与涨停股交叉分析】")
lines.append("")
if cross_analysis["both"]:
    lines.append("■ 涨停+资金双驱动（最优质，持续性强）")
    for s in cross_analysis["both"][:10]:
        lines.append(f"  {s['板块']}: {s['涨停数']}只涨停, 主力净流入{format_amount(s['主力净流入'])}")
    lines.append("")
if cross_analysis["zt_no_fund"]:
    lines.append("■ 有涨停但资金流出（警惕诱多，次日难接力）")
    for s in cross_analysis["zt_no_fund"][:10]:
        lines.append(f"  {s['板块']}: {s['涨停数']}只涨停, 主力净流出{format_amount(abs(s['主力净流入']))}")
    lines.append("")
if cross_analysis["fund_no_zt"]:
    lines.append("■ 资金流入但无涨停（潜在机会，可提前布局）")
    for s in cross_analysis["fund_no_zt"][:10]:
        lines.append(f"  {s['板块']}: 主力净流入{format_amount(s['主力净流入'])}, 暂无涨停")
    lines.append("")

lines.append("【五、打板红黑榜】")
lines.append("")
if red_list:
    lines.append("■ 红榜（优选打板池）")
    for name, code, ind, score, reason in red_list[:15]:
        lines.append(f"  {name}({code}) {ind} 评分:{score} | {reason}")
    lines.append("")
if black_list:
    lines.append("■ 黑榜（避坑警示池）")
    for name, code, ind, score, reason in black_list[:15]:
        lines.append(f"  {name}({code}) {ind} 评分:{score} | {reason}")
    lines.append("")
if not red_list and not black_list:
    lines.append("暂无符合条件的个股")
    lines.append("")

lines.append("【六、昨日涨停今日表现】")
if not df_prev.empty:
    valid = df_prev[df_prev["卖出类型"] != "数据缺失"]
    lines.append(f"昨日涨停 {len(df_prev)} 只，有效分析 {len(valid)} 只")
    lines.append("")
    lines.append("[1.卖出类型分布]")
    for k, v in valid["卖出类型"].value_counts().items():
        lines.append(f"  {k}: {v}只")
    lines.append("")
    good = valid[valid["卖出类型"].str.contains("好卖")]
    if len(good) > 0:
        lines.append("[2.好卖型个股明细]")
        for _, row in good.iterrows():
            lines.append(f"  {row['名称']}({row['代码']}) 开:{row['开盘涨幅']:.2f}% 高:{row['最高涨幅']:.2f}% 收:{row['收盘涨幅']:.2f}% {row['卖出类型']}")
        lines.append("")
    bad = valid[valid["卖出类型"].str.contains("坑人")]
    if len(bad) > 0:
        lines.append("[3.坑人型个股明细]")
        for _, row in bad.iterrows():
            lines.append(f"  {row['名称']}({row['代码']}) 开:{row['开盘涨幅']:.2f}% 高:{row['最高涨幅']:.2f}% 收:{row['收盘涨幅']:.2f}% {row['卖出类型']}")
        lines.append("")
        lines.append("[4.坑人涨停共性深度分析]")
        lines.append(f"  坑人样本: {len(bad)}只")
        lines.append(f"  平均开盘涨幅: {bad['开盘涨幅'].mean():.2f}%")
        lines.append(f"  平均最高涨幅: {bad['最高涨幅'].mean():.2f}%")
        lines.append(f"  平均最低跌幅: {bad['最低涨幅'].mean():.2f}%")
        lines.append(f"  平均收盘跌幅: {bad['收盘涨幅'].mean():.2f}%")
        lines.append(f"  平均振幅: {bad['振幅'].mean():.2f}%")
        lines.append(f"  平均换手率: {bad['换手率'].mean():.2f}%")
        lines.append("")
        bad2 = bad.copy()
        bad2["昨日封板时段"] = bad2["昨日封板时间"].apply(cls_time)
        lines.append("  坑人股昨日封板时段分布:")
        for k, v in bad2["昨日封板时段"].value_counts().items():
            lines.append(f"    {k}: {v}只")
        lines.append("")
        lines.append("  坑人股昨日连板数分布:")
        for k, v in bad2["昨日连板数"].value_counts().items():
            lines.append(f"    {int(k)}连板: {v}只")
    else:
        lines.append("[4.今日无坑人型涨停，市场情绪较好]")
else:
    lines.append("暂无昨日涨停数据")
lines.append("")

lines.append("【七、打板策略总结与避坑指南】")
lines.append("")
lines.append("■ 优选打板特征（好卖概率高）")
lines.append("  1.早盘10点前封板，封板结构扎实（一次封板或大盘回封型）")
lines.append("  2.板块领涨龙头，所属板块涨停家数>=3家，有板块效应")
lines.append("  3.有明确利好驱动（公告利好或板块政策利好）")
lines.append("  4.连板股或近期活跃股，股性好")
lines.append("  5.小盘股（<50亿），资金容易拉升")
lines.append("  6.优先选择强势板块内的个股，避开弱势板块")
lines.append("  7.所属板块主力资金净流入，有资金支撑")
lines.append("")
lines.append("■ 坚决规避特征（坑人概率高）")
lines.append("  1.尾盘偷袭封板（14:45后），非实力资金所为")
lines.append("  2.烂板（炸板>=4次），多空分歧巨大")
lines.append("  3.板块补涨后排，跟风属性强，龙头一倒就崩")
lines.append("  4.无板块支撑的个股独立涨停，缺乏持续性")
lines.append("  5.大盘股（>200亿），需要大量资金才能拉升")
lines.append("  6.弱势板块内的个股，板块整体缺乏持续性")
lines.append("  7.所属板块主力资金净流出，涨停可能是诱多")
lines.append("")
lines.append("■ 好卖涨停的两个核心特征")
lines.append("  1.直接高开：次日高开>=3%，且开盘后继续冲高，全天均价在红盘上方")
lines.append("  2.全天向上：无论开盘高低，日内低点逐步抬升，收盘涨幅>=4%，全程有盈利离场机会")
lines.append("")
lines.append("■ 坑人涨停的典型走势")
lines.append("  1.高开下杀：高开>=2%，10分钟内快速翻绿，全天无有效红盘卖点")
lines.append("  2.水下闷杀：低开后直接下行，全天趴在水面之下，最高都翻不了红")
lines.append("  3.冲高闷杀：短暂冲高>=2%后快速回落，收盘跌>=1%，卖点窗口极短")

report = "\n".join(lines)
print(f"报告生成完成，共{len(report)}字符")

# ===== 邮件推送 =====
mail_user = os.environ.get("MAIL_USER", "")
mail_pass = os.environ.get("MAIL_PASS", "")

if mail_user and mail_pass:
    msg = MIMEText(report, 'plain', 'utf-8')
    msg['From'] = mail_user
    msg['To'] = mail_user
    msg['Subject'] = Header(f"每日涨停复盘 {today}", 'utf-8')
    try:
        smtp = smtplib.SMTP_SSL("smtp.qq.com", 465)
        smtp.login(mail_user, mail_pass)
        smtp.sendmail(mail_user, mail_user, msg.as_string())
        smtp.quit()
        print("邮件推送成功")
    except Exception as e:
        print(f"邮件推送失败: {e}")
else:
    print("未配置邮箱")
    print(report[:2000])
