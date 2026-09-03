import akshare as ak
import pandas as pd
import os
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime

# ===================== 1. 获取全部数据 =====================
today = datetime.now().strftime("%Y%m%d")
today_str = datetime.now().strftime("%Y-%m-%d")

# 1.1 当日涨停池
try:
    df_zt = ak.stock_zt_pool_em(date=today)
    print(f"当日涨停池获取成功，共{len(df_zt)}只")
except Exception as e:
    print(f"获取当日涨停池失败: {e}")
    df_zt = pd.DataFrame()

# 1.2 昨日涨停池今日表现
try:
    df_prev = ak.stock_zt_pool_previous_em(date=today)
    print(f"昨日涨停池获取成功，共{len(df_prev)}只")
except Exception as e:
    print(f"获取昨日涨停池失败: {e}")
    df_prev = pd.DataFrame()

# 1.3 全市场实时行情（获取开盘价、最高价、最低价）
try:
    df_spot = ak.stock_zh_a_spot_em()
    spot_map = {}
    for _, row in df_spot.iterrows():
        code = str(row["代码"]).zfill(6)
        spot_map[code] = {
            "open": float(row["今开"]),
            "high": float(row["最高"]),
            "low": float(row["最低"]),
            "price": float(row["最新价"])
        }
    print(f"全市场行情获取成功，共{len(spot_map)}只")
except Exception as e:
    print(f"获取全市场行情失败: {e}")
    spot_map = {}

# 1.4 当日公告（用于识别公告驱动型）
try:
    df_notice = ak.stock_notice_report(symbol="全部", date=today)
    good_keywords = ["预增", "预盈", "业绩增长", "扭亏", "中标", "合同", "并购", "重组",
                     "定增", "非公开发行", "股权激励", "摘帽", "分红", "回购", "增持",
                     "战略合作", "投资", "获批", "通过", "授权"]
    notice_codes = set()
    for _, row in df_notice.iterrows():
        title = str(row.get("公告标题", ""))
        code = str(row.get("代码", "")).zfill(6)
        if any(kw in title for kw in good_keywords):
            notice_codes.add(code)
    print(f"当日利好公告获取成功，涉及{len(notice_codes)}只股票")
except Exception as e:
    print(f"获取公告失败: {e}")
    notice_codes = set()

# 1.5 上证指数分时数据（用于判断大盘回落震荡后回封）
try:
    df_index = ak.index_zh_a_hist_min_em(
        symbol="000001", period="1",
        start_date=f"{today_str} 09:30:00",
        end_date=f"{today_str} 15:00:00"
    )
    index_map = {}
    for _, row in df_index.iterrows():
        t = str(row["时间"])
        hm = t.split(" ")[1][:5] if " " in t else t[:5]
        index_map[hm] = float(row["收盘"])
    print(f"大盘分时数据获取成功，共{len(index_map)}个时间点")
except Exception as e:
    print(f"获取大盘分时失败: {e}")
    index_map = {}

# ===================== 2. 工具函数 =====================

def parse_time_to_hm(t):
    if pd.isna(t):
        return None
    text = str(t).strip()
    if ":" in text:
        parts = text.split(":")
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    else:
        digits = "".join(ch for ch in text if ch.isdigit()).zfill(6)
        return f"{digits[:2]}:{digits[2:4]}"

def get_index_change_between(start_hm, end_hm):
    if not index_map or not start_hm or not end_hm:
        return 0, 0
    try:
        sorted_times = sorted(index_map.keys())
        start_idx = None
        end_idx = None
        for i, t in enumerate(sorted_times):
            if t >= start_hm and start_idx is None:
                start_idx = i
            if t <= end_hm:
                end_idx = i
        if start_idx is None or end_idx is None or start_idx >= end_idx:
            return 0, 0
        segment = [index_map[sorted_times[i]] for i in range(start_idx, end_idx + 1)]
        start_val = segment[0]
        min_val = min(segment)
        end_val = segment[-1]
        max_drop = (min_val - start_val) / start_val * 100
        rebound = (end_val - min_val) / min_val * 100
        return max_drop, rebound
    except:
        return 0, 0

# ===================== 3. 当日涨停多维度精细分类 =====================

if not df_zt.empty:
    def classify_time(t):
        hm = parse_time_to_hm(t)
        if hm is None:
            return "未知"
        h, m = int(hm.split(":")[0]), int(hm.split(":")[1])
        if h == 9 and m <= 25:
            return "竞价封板"
        elif h == 9 and m <= 30:
            return "开盘秒板"
        elif h <= 10:
            return "早盘封板(9:30-10:00)"
        elif h <= 11:
            return "上午盘封板"
        elif h <= 14 and m <= 30:
            return "午后盘封板"
        else:
            return "尾盘偷袭封板"

    df_zt["封板时间类型"] = df_zt["首次封板时间"].apply(classify_time)

    def classify_structure(row):
        times = row.get("炸板次数", 0)
        if pd.isna(times):
            return "未知"
        if times == 0:
            return "一次封板型"
        elif times == 1:
            start_hm = parse_time_to_hm(row.get("首次封板时间"))
            end_hm = parse_time_to_hm(row.get("最后封板时间"))
            max_drop, rebound = get_index_change_between(start_hm, end_hm)
            if max_drop <= -0.3 and rebound >= 0.2:
                return "单次开板-大盘回落回封型"
            else:
                return "单次开板-自然换手回封型"
        elif times <= 3:
            return "多次开板型"
        else:
            return "烂板型"

    df_zt["封板结构"] = df_zt.apply(classify_structure, axis=1)

    def classify_market_cap(val):
        if pd.isna(val):
            return "未知"
        cap = val / 1e8
        if cap < 50:
            return "小盘股(<50亿)"
        elif cap < 200:
            return "中盘股(50-200亿)"
        else:
            return "大盘股(>200亿)"

    df_zt["市值类型"] = df_zt["总市值"].apply(classify_market_cap)

    def classify_price(val):
        if pd.isna(val):
            return "未知"
        if val < 10:
            return "低价股(<10元)"
        elif val < 50:
            return "中价股(10-50元)"
        else:
            return "高价股(>50元)"

    df_zt["价格类型"] = df_zt["最新价"].apply(classify_price)

    def classify_activity(row):
        lianban = row.get("连板数", 1)
        zt_stat = str(row.get("涨停统计", ""))
        if lianban >= 3:
            return "高位连板(≥3板)"
        elif lianban == 2:
            return "2连板"
        if "/" in zt_stat:
            try:
                days, boards = zt_stat.split("/")
                if int(boards) >= 3:
                    return "近期活跃(近5天≥3板)"
            except:
                pass
        return "首板(近期不活跃)"

    df_zt["股性活跃度"] = df_zt.apply(classify_activity, axis=1)

    industry_counts = df_zt["所属行业"].value_counts().to_dict()

    def classify_reason(row):
        code = str(row.get("代码", "")).zfill(6)
        industry = row.get("所属行业", "")
        lianban = row.get("连板数", 1)
        ind_count = industry_counts.get(industry, 0)
        if code in notice_codes:
            return "公告驱动型"
        elif ind_count >= 3:
            return "板块驱动型"
        elif lianban >= 2:
            return "连板龙头/资金驱动型"
        else:
            return "个股独立/消息驱动型"

    df_zt["驱动原因"] = df_zt.apply(classify_reason, axis=1)

    def classify_position(row):
        industry = row.get("所属行业", "")
        ind_count = industry_counts.get(industry, 0)
        if ind_count < 3:
            return "独立个股(无板块)"
        industry_df = df_zt[df_zt["所属行业"] == industry].sort_values("首次封板时间")
        rank = list(industry_df["代码"]).index(row["代码"]) + 1
        if rank == 1:
            return "板块领涨龙头"
        elif rank <= 3:
            return "板块跟风"
        else:
            return "板块补涨后排"

    df_zt["板块地位"] = df_zt.apply(classify_position, axis=1)
    print("当日涨停多维度分类完成")

# ===================== 4. 昨日涨停今日表现 =====================

if not df_prev.empty:
    df_prev["今日开盘价"] = df_prev["代码"].apply(
        lambda x: spot_map.get(str(x).zfill(6), {}).get("open", None))
    df_prev["今日最高价"] = df_prev["代码"].apply(
        lambda x: spot_map.get(str(x).zfill(6), {}).get("high", None))
    df_prev["今日最低价"] = df_prev["代码"].apply(
        lambda x: spot_map.get(str(x).zfill(6), {}).get("low", None))

    df_prev["昨收价"] = df_prev["最新价"] / (1 + df_prev["涨跌幅"] / 100)
    df_prev["开盘涨跌幅"] = (df_prev["今日开盘价"] - df_prev["昨收价"]) / df_prev["昨收价"] * 100
    df_prev["最高涨跌幅"] = (df_prev["今日最高价"] - df_prev["昨收价"]) / df_prev["昨收价"] * 100
    df_prev["最低涨跌幅"] = (df_prev["今日最低价"] - df_prev["昨收价"]) / df_prev["昨收价"] * 100
    df_prev["收盘涨跌幅"] = df_prev["涨跌幅"]

    def classify_sell(row):
        open_p = row.get("开盘涨跌幅", 0)
        close_p = row.get("收盘涨跌幅", 0)
        high_p = row.get("最高涨跌幅", 0)
        low_p = row.get("最低涨跌幅", 0)
        if pd.isna(open_p) or pd.isna(high_p):
            return "数据缺失"
        # 好卖型
        if open_p >= 3 and close_p >= 2 and high_p >= open_p:
            return "✅ 好卖-高开溢价型"
        if close_p >= 4 and high_p - close_p <= 1.5 and close_p > open_p:
            return "✅ 好卖-全天向上型"
        # 坑人型
        if open_p >= 2 and close_p < 0 and low_p < -2:
            return "❌ 坑人-高开下杀型"
        if open_p < 0 and close_p < -2 and high_p < 1:
            return "❌ 坑人-水下闷杀型"
        if high_p >= 2 and close_p < -1 and high_p - close_p >= 5:
            return "❌ 坑人-冲高闷杀型"
        return "一般可卖型"

    df_prev["卖出类型"] = df_prev.apply(classify_sell, axis=1)
    print("昨日涨停表现分类完成")

# ===================== 5. 打板红黑榜 =====================

red_list = []
black_list = []

if not df_zt.empty:
    for _, row in df_zt.iterrows():
        score = 0
        reasons = []
        if row["封板时间类型"] in ["竞价封板", "开盘秒板", "早盘封板(9:30-10:00)"]:
            score += 2; reasons.append("早盘封板")
        if row["封板结构"] in ["一次封板型", "单次开板-大盘回落回封型", "单次开板-自然换手回封型"]:
            score += 1; reasons.append("封板结构好")
        if row["板块地位"] == "板块领涨龙头":
            score += 2; reasons.append("板块龙头")
        if row["连板数"] >= 2:
            score += 1; reasons.append(f"{int(row['连板数'])}连板")
        if row["驱动原因"] == "板块驱动型":
            score += 1; reasons.append("有板块效应")
        if row["驱动原因"] == "公告驱动型":
            score += 1; reasons.append("公告利好")
        if row["市值类型"] == "小盘股(<50亿)":
            score += 1; reasons.append("小盘股")
        if row["封板时间类型"] == "尾盘偷袭封板":
            score -= 2; reasons.append("尾盘偷袭")
        if row["封板结构"] == "烂板型":
            score -= 2; reasons.append("烂板")
        if row["板块地位"] == "板块补涨后排":
            score -= 1; reasons.append("后排跟风")
        if row["驱动原因"] == "个股独立/消息驱动型":
            score -= 1; reasons.append("无板块支撑")
        if row["市值类型"] == "大盘股(>200亿)":
            score -= 1; reasons.append("大盘股")
        if score >= 4:
            red_list.append((row["名称"], row["代码"], row["所属行业"], score, "、".join(reasons)))
        elif score <= -2:
            black_list.append((row["名称"], row["代码"], row["所属行业"], score, "、".join(reasons)))
    red_list.sort(key=lambda x: -x[3])
    black_list.sort(key=lambda x: x[3])

# ===================== 6. 生成复盘报告 =====================

report = f"# 📈 每日涨停复盘报告 {today}\n\n"

report += "## 一、当日涨停总览（全维度分类）\n"
if not df_zt.empty:
    report += f"当日共涨停 **{len(df_zt)}** 只\n\n"
    report += "### 1. 按封板时间分布\n" + df_zt["封板时间类型"].value_counts().to_markdown() + "\n\n"
    report += "### 2. 按封板结构分布（含大盘回封判断）\n" + df_zt["封板结构"].value_counts().to_markdown() + "\n\n"
    report += "### 3. 按驱动原因分布（公告/板块/资金/个股）\n" + df_zt["驱动原因"].value_counts().to_markdown() + "\n\n"
    report += "### 4. 按板块地位分布（龙头/跟风/补涨/独立）\n" + df_zt["板块地位"].value_counts().to_markdown() + "\n\n"
    report += "### 5. 按市值分布\n" + df_zt["市值类型"].value_counts().to_markdown() + "\n\n"
    report += "### 6. 按股性活跃度分布\n" + df_zt["股性活跃度"].value_counts().to_markdown() + "\n\n"
    report += "### 7. 按价格区间分布\n" + df_zt["价格类型"].value_counts().to_markdown() + "\n\n"
    report += "### 8. 热门板块TOP8（按涨停家数）\n" + df_zt["所属行业"].value_counts().head(8).to_markdown() + "\n\n"
else:
    report += "当日暂无涨停数据\n\n"

report += "## 二、打板红黑榜\n\n"
if red_list:
    report += "### 🔴 红榜（优选打板池，按评分排序）\n"
    report += "| 名称 | 代码 | 行业 | 评分 | 核心逻辑 |\n|------|------|------|------|----------|\n"
    for name, code, ind, score, reason in red_list[:15]:
        report += f"| {name} | {code} | {ind} | {score} | {reason} |\n"
    report += "\n"
if black_list:
    report += "### ⚫ 黑榜（避坑警示池）\n"
    report += "| 名称 | 代码 | 行业 | 评分 | 风险点 |\n|------|------|------|------|--------|\n"
    for name, code, ind, score, reason in black_list[:15]:
        report += f"| {name} | {code} | {ind} | {score} | {reason} |\n"
    report += "\n"
if not red_list and not black_list:
    report += "暂无符合条件的个股\n\n"

report += "## 三、昨日涨停今日表现（好卖/坑人分析）\n"
if not df_prev.empty:
    valid_df = df_prev[df_prev["卖出类型"] != "数据缺失"]
    report += f"昨日涨停共 **{len(df_prev)}** 只，有效分析 **{len(valid_df)}** 只\n\n"
    report += "### 1. 卖出类型分布\n" + valid_df["卖出类型"].value_counts().to_markdown() + "\n\n"

    good_df = valid_df[valid_df["卖出类型"].str.contains("好卖")]
    if len(good_df) > 0:
        report += "### 2. ✅ 好卖型个股明细（可复制的盈利模式）\n"
        report += "| 名称 | 代码 | 开盘涨幅 | 最高涨幅 | 收盘涨幅 | 昨日连板 | 类型 |\n|------|------|----------|----------|----------|----------|------|\n"
        for _, row in good_df.iterrows():
            report += f"| {row['名称']} | {row['代码']} | {row['开盘涨跌幅']:.2f}% | {row['最高涨跌幅']:.2f}% | {row['收盘涨跌幅']:.2f}% | {int(row['昨日连板数'])} | {row['卖出类型']} |\n"
        report += "\n"

    bad_df = valid_df[valid_df["卖出类型"].str.contains("坑人")]
    if len(bad_df) > 0:
        report += "### 3. ❌ 坑人型个股明细（必须绕开的陷阱）\n"
        report += "| 名称 | 代码 | 开盘涨幅 | 最高涨幅 | 收盘涨幅 | 昨日连板 | 类型 |\n|------|------|----------|----------|----------|----------|------|\n"
        for _, row in bad_df.iterrows():
            report += f"| {row['名称']} | {row['代码']} | {row['开盘涨跌幅']:.2f}% | {row['最高涨跌幅']:.2f}% | {row['收盘涨跌幅']:.2f}% | {int(row['昨日连板数'])} | {row['卖出类型']} |\n"
        report += "\n"

        report += "### 4. ⚠️ 坑人涨停共性深度分析（打板避坑核心）\n\n"
        report += f"**坑人样本数量：{len(bad_df)} 只**\n\n"
        report += "#### 4.1 核心指标统计\n"
        report += f"- 平均开盘涨幅：{bad_df['开盘涨跌幅'].mean():.2f}%\n"
        report += f"- 平均最高涨幅：{bad_df['最高涨跌幅'].mean():.2f}%\n"
        report += f"- 平均最低跌幅：{bad_df['最低涨跌幅'].mean():.2f}%\n"
        report += f"- 平均收盘跌幅：{bad_df['收盘涨跌幅'].mean():.2f}%\n"
        report += f"- 平均振幅：{bad_df['振幅'].mean():.2f}%\n"
        report += f"- 平均换手率：{bad_df['换手率'].mean():.2f}%\n\n"

        bad_df_copy = bad_df.copy()
        bad_df_copy["昨日封板时段"] = bad_df_copy["昨日封板时间"].apply(classify_time)
        report += "#### 4.2 坑人股昨日封板时段分布\n" + bad_df_copy["昨日封板时段"].value_counts().to_markdown() + "\n\n"
        report += "#### 4.3 坑人股昨日连板数分布\n" + bad_df_copy["昨日连板数"].value_counts().to_markdown() + "\n\n"
        report += "#### 4.4 坑人股所属行业分布\n" + bad_df_copy["所属行业"].value_counts().head(8).to_markdown() + "\n\n"
        bad_df_copy["市值类型"] = bad_df_copy["总市值"].apply(classify_market_cap)
        report += "#### 4.5 坑人股市值分布\n" + bad_df_copy["市值类型"].value_counts().to_markdown() + "\n"
    else:
        report += "### 4. 今日无坑人型涨停，市场情绪较好\n\n"
else:
    report += "暂无昨日涨停数据\n\n"

report += "\n## 四、打板策略总结与避坑指南\n\n"
report += "### 优选打板特征（好卖概率高）\n"
report += "1. 早盘10点前封板，封板结构扎实（一次封板或大盘回封型）\n"
report += "2. 板块领涨龙头，所属板块涨停家数≥3家，有板块效应\n"
report += "3. 有明确利好驱动（公告利好或板块政策利好）\n"
report += "4. 连板股或近期活跃股，股性好\n"
report += "5. 小盘股（<50亿），资金容易拉升\n\n"
report += "### 坚决规避特征（坑人概率高）\n"
report += "1. 尾盘偷袭封板（14:45后），非实力资金所为\n"
report += "2. 烂板（炸板≥4次），多空分歧巨大\n"
report += "3. 板块补涨后排，跟风属性强，龙头一倒就崩\n"
report += "4. 无板块支撑的个股独立涨停，缺乏持续性\n"
report += "5. 大盘股（>200亿），需要大量资金才能拉升\n\n"
report += "### 好卖涨停的两个核心特征\n"
report += "1. **直接高开**：次日高开≥3%，且开盘后继续冲高，全天均价在红盘上方\n"
report += "2. **全天向上**：无论开盘高低，日内低点逐步抬升，收盘涨幅≥4%，全程有盈利离场机会\n\n"
report += "### 坑人涨停的典型走势\n"
report += "1. **高开下杀**：高开≥2%，10分钟内快速翻绿，全天无有效红盘卖点\n"
report += "2. **水下闷杀**：低开后直接下行，全天趴在水面之下，最高都翻不了红\n"
report += "3. **冲高闷杀**：短暂冲高≥2%后快速回落，收盘跌≥1%，卖点窗口极短\n"

# ===================== 7. 邮箱推送 =====================
mail_user = os.environ.get("MAIL_USER", "")
mail_pass = os.environ.get("MAIL_PASS", "")

if mail_user and mail_pass:
    msg = MIMEText(report, 'markdown', 'utf-8')
    msg['From'] = mail_user
    msg['To'] = mail_user
    msg['Subject'] = Header(f"每日涨停复盘 {today}", 'utf-8')
    try:
        smtp = smtplib.SMTP_SSL("smtp.qq.com", 465)
        smtp.login(mail_user, mail_pass)
        smtp.sendmail(mail_user, mail_user, msg.as_string())
        smtp.quit()
        print("复盘报告推送成功")
    except Exception as e:
        print(f"推送失败: {e}")
else:
    print("未配置邮箱信息，跳过推送")
