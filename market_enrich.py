# -*- coding: utf-8 -*-
"""
market_enrich.py —— A股复盘增强模块（零第三方依赖，纯标准库，专为 GitHub Actions 海外环境设计）
来源：参考 github.com/simonlin1212/a-stock-data，已逐个接口实测、剔除海外不通的源。

提供 4 个能力：
  1) tencent_quotes(codes)   批量实时报价（名称/现价/涨跌幅/量比/换手/涨停价/市值/PB）——不封IP，海外可达，用来"给出现的个股标价格"
  2) tag_prices(codes)       直接生成可拼进复盘报告的"带价格"文本行
  3) monitor_pool()          交易所重点监控【真实名单】+生效起止日（独立静态域名，非被封的push2），校准纯算法预判
  4) dedupe_sw_industry()    申万行业去层级重复（剔除"保险Ⅲ"这类三级行，统一保留二级，解决保险Ⅱ/Ⅲ重复）
  5) sina_stock_fund_flow()  个股资金流备胎（东财push2海外被封时降级用；注意：只覆盖个股，板块级无可靠海外源）

设计原则：
  - 不用 mootdx（通达信走TCP 7709，海外服务器连不通）；全部 HTTP。
  - 任何一个源失败都返回空/降级，绝不抛崩整个复盘流程。
"""
import re
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Mobile Safari/537.36"
SH_INDEX = {"000300", "000905", "000016", "000688", "000852", "000010"}


# ---------------------------------------------------------------- 通用
def _http_get(url, headers=None, decode="utf-8", timeout=12, retry=2):
    last = None
    for _ in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode(decode, "ignore")
        except Exception as e:  # 网络层失败统一吞掉，由调用方降级
            last = e
            time.sleep(0.6)
    raise last


def to_digits(code: str) -> str:
    """600519.SH / sh600519 / SH600519 -> 纯6位数字；无法解析抛 ValueError。"""
    m = re.search(r"(\d{6})", str(code))
    if not m:
        raise ValueError(f"无法从 {code!r} 解析出6位代码")
    return m.group(1)


def _tx_prefix(code: str) -> str:
    """纯6位代码 -> 腾讯带前缀键。"""
    c = code.lower()
    if c.startswith(("sh", "sz", "bj")):
        return c
    d = to_digits(code)
    if d.startswith("92"):
        return "bj" + d
    if d in SH_INDEX or d.startswith(("5", "6", "9")):
        return "sh" + d
    if d.startswith(("4", "8")):
        return "bj" + d
    return "sz" + d


# ---------------------------------------------------------------- 1) 腾讯批量报价
def tencent_quotes(codes, chunk=50):
    """
    批量实时行情。输入可混 600519 / 600519.SH / sh600519。
    返回 {纯6位代码: {name, price, change_pct, vol_ratio, turnover_pct,
                     last_close, limit_up, limit_down, pe_ttm, pb, mcap_yi, float_mcap_yi, is_stale}}
    一次 HTTP 最多带 chunk 只，自动分片。腾讯不封IP、全球CDN，海外 Actions 可用。
    """
    digits = [to_digits(c) for c in codes]
    digits = list(dict.fromkeys(digits))           # 去重保序
    out = {}
    for i in range(0, len(digits), chunk):
        seg = digits[i:i + chunk]
        q = ",".join(_tx_prefix(c) for c in seg)
        try:
            text = _http_get("https://qt.gtimg.cn/q=" + q, decode="gbk")
        except Exception:
            continue                                # 这一片失败不影响其他片
        for line in text.strip().split(";"):
            if '="' not in line:
                continue
            v = line.split('"')[1].split("~")
            if len(v) < 53:
                continue
            def f(idx):
                try:
                    return float(v[idx]) if v[idx] not in ("", "-") else 0.0
                except ValueError:
                    return 0.0
            code = to_digits(v[2] if len(v) > 2 else line) if re.search(r"\d{6}", line.split("=")[0]) else None
            code = re.search(r"(s[hz]|bj)(\d{6})", line.split("=")[0])
            code = code.group(2) if code else None
            if not code:
                continue
            price, last_close, amount_wan = f(3), f(4), f(37)
            out[code] = {
                "name": v[1],
                "price": price,
                "last_close": last_close,
                "change_pct": f(32),
                "vol_ratio": f(49),
                "turnover_pct": f(38),
                "high": f(33), "low": f(34),
                "limit_up": f(47), "limit_down": f(48),
                "pe_ttm": f(39), "pb": f(46),
                "float_mcap_yi": f(44), "mcap_yi": f(45),
                "is_stale": (amount_wan == 0 and price == last_close and price > 0),
            }
        time.sleep(0.15)
    return out


# ---------------------------------------------------------------- 2) 报告里"标价格"
def tag_prices(codes, with_extra=False):
    """
    给一批代码生成带价格的文本行（直接贴进复盘报告）。
    with_extra=True 时追加 量比/换手/流通市值。
    返回 list[str]，顺序与输入一致；取不到价格的代码也会列出并标注 [无行情]。
    """
    seq = [to_digits(c) for c in codes]
    q = tencent_quotes(seq)
    lines = []
    for code in seq:
        d = q.get(code)
        if not d:
            lines.append(f"{code} [无行情]")
            continue
        arrow = "+" if d["change_pct"] > 0 else ""
        base = f'{code} {d["name"]} 现价{d["price"]:.2f} {arrow}{d["change_pct"]:.2f}%'
        if with_extra:
            base += f' 量比{d["vol_ratio"]:.2f} 换手{d["turnover_pct"]:.2f}% 流通{d["float_mcap_yi"]:.1f}亿'
        if d["is_stale"]:
            base += " [停牌/无量]"
        lines.append(base)
    return lines


def price_map(codes):
    """返回 {code: '名称 现价 涨跌幅%'} 简短映射，便于和涨停池/监控名单 join。"""
    seq = [to_digits(c) for c in codes]
    q = tencent_quotes(seq)
    m = {}
    for code in seq:
        d = q.get(code)
        if d:
            m[code] = f'{d["name"]} {d["price"]:.2f}元 {d["change_pct"]:+.2f}%'
    return m


# ---------------------------------------------------------------- 3) 重点监控真实名单
_STOCK_PREFIX = ("60", "68", "00", "30", "92", "43", "83", "87")  # 个股号段；其余为ETF/LOF/基金等

def monitor_pool(only_active=True, stock_only=True):
    """
    交易所重点监控名单（东财App静态配置，独立域名 mobappconfig.securities，非被封的push2）。
    返回 [{code,name,market,start,end,days_left}]，按结束日升序。
    stock_only=True（默认）：剔除跨境ETF/LOF/基金，只留个股（该名单会混入纳指ETF等溢价风险警示基金）。
    这是"真实被监控名单"，用来和你纯算法算出的"技术触线"交叉，而不是用算法结果冒充官方名单。
    """
    url = "https://mobappconfig.securities.eastmoney.com/emcfg/stock_monitor.json"
    try:
        rows = json.loads(_http_get(url, headers={"User-Agent": UA,
                                    "Referer": "https://vipmoney.eastmoney.com/"}, timeout=20))
    except Exception:
        return []
    today = datetime.now(CN_TZ).date()
    mkt = {"1": "SH", "0": "SZ", "B": "BJ"}
    out = []
    for x in rows:
        raw_code = str(x.get("STKCODE", ""))
        if stock_only and not raw_code.startswith(_STOCK_PREFIX):
            continue  # 跳过 ETF/LOF/基金（51/50/15/16/56/58/501 等）
        start, end = x.get("VALIDATESTARTDATE", ""), x.get("VALIDATEENDDATE", "")
        if only_active:
            try:
                d0 = datetime.strptime(start, "%Y-%m-%d").date()
                d1 = datetime.strptime(end, "%Y-%m-%d").date()
                if not (d0 <= today <= d1):
                    continue
                days_left = (d1 - today).days
            except ValueError:
                days_left = None
        else:
            days_left = None
        out.append({
            "code": raw_code,
            "name": x.get("STKNAME", ""),
            "market": mkt.get(str(x.get("MARKET", "")).upper(), str(x.get("MARKET", ""))),
            "start": start, "end": end, "days_left": days_left,
        })
    out.sort(key=lambda r: (r["end"] or "9999"))
    return out


# ---------------------------------------------------------------- 4) 申万行业层级去重
def dedupe_sw_industry(rows, name_key="name", keep_level="L2"):
    """
    解决"保险Ⅱ / 保险Ⅲ"数值完全相同、被算两次的问题。
    rows: list[dict]，行业名字段由 name_key 指定（申万命名：一级无后缀、二级带Ⅱ、三级带Ⅲ）。
    keep_level:
      'L2' = 统一保留到二级：剔除所有三级(以Ⅲ结尾)行；一级、二级保留（默认，推荐）。
      'L1' = 只留一级（同时剔除Ⅱ、Ⅲ）。
    返回去重后的新列表。
    """
    lvl3 = re.compile(r"Ⅲ$|III$")                 # 以Ⅲ(三级)结尾
    lvl2 = re.compile(r"Ⅱ$|II$")                  # 以Ⅱ(二级)结尾
    res = []
    for r in rows:
        nm = str(r.get(name_key, "")).strip()
        if keep_level == "L1" and (lvl2.search(nm) or lvl3.search(nm)):
            continue
        if keep_level == "L2" and lvl3.search(nm):
            continue
        res.append(r)
    return res


# ---------------------------------------------------------------- 5) 个股资金流备胎（新浪，日度四档）
def sina_stock_fund_flow(code, days=5):
    """东财push2海外被封时的【个股】资金流备胎（日度，非板块）。失败返回 []。"""
    d = to_digits(code)
    pre = ("bj" if d.startswith(("92", "8")) else "sh" if d.startswith(("6", "9")) else "sz") + d
    url = (f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num={days}&sort=opendate&asc=0&daima={pre}")
    try:
        t = _http_get(url, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"})
        arr = json.loads(t[t.index("["):t.rindex("]") + 1])
        return [{"date": x.get("opendate"), "close": x.get("trade"),
                 "net_amount": float(x.get("netamount", 0) or 0)} for x in arr]
    except Exception:
        return []


# ---------------------------------------------------------------- 演示
if __name__ == "__main__":
    demo_codes = ["603330", "002631", "603318", "002129", "601600",
                  "603327", "002045", "002176", "600613", "601108"]
    print("【一】给出现的个股标价格（报告直接可用）")
    for line in tag_prices(demo_codes, with_extra=True):
        print("  ", line)

    print("\n【二】当前交易所重点监控真实名单（含价格）")
    pool = monitor_pool()
    pm = price_map([s["code"] for s in pool]) if pool else {}
    print(f"  共 {len(pool)} 只")
    for s in pool:
        extra = pm.get(s["code"], "")
        print(f'  {s["code"]} {s["name"]}({s["market"]}) {s["start"]}~{s["end"]} '
              f'剩{s["days_left"]}天 | {extra}')

    print("\n【三】申万行业层级去重（保险Ⅲ被剔除）")
    demo_rows = [{"name": "营销代理", "out": 13.46}, {"name": "保险Ⅱ", "out": 13.27},
                 {"name": "保险Ⅲ", "out": 13.27}, {"name": "证券Ⅱ", "out": 9.1},
                 {"name": "证券Ⅲ", "out": 9.1}]
    for r in dedupe_sw_industry(demo_rows):
        print("  保留:", r)
