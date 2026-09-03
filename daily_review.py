import akshare as ak
import pandas as pd
import os
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime, timedelta

# ===================== 功能核心：涨停多维度分类+次日表现分析 =====================

# 1. 获取数据
today = datetime.now().strftime("%Y%m%d")

# 当日涨停池
try:
    df_zt = ak.stock_zt_pool_em(date=today)
except:
    df_zt = pd.DataFrame()

# 昨日涨停池今日表现
try:
    df_prev = ak.stock_zt_pool_previous_em(date=today)
except:
    df_prev = pd.DataFrame()

# 2. 当日涨停多维度精细分类
def classify_time(t):
    if pd.isna(t):
        return "未知"
    text = str(t).strip()
    if ":" in text:
        h, m = int(text.split(":")[0]), int(text.split(":")[1])
    else:
        digits = "".join(ch for ch in text if ch.isdigit()).zfill(6)
        h, m = int(digits[:2]), int(digits[2:4])
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

def classify_structure(times):
    if pd.isna(times):
        return "未知"
    if times == 0:
        return "一次封板型"
    elif times == 1:
        return "单次开板回封型"
    elif times <= 3:
        return "多次开板型"
    else:
        return "烂板型"

def classify_reason(row):
    reason = str(row.get("涨停原因", ""))
    if "公告" in reason or "业绩" in reason or "并购" in reason or "定增" in reason:
        return "公告驱动型"
    elif "板块" in reason or "概念" in reason or "行业" in reason:
        return "板块驱动型"
    elif "资金" in reason or "龙头" in reason:
        return "资金驱动型"
    else:
        return "消息/其他驱动"

if not df_zt.empty:
    df_zt["封板时间类型"] = df_zt["首次封板时间"].apply(classify_time)
    df_zt["封板结构"] = df_zt["炸板次数"].apply(classify_structure)
    df_zt["驱动原因"] = df_zt.apply(classify_reason, axis=1)

# 3. 昨日涨停今日表现：好卖/坑人分类
def classify_sell(row):
    open_p = row.get("开盘涨跌幅", 0)
    close_p = row.get("收盘涨跌幅", 0)
    high_p = row.get("最高涨跌幅", 0)
    
    # 好卖型：高开溢价 / 全天向上
    if open_p >= 3 and close_p >= 2:
        return "✅ 好卖-高开溢价型"
    if close_p >= 4 and high_p - close_p <= 1:
        return "✅ 好卖-全天向上型"
    
    # 坑人型：高开下杀 / 水下闷杀 / 冲高闷杀
    if open_p >= 2 and close_p < 0:
        return "❌ 坑人-高开下杀型"
    if open_p < 0 and close_p < -2:
        return "❌ 坑人-水下闷杀型"
    if high_p >= 2 and close_p < -1:
        return "❌ 坑人-冲高闷杀型"
    
    return "一般可卖型"

if not df_prev.empty:
    df_prev["卖出类型"] = df_prev.apply(classify_sell, axis=1)

# 4. 生成复盘报告
report = f"# 📈 每日涨停复盘报告 {today}\n\n"

# 当日涨停总览
report += "## 一、当日涨停总览\n"
if not df_zt.empty:
    report += f"当日共涨停 **{len(df_zt)}** 只\n\n"
    
    report += "### 按封板时间分布\n"
    report += df_zt["封板时间类型"].value_counts().to_markdown() + "\n\n"
    
    report += "### 按封板结构分布\n"
    report += df_zt["封板结构"].value_counts().to_markdown() + "\n\n"
    
    report += "### 按驱动原因分布\n"
    report += df_zt["驱动原因"].value_counts().to_markdown() + "\n\n"
else:
    report += "当日暂无涨停数据\n\n"

# 昨日涨停表现
report += "## 二、昨日涨停今日表现\n"
if not df_prev.empty:
    report += df_prev["卖出类型"].value_counts().to_markdown() + "\n\n"
    
    # 坑人型涨停共性总结
    bad_df = df_prev[df_prev["卖出类型"].str.contains("坑人")]
    if len(bad_df) > 0:
        report += "### ⚠️ 坑人涨停共性总结\n"
        report += f"- 样本数量：{len(bad_df)}只\n"
        report += "- 典型特征：尾盘封板、多次炸板、无明确板块支撑的个股占比高\n"
        report += f"- 平均收盘跌幅：{bad_df['收盘涨跌幅'].mean():.2f}%\n"
else:
    report += "暂无昨日涨停数据\n\n"

# 打板策略提示
report += "\n## 三、打板避坑指南\n"
report += "1. 优选：早盘10点前封板、一次封板、板块龙头、有明确利好驱动\n"
report += "2. 规避：尾盘偷袭封板、多次烂板、无板块跟风、利好兑现型公告\n"
report += "3. 好卖核心：次日高开溢价 OR 全天重心向上，有充足离场窗口\n"

# ===================== 邮箱推送模块 =====================

mail_user = os.environ["MAIL_USER"]
mail_pass = os.environ["MAIL_PASS"]

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
