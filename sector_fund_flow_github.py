# -*- coding: utf-8 -*-
"""
GitHub Actions（海外IP）获取 A股行业板块主力资金流向 —— 多源容错版（v2 修复行业匹配）
============================================================================
v2 关键修复（解决“大部分板块主力净流入=0/无数据”）：
  1) 涨停池 ak.stock_zt_pool_em 的“所属行业”是【东财细分行业】（如 养殖业/林业Ⅱ/
     IT服务Ⅱ，板块代码 BK12xx），旧版东财源只保留 BK04/05/07/09/10 的74个一级行业，
     把这些 BK12 细分行业全部过滤掉了，导致一半行业查不到资金流。本版【保留全部板块】。
  2) 两边名称存在 罗马数字后缀(Ⅱ/Ⅲ)、名称扩展(工程咨询→工程咨询服务Ⅱ)、截断
     (汽车零部→汽车零部件) 差异，新增 FundLookup 智能匹配：精确→去后缀→前缀，三级兜底。
  3) 默认源顺序改为 东财(em) 优先：涨停池是东财数据，资金流也用东财才能保证行业名同源；
     同花顺(ths)名称体系不同、仅作东财全挂时的兜底。

数据源降级顺序：东财备用节点(em) → 同花顺(ths) → 自建 Cloudflare Worker(cf)
统一输出列：板块名称 / 板块代码 / 涨跌幅% / 主力净流入(亿) / 主力净占比% /
            超大单净流入(亿) / 大单净流入(亿) / 数据来源
依赖：pip install akshare pandas requests py_mini_racer lxml
"""
import os
import re
import time
import requests
import pandas as pd

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 东财备用节点：主站 push2 若被海外封，这些节点走不同入口，逐个尝试
_EM_HOSTS = ["push2delay.eastmoney.com", "push2.eastmoney.com"] + \
            [f"{i}.push2.eastmoney.com" for i in range(1, 21)]
# 二级/一级行业前缀（前缀匹配时优先于三级细分 BK13-BK16）
_MAIN_PREFIX = ("BK04", "BK05", "BK07", "BK09", "BK10", "BK12")
_EM_FIELDS = "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87"


# ============================ 名称归一化与智能匹配 ============================
def _norm_name(s):
    """去空格 + 去末尾罗马数字(Ⅰ/Ⅱ/Ⅲ/Ⅳ/I/II/III)，用于跨口径行业名对齐。"""
    s = str(s).strip().replace(" ", "")
    return re.sub(r"(Ⅰ|Ⅱ|Ⅲ|Ⅳ|II|III|IV|[I])+$", "", s)


class FundLookup:
    """
    行业资金流智能查找器：用涨停池的“所属行业”名查资金流，自动容忍
    后缀Ⅱ/Ⅲ、名称扩展、截断等差异。用法：
        lookup = FundLookup(df_fund)
        rec = lookup.get("林业Ⅱ")          # 命中 -> dict；查不到 -> None
        rec["main_inflow"]                  # 主力净流入，单位：元（与旧代码口径一致）
    """

    def __init__(self, df):
        self.exact, self.norm_idx, self.records = {}, {}, []
        if df is None:
            return
        for _, row in df.iterrows():
            name = str(row["板块名称"]).strip()
            if not name:
                continue
            rec = {
                "板块名称": name,
                "板块代码": str(row.get("板块代码", "")),
                "main_inflow": float(row.get("主力净流入(亿)", 0) or 0) * 1e8,  # 元
                "main_ratio": float(row.get("主力净占比%", 0) or 0),
                "super_inflow": float(row.get("超大单净流入(亿)", 0) or 0) * 1e8,
                "big_inflow": float(row.get("大单净流入(亿)", 0) or 0) * 1e8,
                "change_pct": float(row.get("涨跌幅%", 0) or 0),
                "主力净流入(亿)": float(row.get("主力净流入(亿)", 0) or 0),
                "主力净占比%": float(row.get("主力净占比%", 0) or 0),
                "涨跌幅%": float(row.get("涨跌幅%", 0) or 0),
            }
            self.exact[name] = rec
            self.norm_idx.setdefault(_norm_name(name), []).append((name, rec))
            self.records.append((name, rec))

    def get(self, name, default=None):
        name = str(name).strip()
        # 1) 精确匹配
        if name in self.exact:
            return self.exact[name]
        nn = _norm_name(name)
        # 2) 去罗马数字后缀匹配（动物保健 -> 动物保健Ⅱ）
        if nn in self.norm_idx:
            return self._pick_main(self.norm_idx[nn])[1]
        # 3) 前缀匹配：优先“板块名是查询名的延伸”（汽车零部->汽车零部件）
        cands = []
        for n, rec in self.records:
            a = _norm_name(n)
            if a.startswith(nn) and len(a) >= len(nn):
                cands.append((0, n, rec))          # 板块名更长、是查询的延伸，最可靠
            elif nn.startswith(a) and len(a) >= 2:
                cands.append((1, n, rec))          # 板块名更短，次选
        if cands:
            cands.sort(key=lambda x: (x[0],
                                      0 if str(x[2].get("板块代码", "")).startswith(_MAIN_PREFIX) else 1,
                                      len(x[1])))
            return cands[0][2]
        return default

    @staticmethod
    def _pick_main(pairs):
        pairs = sorted(pairs, key=lambda x: (0 if str(x[1].get("板块代码", "")).startswith(_MAIN_PREFIX) else 1, len(x[0])))
        return pairs[0]

    def __len__(self):
        return len(self.exact)

    def __bool__(self):
        return len(self.exact) > 0

    def __iter__(self):
        return iter(self.exact)

    def items(self):
        return self.exact.items()


# ============================ 源1：东财备用节点（默认首选，与涨停池同源） ============================
def _rows_to_em_df(pool, source_tag):
    rows = list(pool.values())                 # v2：保留全部板块，不再按前缀硬过滤
    if not rows:
        raise RuntimeError(f"{source_tag} 未取到任何板块")
    df = pd.DataFrame(rows)
    out = pd.DataFrame({
        "板块名称": df["f14"].astype(str).str.strip(),
        "板块代码": df["f12"].astype(str),
        "涨跌幅%": pd.to_numeric(df["f3"], errors="coerce").round(2),
        "主力净流入(亿)": (pd.to_numeric(df["f62"], errors="coerce") / 1e8).round(2),
        "主力净占比%": pd.to_numeric(df["f184"], errors="coerce").round(2),
        "超大单净流入(亿)": (pd.to_numeric(df.get("f66"), errors="coerce") / 1e8).round(2),
        "大单净流入(亿)": (pd.to_numeric(df.get("f72"), errors="coerce") / 1e8).round(2),
    })
    out["数据来源"] = source_tag
    return out


def _em_request_one(host, timeout=10):
    """从单个东财节点翻页拉取【全部】行业板块资金流。"""
    ut = "b2884a393a59ad64002292a3e90d46a5"
    headers = {"User-Agent": _UA, "Referer": "https://data.eastmoney.com/"}
    pool = {}
    for pn in range(1, 7):
        params = {"pn": pn, "pz": 100, "po": 1, "np": 1, "ut": ut,
                  "fltt": 2, "invt": 2, "fid0": "f62",
                  "fs": "m:90 t:2", "stat": 1, "fields": _EM_FIELDS}
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
        time.sleep(0.12)
    return _rows_to_em_df(pool, f"东财:{host}")


def from_eastmoney_backup():
    """轮询东财备用节点，任一成功即返回。"""
    last_err = None
    for host in _EM_HOSTS:
        try:
            return _em_request_one(host)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"东财所有节点均失败，最后错误：{last_err}")


# ============================ 源2：同花顺（东财全挂时兜底） ============================
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
        "板块代码": "",
        "涨跌幅%": df["行业-涨跌幅"].round(2),
        "主力净流入(亿)": df["净额"].round(2),
        "主力净占比%": ((df["净额"] / (df["流入资金"] + df["流出资金"])) * 100).round(2),
        "超大单净流入(亿)": 0.0,
        "大单净流入(亿)": 0.0,
    })
    out["数据来源"] = "同花顺"
    return out


# ====================== 源3：Cloudflare Worker 反代（终极兜底） ======================
def from_cf_proxy():
    """通过自建 Worker 反代东财（Worker 返回与东财 clist 同结构 JSON，全量板块）。"""
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
        time.sleep(0.1)
    return _rows_to_em_df(pool, "CF反代")


# ============================ 统一入口（自动降级） ============================
def get_sector_fund_flow(sources=("em", "ths", "cf")):
    """按顺序尝试各数据源，成功即返回，全部失败抛出最后一个异常。默认东财优先（与涨停池同源）。"""
    source_fn = {"em": from_eastmoney_backup, "ths": from_ths, "cf": from_cf_proxy}
    errors = []
    for name in sources:
        try:
            df = source_fn[name]()
            df = df.sort_values("主力净流入(亿)", ascending=False).reset_index(drop=True)
            print(f"[资金流] 使用数据源：{df['数据来源'].iloc[0]}，共 {len(df)} 个板块")
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
    result.to_csv("sector_fund_flow.csv", index=False, encoding="utf-8-sig")
    print("\n已保存 sector_fund_flow.csv")

    # 自检：用几个“名称对不上”的行业验证智能匹配
    print("\n=== 智能匹配自检 ===")
    lk = FundLookup(result)
    for q in ["林业Ⅱ", "养殖业", "动物保健", "工程咨询", "汽车零部", "调味发酵", "炼化及贸"]:
        r = lk.get(q)
        print(f"  {q:6} -> {r['板块名称'] if r else '未匹配':10} "
              f"{r['主力净流入(亿)'] if r else 0:>8}亿")
