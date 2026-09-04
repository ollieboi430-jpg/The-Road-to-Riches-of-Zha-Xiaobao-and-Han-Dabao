# -*- coding: utf-8 -*-
"""
GitHub Actions（海外IP）获取 A股行业板块主力资金流向 —— 多源容错版
============================================================================
为什么需要多源：东方财富主站 push2.eastmoney.com 与新浪财经会对海外数据中心 IP
返回 502 / 空响应 / 跳转页。本模块按下列顺序自动降级，任一源成功即返回：

  源1 同花顺 data.10jqka.com.cn（akshare 已内置 hexin-v 反爬，v码本地JS生成、不依赖IP地域，海外可达性好）
  源2 东方财富“备用节点”（绕开被封的 push2 主站，轮询 push2delay / 数字子域，按行业前缀过滤）
  源3 自建 Cloudflare Worker 反代（需配置环境变量 EM_PROXY_URL，100%可控的终极兜底）

统一输出列：板块名称 / 涨跌幅% / 主力净流入(亿) / 主力净占比% / 数据来源
依赖：pip install akshare pandas requests py_mini_racer lxml
"""
import os
import time
import json
import requests
import pandas as pd

# 海外 runner 上若残留代理变量反而可能干扰，按需可放开下面两行
# for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
#     os.environ.pop(k, None)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 东财行业板块代码前缀（用于在备用节点返回的“行业+申万+三级细分”混合结果中筛出标准行业）
_EM_INDUSTRY_PREFIX = ("BK04", "BK05", "BK07", "BK09", "BK10")
# 东财备用节点：主站 push2 被海外封，这些节点走不同入口，逐个尝试
_EM_HOSTS = ["push2delay.eastmoney.com", "push2.eastmoney.com"] + \
            [f"{i}.push2.eastmoney.com" for i in range(1, 21)]


# ============================ 源1：同花顺 ============================
def from_ths():
    """同花顺-行业资金流（即时）。akshare 本地生成 hexin-v，不依赖出口IP位置。"""
    import akshare as ak
    df = ak.stock_fund_flow_industry(symbol="即时")
    if df is None or df.empty:
        raise RuntimeError("同花顺返回空")
    for c in ["行业-涨跌幅", "流入资金", "流出资金", "净额"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    out = pd.DataFrame({
        "板块名称": df["行业"].astype(str).str.strip(),
        "涨跌幅%": df["行业-涨跌幅"].round(2),
        "主力净流入(亿)": df["净额"].round(2),  # 同花顺净额单位本就是“亿元”
        # 同花顺不直接给净占比，用 净额/(流入+流出) 自算（流入+流出=总成交额）
        "主力净占比%": ((df["净额"] / (df["流入资金"] + df["流出资金"])) * 100).round(2),
    })
    out["数据来源"] = "同花顺"
    return out


# ========================== 源2：东财备用节点 ==========================
def _em_request_one(host, timeout=10):
    """从单个东财节点拉取行业资金流（翻页去重 + 行业前缀过滤）。"""
    ut = "b2884a393a59ad64002292a3e90d46a5"
    fields = "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87"
    headers = {"User-Agent": _UA, "Referer": "https://data.eastmoney.com/"}
    pool = {}
    for pn in range(1, 7):
        params = {
            "pn": pn, "pz": 100, "po": 1, "np": 1, "ut": ut,
            "fltt": 2, "invt": 2, "fid0": "f62",
            "fs": "m:90 t:2", "stat": 1, "fields": fields,
        }
        r = requests.get(f"https://{host}/api/qt/clist/get",
                         params=params, headers=headers, timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(f"{host} HTTP {r.status_code}")
        data = r.json().get("data")
        if not data:
            break
        diff = data.get("diff") or []
        if not diff:
            break
        for x in diff:
            pool[x["f12"]] = x
        time.sleep(0.15)
    rows = [x for x in pool.values() if str(x.get("f12", "")).startswith(_EM_INDUSTRY_PREFIX)]
    if not rows:
        raise RuntimeError(f"{host} 未取到行业板块")
    df = pd.DataFrame(rows)
    out = pd.DataFrame({
        "板块名称": df["f14"].astype(str).str.strip(),
        "涨跌幅%": pd.to_numeric(df["f3"], errors="coerce").round(2),
        "主力净流入(亿)": (pd.to_numeric(df["f62"], errors="coerce") / 1e8).round(2),
        "主力净占比%": pd.to_numeric(df["f184"], errors="coerce").round(2),
    })
    out["数据来源"] = f"东财:{host}"
    return out


def from_eastmoney_backup():
    """轮询东财备用节点，任一成功即返回。"""
    last_err = None
    for host in _EM_HOSTS:
        try:
            return _em_request_one(host)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"东财所有备用节点均失败，最后错误：{last_err}")


# ====================== 源3：Cloudflare Worker 反代 ======================
def from_cf_proxy():
    """
    通过自建 Worker 反代东财（部署 cloudflare_worker.js 后，把 Worker URL 配到环境变量）。
    Worker 返回的应是与东财 clist 相同结构的 JSON。
    """
    base = os.environ.get("EM_PROXY_URL", "").strip()
    if not base:
        raise RuntimeError("未配置 EM_PROXY_URL")
    headers = {"User-Agent": _UA}
    pool = {}
    for pn in range(1, 7):
        r = requests.get(base, params={"pn": pn, "pz": 100}, headers=headers, timeout=15)
        data = r.json().get("data")
        if not data:
            break
        for x in (data.get("diff") or []):
            pool[x["f12"]] = x
    rows = [x for x in pool.values() if str(x.get("f12", "")).startswith(_EM_INDUSTRY_PREFIX)]
    if not rows:
        raise RuntimeError("Worker反代未取到行业板块")
    df = pd.DataFrame(rows)
    out = pd.DataFrame({
        "板块名称": df["f14"].astype(str).str.strip(),
        "涨跌幅%": pd.to_numeric(df["f3"], errors="coerce").round(2),
        "主力净流入(亿)": (pd.to_numeric(df["f62"], errors="coerce") / 1e8).round(2),
        "主力净占比%": pd.to_numeric(df["f184"], errors="coerce").round(2),
    })
    out["数据来源"] = "CF反代"
    return out


# ============================ 统一入口（自动降级） ============================
def get_sector_fund_flow(sources=("ths", "em", "cf")):
    """按顺序尝试各数据源，成功即返回，全部失败抛出最后一个异常。"""
    source_fn = {"ths": from_ths, "em": from_eastmoney_backup, "cf": from_cf_proxy}
    errors = []
    for name in sources:
        try:
            df = source_fn[name]()
            df = df.sort_values("主力净流入(亿)", ascending=False).reset_index(drop=True)
            print(f"[资金流] 使用数据源：{df['数据来源'].iloc[0]}，共 {len(df)} 个行业板块")
            return df
        except Exception as e:
            errors.append(f"{name}: {e}")
            print(f"[资金流] 源 {name} 失败：{e}")
    raise RuntimeError("所有资金流数据源均失败 -> " + " | ".join(errors))


if __name__ == "__main__":
    pd.set_option("display.unicode.east_asian_width", True)
    result = get_sector_fund_flow()
    print("\n=== 主力净流入 TOP15 ===")
    print(result.head(15).to_string(index=False))
    print("\n=== 主力净流出 TOP15 ===")
    print(result.tail(15).iloc[::-1].to_string(index=False))
    out_file = "sector_fund_flow.csv"
    result.to_csv(out_file, index=False, encoding="utf-8-sig")
    print(f"\n已保存 {out_file}")
