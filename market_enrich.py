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


# ------------------------------------------- A股代码识别 + 对最终报告一键全局标价格
# 只认真实 A 股号段，避免把日期(20260907)、金额、时间(153000)误判成股票代码；
# 兼容多种写法：603330 / 603330.SH / 百大集团(603330) / （603330）全角半角括号。
# sfx=可选交易所后缀，rp=代码后紧邻的一个右括号（价格要插到括号外，不能把括号吃掉）。
_A_SHA_RE = re.compile(
    r"(?<![0-9A-Za-z])"
    r"(60[0135]\d{3}|68[89]\d{3}|00[0-3]\d{3}|30[01]\d{3}"
    r"|920\d{3}|43\d{4}|83\d{4}|87\d{4})"
    r"(?P<sfx>\.(?:SH|SZ|BJ|sh|sz|bj))?"
    r"(?P<rp>[)）])?"
)


def extract_stock_codes(text):
    """从任意复盘文本里按出现顺序提取、去重 A 股 6 位代码（兼容括号/后缀写法）。"""
    seen = []
    for m in _A_SHA_RE.finditer(str(text)):
        c = m.group(1)
        if c not in seen:
            seen.append(c)
    return seen


def market_suffix(code):
    """6位代码 -> 交易所后缀：60/68/9/5 开头 .SH，920/43/83/87 .BJ，其余 .SZ。"""
    d = to_digits(code)
    if d.startswith(("920", "43", "83", "87", "4", "8")):
        return ".BJ"
    if d.startswith(("5", "6", "9")):
        return ".SH"
    return ".SZ"


def enrich_report_text(report, inline="first", with_extra=False, append_summary=True,
                       title="【本报告出现个股 · 实时价格一览】"):
    """
    对"已经生成好的整段复盘文本"一键标注价格——【规则：只要出现股票代码就标，不挑榜单】。
    兼容 "百大集团(600865)" 这种括号写法：价格补在右括号外，且不重复前面已有的中文名，
    例如  百大集团(600865) 一般零售…  →  百大集团(600865) 现价8.50元 +1.20% 一般零售…
    report:        原始报告字符串。
    inline:        'first'=每只仅在首次出现处内联补价格(默认，不刷屏)；
                   'all'=每一处代码都补；'none'=不改正文只出末尾汇总。
    with_extra:    是否额外带 量比、换手率（默认关，报告更清爽）。
    append_summary:末尾追加一张"全部出现个股价格汇总（含全称）"，保证一只不漏。
    返回标注后的新字符串；取不到行情的代码标 [无行情]，绝不因个别失败而丢内容。
    """
    text = str(report)
    codes = extract_stock_codes(text)
    if not codes:
        return text
    q = tencent_quotes(codes)

    def inline_tag(code):
        """正文内联：只补价格不重复名称（名称通常就在代码前面）。"""
        d = q.get(code)
        if not d:
            return "[无行情]"
        s = f'现价{d["price"]:.2f}元 {d["change_pct"]:+.2f}%'
        if with_extra:
            s += f' 量比{d["vol_ratio"]:.2f} 换手{d["turnover_pct"]:.2f}%'
        if d.get("is_stale"):
            s += " [停牌/无量]"
        return s

    def summary_line(code):
        """末尾汇总：带全称。"""
        d = q.get(code)
        if not d:
            return f"{code} [无行情]"
        s = f'{code} {d["name"]} {d["price"]:.2f}元 {d["change_pct"]:+.2f}%'
        if with_extra:
            s += f' 量比{d["vol_ratio"]:.2f} 换手{d["turnover_pct"]:.2f}%'
        if d.get("is_stale"):
            s += " [停牌/无量]"
        return s

    if inline in ("first", "all"):
        done = set()

        def _sub(m):
            c, sfx, rp = m.group(1), m.group("sfx") or "", m.group("rp") or ""
            if inline == "first":
                if c in done:
                    return m.group(0)          # 重复出现：原样返回（保留后缀/括号，不能吃掉）
                done.add(c)
            return f"{c}{sfx}{rp} {inline_tag(c)}".rstrip()

        text = _A_SHA_RE.sub(_sub, text)

    if append_summary:
        block = ["", title, f"共出现 {len(codes)} 只（按首次出现顺序，价格为抓取时点）："]
        block += ["  " + summary_line(c) for c in codes]
        text = text.rstrip() + "\n" + "\n".join(block)
    return text


# 榜单标题行识别：只认"真正的栏目名"——行首是 ■/#/【/◆/★/▶ 等符号，或"一、二、"中文序号。
# 【刻意不认"1. 2."阿拉伯数字序号】：报告里这种行几乎都是"1. 电子:主力净流入306.95亿"
# 的数据条目，若当标题，切分会被切碎、改名还会误伤正文数据。确需阿拉伯数字分节可传 title_re 自定义。
_SECTION_TITLE_RE = re.compile(
    r"^\s*(?:[#■◆★●◇▶▍■【\[]|[一二三四五六七八九十百]+[、.．)])"
)


def enrich_named_sections(report, section_keys, inline="first", with_extra=True,
                          append_summary=True, title_re=None,
                          summary_title="【目标榜单出现个股 · 实时价格一览】"):
    """
    只给【指定榜单区块】里出现的股票标价格，其它区块一字不动。
    适合"只要红黑榜/好卖型这类榜单带价格"的需求。

    report:       整段复盘报告文本。
    section_keys: 目标榜单标题关键词列表（标题行包含任一关键词即命中，模糊匹配），
                  例如 ["红黑榜", "好卖型"]。标题具体叫"■ 红黑榜TOP10"也能命中。
    inline:       'first'=区块内每只首次出现处内联标价(默认)；'all'=每处都标；'none'=只出汇总。
    title_re:     你的榜单标题若不按常规格式开头，可传自定义正则；默认已覆盖常见符号/序号。
    返回处理后的报告；目标榜单之外的股票代码保持原样，末尾只汇总目标榜单里的个股。
    """
    text = str(report)
    tre = re.compile(title_re) if isinstance(title_re, str) else (title_re or _SECTION_TITLE_RE)
    keys = [str(k) for k in section_keys]

    # 1) 按标题行把报告切成 [(标题, 正文行列表), ...]
    secs, cur_title, cur_body = [], "", []
    for ln in text.split("\n"):
        if tre.match(ln):
            secs.append((cur_title, cur_body))
            cur_title, cur_body = ln, []
        else:
            cur_body.append(ln)
    secs.append((cur_title, cur_body))
    if secs and secs[0][0] == "" and not secs[0][1]:
        secs = secs[1:]

    def is_target(title):
        return any(k in title for k in keys)

    # 2) 汇总目标区块里的代码，一次批量取价
    target_text = "\n".join("\n".join(body) for t, body in secs if is_target(t))
    codes = extract_stock_codes(target_text)
    q = tencent_quotes(codes) if codes else {}

    def brief(code):
        d = q.get(code)
        if not d:
            return f"{code}[无行情]"
        s = f'{code}{d["name"]} {d["price"]:.2f}元 {d["change_pct"]:+.2f}%'
        if with_extra:
            s += f' 量比{d["vol_ratio"]:.2f} 换手{d["turnover_pct"]:.2f}%'
        if d.get("is_stale"):
            s += " [停牌/无量]"
        return s

    # 3) 逐区块重建：只有命中的区块内才替换代码
    out = []
    for title, body in secs:
        if title:
            out.append(title)
        block = "\n".join(body)
        if is_target(title) and inline in ("first", "all"):
            done = set()

            def _sub(m):
                c = m.group(1)
                if inline == "first":
                    if c in done:
                        return c
                    done.add(c)
                return brief(c)

            block = _A_SHA_RE.sub(_sub, block)
        out.extend(block.split("\n"))
    result = "\n".join(out)

    if append_summary and codes:
        block = ["", summary_title, f"目标榜单共出现 {len(codes)} 只（价格为抓取时点）："]
        block += ["  " + brief(c) for c in codes]
        result = result.rstrip() + "\n" + "\n".join(block)
    return result


# ------------------------------------------------- 栏目名同花顺风格化
# 借鉴同花顺「涨停聚焦 / 连板天梯 / 最强风口 / 市场情绪 / 超短风向标」等真实栏目命名。
# 只改"榜单标题行"里的词，正文不动；长词优先替换，避免"涨停"误伤"昨日涨停"。
THS_SECTION_RENAME = {
    # —— 多空/强弱类 ——
    "红黑榜": "多空风向标", "红榜": "强势聚焦", "黑榜": "走弱预警",
    "好卖型": "形态优选", "好买型": "上攻形态",
    # —— 涨停情绪类（同花顺:涨停聚焦/连板天梯/最强风口）——
    "昨日涨停": "昨涨停·晋级观察", "涨停池": "涨停聚焦", "涨停股": "涨停聚焦",
    "涨停板": "涨停聚焦", "连板股": "连板天梯", "连板": "连板天梯",
    "跌停池": "跌停观察", "跌停股": "跌停观察", "炸板": "涨停打开",
    "最强题材": "最强风口", "题材股": "风口题材",
    # —— 资金类 ——
    "主力净流入": "主力增仓", "主力净流出": "主力减仓",
    "资金流入": "资金主攻", "资金流出": "资金撤离", "资金出逃": "资金撤离",
    # —— 量比五信号 ——
    "早盘介入": "开盘量能突破", "盘中加仓": "企稳放量加仓",
    "尾盘抢筹": "尾盘资金异动", "次日高开": "量能延续预期", "放量滞涨": "量价背离预警",
    # —— 监管/风险 ——
    "监管提醒": "风险警示监控", "重点监控": "重点监控",
}


def rename_section_titles(report, extra=None, only_title_line=True, title_re=None):
    """
    把报告里"老套的榜单/分类名"替换成同花顺风格专业名（只动标题行，正文与股票代码不受影响）。
    extra: 你自己的 {旧名: 新名}，会覆盖/补充内置 THS_SECTION_RENAME。
    建议放在流程【最后一步】：先用 enrich_named_sections 按旧榜单名标好价格，最后再统一改名。
    """
    mapping = dict(THS_SECTION_RENAME)
    if extra:
        mapping.update(extra)
    # 长词优先 + 正则一次性扫描：每个原文片段只替换一次，
    # 避免"连板股→连板天梯"后新名里的"连板"被二次替换成"连板天梯天梯"。
    keys = sorted(mapping, key=len, reverse=True)
    pat = re.compile("|".join(re.escape(k) for k in keys))
    tre = re.compile(title_re) if isinstance(title_re, str) else (title_re or _SECTION_TITLE_RE)
    out = []
    for ln in str(report).split("\n"):
        is_title = (not only_title_line) or bool(tre.match(ln))
        if is_title:
            ln = pat.sub(lambda m: mapping[m.group(0)], ln)
        out.append(ln)
    return "\n".join(out)


# ------------------------------------------- 优选股票：只提取纯代码清单
# "判断为优"的区块标题关键词（标题命中即认为整段是优选）；可被入参覆盖。刻意用较精确的词，
# 不用"强势/多头/龙头"这种宽词，避免把"多空风向标(含走弱)""板块龙头(正文标签)"误判进来。
DEFAULT_PICK_KEYS = ["优选", "打板", "强势聚焦", "红榜", "好卖型", "形态优选", "上攻形态",
                     "主攻", "入选", "精选", "金股", "晋级"]
_SCORE_RE = re.compile(r"评分[:：]\s*(\d+(?:\.\d+)?)")


def _split_sections(text, title_re=None):
    """按标题行把文本切成 [(标题, [正文行...]), ...]，供"只处理指定区块"复用。"""
    tre = title_re or _SECTION_TITLE_RE
    secs, title, body = [], "", []
    for ln in str(text).split("\n"):
        if tre.match(ln):
            secs.append((title, body))
            title, body = ln, []
        else:
            body.append(ln)
    secs.append((title, body))
    if secs and secs[0][0] == "" and not secs[0][1]:
        secs = secs[1:]
    return secs


def extract_picked_codes(report, section_keys=None, min_score=None, suffix=False):
    """
    从复盘报告里挑出"判断为优"的股票，【只返回股票代码】（去重、按首次出现保序）。
    两条判定取并集：
      1) 区块标题命中 section_keys（默认 优选/打板/强势聚焦/红榜/好卖型…）→ 区块内代码全收；
      2) 给定 min_score 时，任何一行写着"评分:N"且 N≥min_score → 该行代码收（跨区块兜底）。
    suffix=True 返回带交易所后缀（600865.SH / 002564.SZ），默认返回纯6位代码。
    """
    keys = DEFAULT_PICK_KEYS if section_keys is None else list(section_keys)
    picked, order = set(), []

    def add(c):
        if c not in picked:
            picked.add(c)
            order.append(c)

    for title, body in _split_sections(report):
        if any(k in title for k in keys):
            for c in extract_stock_codes("\n".join(body)):
                add(c)
    if min_score is not None:
        for ln in str(report).split("\n"):
            ms = _SCORE_RE.search(ln)
            if ms and float(ms.group(1)) >= float(min_score):
                for c in extract_stock_codes(ln):
                    add(c)
    return [c + market_suffix(c) if suffix else c for c in order]


def picked_codes_block(report, section_keys=None, min_score=None, suffix=False,
                       title="【优选股票代码清单·可直接复制】"):
    """生成可贴到报告末尾的"纯代码清单"文本块（每行一个，方便批量导入软件）。"""
    codes = extract_picked_codes(report, section_keys, min_score, suffix)
    if not codes:
        return ""
    return "\n".join(["", title, f"共 {len(codes)} 只，每行一个：", *codes])


def finalize_report(report, pick_keys=None, min_score=None, with_extra=False,
                    picked_suffix=False, rename=False):
    """
    一键收尾，一次做完三件事（对应固定运行规则）：
      ① 全文只要出现股票代码就标现价/涨跌幅（不挑榜单、兼容"名称(代码)"括号写法）+末尾价格汇总；
      ② 把"判断为优"的股票单独整理成【纯代码清单】（默认纯6位；picked_suffix=True 带 .SH/.SZ）；
      ③ rename=True 时顺带把栏目名改成同花顺风格。
    返回最终可直接保存/推送的报告文本；任何行情取不到都只标[无行情]，不丢内容。
    """
    original = str(report)
    text = enrich_report_text(original, with_extra=with_extra)
    if rename:
        text = rename_section_titles(text)
    picked = extract_picked_codes(original, pick_keys, min_score, picked_suffix)
    if picked:
        text = text.rstrip() + "\n" + "\n".join(
            ["", "【优选股票代码清单·可直接复制】", f"共 {len(picked)} 只，每行一个：", *picked])
    return text


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
_LEVEL_SUFFIX = re.compile(r"(Ⅲ|Ⅱ|Ⅰ|III|II|I)$")   # 申万层级后缀：一级裸名/带Ⅰ，二级Ⅱ，三级Ⅲ


def _split_industry_level(name: str):
    """'证券Ⅱ' -> ('证券', 2)；'银行' -> ('银行', 1)。兼容拉丁 II/III。"""
    nm = str(name).strip()
    m = _LEVEL_SUFFIX.search(nm)
    if not m:
        return nm, 1
    lvl = {"Ⅲ": 3, "III": 3, "Ⅱ": 2, "II": 2, "Ⅰ": 1, "I": 1}.get(m.group(1), 1)
    return nm[:m.start()].strip(), lvl


def dedupe_sw_industry(rows, name_key="name", keep_level="L2"):
    """
    解决同一行业被层级重复计算（实测会同时出现：
      "证券Ⅲ"="证券Ⅱ"、"银行"="银行Ⅱ"、"保险Ⅱ"="保险Ⅲ"，数值完全相同）。
    数据源把申万一级(裸名)、二级(Ⅱ)、三级(Ⅲ)混在一张表时，按"去掉层级后缀的行业名"
    分组，每个行业只保留一条：
      keep_level='L2'(默认推荐)：优先二级Ⅱ；该行业没有二级时回退保留一级裸名；剔除三级Ⅲ。
      keep_level='L1'：优先一级裸名，没有才回退二级。
    只有一级裸名的行业（非银金融/计算机/有色金属等）原样保留。输出保持首次出现顺序。
    """
    pref = {"L2": {2: 0, 1: 1, 3: 2}, "L1": {1: 0, 2: 1, 3: 2}}[keep_level]
    groups, order = {}, []
    for idx, r in enumerate(rows):
        base, lvl = _split_industry_level(r.get(name_key, ""))
        if base not in groups:
            groups[base] = []
            order.append(base)
        groups[base].append((pref.get(lvl, 9), idx, r))
    out = []
    for base in order:
        groups[base].sort(key=lambda t: (t[0], t[1]))   # 层级优先，同级保持原序
        out.append(groups[base][0][2])
    return out


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


# ==================================================== 6) 板块资金流（题材概念 / 行业，走延时节点）
# 关键实测结论（2026-09）：东财主域 push2.eastmoney.com / 17.push2 在海外 Actions 502、断连，
# 但【延时节点 push2delay.eastmoney.com】可用（延时约15分钟，盘后复盘完全无影响）。
# 因此多节点轮询，延时节点优先；任一节点通即用，全失败返回 []，绝不拖崩复盘。
EM_FUND_HOSTS = [
    "https://push2delay.eastmoney.com",   # 延时节点，实测最稳，盘后复盘首选
    "https://push2.eastmoney.com",        # 主域（海外可能502，兜底）
    "https://17.push2.eastmoney.com",     # 编号节点（兜底）
]
# 东财板块类型：t:1 地域 / t:2 行业(申万一/二/三级混在一个池) / t:3 概念题材
_EM_BOARD_KIND = {"region": "1", "industry": "2", "concept": "3"}


# 概念池(t:3)里混着大量"风格/宽基/互联互通/业绩标签"，不是产业题材，短线看了没用，必须剔除。
# 下面是东财概念池里实测存在的全部非题材名（快照），再加兜底正则拦住以后新增的同类。
CONCEPT_STYLE_TAGS = {
    "融资融券", "深股通", "沪股通", "港股通", "MSCI中国", "富时罗素", "QFII重仓", "社保重仓",
    "证金持股", "机构重仓", "基金重仓", "券商金股", "券商概念", "东方财富热股",
    "百元股", "高价股", "低价股", "大盘股", "中盘股", "小盘股", "微盘股", "权重股", "价值股",
    "成长股", "周期股", "反转股", "趋势股", "超跌股", "微利股", "次新股", "题材股", "高成长股",
    "破净股", "破发股", "破增发价股", "红利股", "红利破净股", "长期破净", "ST股", "B股",
    "AH股", "AB股", "科创板做市股", "大盘价值", "大盘成长", "中盘价值", "中盘成长", "小盘价值",
    "小盘成长", "科技风格", "消费风格", "医药医疗风格", "先进制造风格", "金融地产风格",
    "HS300_", "上证50_", "上证180_", "上证380", "中证500", "中证1000", "中证800", "中证100",
    "沪深300", "深证100R", "深成500", "深证成指", "深证成指R", "国证2000", "创业板综", "创业成份",
    "高市净率", "低市净率", "股权分散", "股权集中", "股权激励", "股权转让",
    "参股保险", "参股券商", "参股银行", "参股期货", "参股新三板",
    "举牌", "近期摘帽", "近期新高", "贬值受益",
    "昨日涨停", "昨日涨停_含一字", "昨日连板", "昨日连板_含一字", "昨日首板", "昨日炸板",
    "昨日触板", "昨日高振幅", "昨日高换手", "昨日打二板以上表现",
    "标准普尔", "标普500", "道琼斯", "纳斯达克", "行业龙头", "龙头股",
}
# 兜底：财报季业绩标签(2026中报预增…)、"昨日…"、"…股"风格词，即便东财改名也能拦住。
_CONCEPT_STYLE_RE = re.compile(
    r"(^20\d{2}.*报(预增|预减|预盈|预亏)$)|(^昨日)"
    r"|((大盘|中盘|小盘)(价值|成长)$)|((价值|周期|趋势|反转|超跌|微利|权重|次新|破净|破发|红利|高成长|微盘|高价|低价)股$)"
    # 宽基/指数类标签兜底（真产业题材名里不会出现这些词）
    r"|(深成|深证|上证|中证|沪深|国证|创业板综|创业成份|HS300|MSCI|富时|罗素|标普|标准普尔|道琼|纳斯达克)"
    r"|(行业龙头|龙头股$)"
)


def _is_style_concept(name: str) -> bool:
    n = str(name).strip()
    return n in CONCEPT_STYLE_TAGS or bool(_CONCEPT_STYLE_RE.search(n))


# ====== 题材名对齐同花顺（2026-09 快照：用 akshare.stock_board_concept_name_ths 取到的
# 同花顺官方 375 个概念板块名固化于此，离线校验、运行时不再访问同花顺，海外 Actions 无依赖。
# 同花顺概念会随时间增减，需更新时本地跑一次该 akshare 接口替换本集合即可）======
THS_CONCEPT_NAMES = frozenset( {
    '2026一季报预增', '2026中报预增', '3D打印', '5G', '6G概念', 'AI PC',
    'AI应用', 'AI手机', 'AI智能体', 'AI眼镜', 'AI视频', 'AI语料',
    'BC电池', 'DeepSeek概念', 'EDR概念', 'ERP概念', 'ETC', 'F5G概念',
    'IP经济(谷子经济)', 'MCU芯片', 'MLCC概念', 'MiniLED', 'NFT概念', 'OLED',
    'PCB概念', 'PEEK材料', 'PET铜箔', 'PM2.5', 'POE胶膜', 'PPP概念',
    'ST板块', 'TOPCON电池', 'WiFi 6', '一体化压铸', '一带一路', '三胎概念',
    '上海国企改革', '上海自贸区', '专精特新', '丙烯酸', '东数西算(算力)', '两轮车',
    '中俄贸易概念', '中国AI 50', '中字头股票', '中船系', '中芯国际概念', '中韩自贸区',
    '举牌', '乡村振兴', '乳业', '云办公', '云游戏', '云计算',
    '互联网保险', '互联网金融', '京津冀一体化', '人工智能', '人形机器人', '人脸识别',
    '人造肉', '代糖概念', '仿制药一致性评价', '传感器', '低空经济', '体育产业',
    '供销社', '俄乌冲突概念', '信创', '信托概念', '储能', '元宇宙',
    '充电桩', '先进封装', '光伏概念', '光刻机', '光刻胶', '光热发电',
    '光纤概念', '免税店', '共享单车', '共同富裕示范区', '共封装光学(CPO)', '兵装重组概念',
    '养老概念', '养鸡', '军工', '军工信息化', '军民融合', '农业种植',
    '农机', '农村电商', '冰雪产业', '冷链物流', '净水概念', '减肥药',
    '减速器', '创投', '创新药', '动力电池回收', '动物疫苗', '化债概念(AMC概念)',
    '化肥', '区块链', '医疗器械概念', '医美概念', '医药电商', '华为手机',
    '华为数字能源', '华为昇腾', '华为概念', '华为欧拉', '华为汽车', '华为海思概念股',
    '华为盘古', '华为鲲鹏', '卫星导航', '参股保险', '参股券商', '参股银行',
    '可控核聚变', '可燃冰', '可降解塑料', '合成生物', '同花顺中特估100', '同花顺出海50',
    '同花顺新质50', '同花顺果指数', '同花顺漂亮100', '商业航天', '啤酒概念', '固废处理',
    '固态电池', '国产操作系统', '国产航母', '国企改革', '国家大基金持股', '国资云',
    '土地流转', '土壤修复', '在线教育', '地下管网', '垃圾分类', '培育钻石',
    '基因测序', '多模态AI', '大豆', '大飞机', '天津自贸区', '天然气',
    '太赫兹', '央企国企改革', '存储芯片', '宁德时代概念', '安防', '宠物经济',
    '家庭医生', '家用电器', '富士康概念', '小米概念', '小米汽车', '小红书概念',
    '小金属概念', '工业互联网', '工业大麻', '工业母机', '幽门螺杆菌概念', '广东自贸区',
    '建筑节能', '快手概念', '成飞概念', '房屋检测', '手机游戏', '托育服务',
    '抖音概念(字节概念)', '抽水蓄能', '拼多多概念', '换电概念', '摘帽', '数字乡村',
    '数字孪生', '数字水印', '数字经济', '数字货币', '数据中心(AIDC)', '数据安全',
    '数据确权', '数据要素', '文化传媒概念', '新型城镇化', '新型工业化', '新型烟草(电子烟)',
    '新疆振兴', '新股与次新股', '新能源汽车', '旅游概念', '无人机', '无人零售',
    '无人驾驶', '无线充电', '无线耳机', '时空大数据', '星闪概念', '智慧城市',
    '智慧政务', '智慧灯杆', '智能医疗', '智能家居', '智能座舱', '智能物流',
    '智能电网', '智能穿戴', '智能音箱', '智谱AI', '有机硅概念', '期货概念',
    '机器人概念', '机器视觉', '染料', '柔性屏(折叠屏)', '柔性直流输电', '核污染防治',
    '核电', '横琴新区', '比亚迪概念', '毛发医疗', '毫米波雷达', '民爆概念',
    '民营医院', '氟化工概念', '氢能源', '水利', '水泥概念', '污水处理',
    '汽车拆解概念', '汽车热管理', '汽车电子', '汽车芯片', '沪股通', '注册制次新股',
    '流感', '海南自贸区', '海峡两岸', '海工装备', '消毒剂', '消费电子概念',
    '液冷服务器', '深圳国企改革', '深股通', '烟草', '煤化工概念', '煤炭概念',
    '燃料电池', '牙科医疗', '物业管理', '物联网', '特斯拉概念', '特色小镇',
    '特钢概念', '特高压', '独角兽概念', '猪肉', '猴痘概念', '玉米',
    '环氧丙烷', '玻璃基板', '生态农业', '生物疫苗', '生物质能发电', '电力物联网',
    '电子竞技', '电子纸', '电子身份证', '白酒概念', '百度概念', '盐湖提锂',
    '眼科医疗', '知识产权保护', '短剧游戏', '石墨烯', '石墨电极', '硅能源',
    '碳中和', '碳交易', '碳纤维', '磷化工', '福建自贸区', '禽流感',
    '科创次新股', '租售同权', '移动支付', '稀土永磁', '空气能热泵', '空间计算',
    '第三代半导体', '算力租赁', '粤港澳大湾区', '粮食概念', '细胞免疫治疗', '统一大市场',
    '维生素', '绿色电力', '网红经济', '网约车', '网络安全', '网络游戏',
    '职业教育', '肝炎概念', '股权转让(并购重组)', '脑机接口', '腾讯概念', '自由贸易港',
    '航空发动机', '航运概念', '芬太尼', '芯片概念', '英伟达概念', '苹果概念',
    '草甘膦', '虚拟数字人', '虚拟现实', '虚拟电厂', '蚂蚁集团概念', '融资融券',
    '血氧仪', '装配式建筑', '西部大开发', '证金持股', '语音技术', '财税数字化',
    '赛马概念', '超导概念', '超级品牌', '超级电容', '超超临界发电', '足球概念',
    '跨境电商', '车联网(车路协同)', '转基因', '辅助生殖', '重组蛋白', '量子科技',
    '金属回收', '金属钴', '金属铅', '金属铜', '金属锌', '金属镍',
    '钒电池', '钙钛矿电池', '钛白粉概念', '钠离子电池', '铜缆高速连接', '锂电池概念',
    '长三角一体化', '长安汽车概念', '阿尔茨海默概念', '阿里巴巴概念', '雄安新区', '雅下水电概念',
    '露营经济', '青蒿素', '页岩气', '预制菜', '风电', '飞行汽车(eVTOL)',
    '食品安全', '高压快充', '高压氧舱', '高端装备', '高股息精选', '高铁',
    '鸿蒙概念', '黄金概念', '黑龙江自贸区',
})

# 东财名 -> 同花顺概念板块【确切存在】的标准名（30 条，目标均已校验在上面 375 集合内）。
THS_CONCEPT_ALIAS = {
    'CPO概念': '共封装光学(CPO)',
    'PCB': 'PCB概念',
    '国产芯片': '芯片概念',
    'AI芯片': '芯片概念',
    '数据中心': '数据中心(AIDC)',
    '电子烟': '新型烟草(电子烟)',
    '华为海思': '华为海思概念股',
    '阿里概念': '阿里巴巴概念',
    '腾讯云': '腾讯概念',
    '养老金': '养老概念',
    '央国企改革': '国企改革',
    '知识产权': '知识产权保护',
    '并购重组概念': '股权转让(并购重组)',
    '海南自贸': '海南自贸区',
    '上海自贸': '上海自贸区',
    '京津冀': '京津冀一体化',
    '降解塑料': '可降解塑料',
    '汽车一体化压铸': '一体化压铸',
    '智能驾驶': '无人驾驶',
    '新能源车': '新能源汽车',
    '机器人执行器': '机器人概念',
    '虚拟机器人': '机器人概念',
    '毫米波概念': '毫米波雷达',
    '旅游酒店': '旅游概念',
    '核能核电': '核电',
    '谷子经济': 'IP经济(谷子经济)',
    '中字头': '中字头股票',
    'PPP模式': 'PPP概念',
    '东数西算': '东数西算(算力)',
    '5G概念': '5G',
}

# 同花顺概念板块列表里没有、但在同花顺【搜索框】输入能搜到个股/资讯的习惯口语名
# （如东财'光通信模块'，你平时和资金气泡图里都叫'光模块'，搜索框可搜，但它不是独立概念板块）。
THS_SEARCH_FRIENDLY = {
    '光通信模块': '光模块', '半导体概念': '半导体', '算力概念': '算力',
    'AI概念': '人工智能', 'AIGC概念': 'AIGC', 'LED概念': 'LED',
}


def _to_ths_name(raw: str) -> str:
    '''东财题材名 -> 同花顺口径名，五级择优，宁可不改也不硬编一个同花顺里不存在的名：
    1)已核实别名(目标∈同花顺375概念) 2)本就同名 3)去'概念'后同花顺确有其名
    4)搜索框习惯名 5)都不满足则保留东财原名(搜索框仍能搜到相关个股，不张冠李戴)。'''
    n = str(raw).strip()
    if n in THS_CONCEPT_ALIAS:
        return THS_CONCEPT_ALIAS[n]
    if n in THS_CONCEPT_NAMES:
        return n
    if n.endswith('概念') and n[:-2] in THS_CONCEPT_NAMES:
        return n[:-2]
    if n in THS_SEARCH_FRIENDLY:
        return THS_SEARCH_FRIENDLY[n]
    return n


def concept_ths_alignment(topn=300):
    '''[自检用] 返回(精确对齐数, 总数, 未对齐东财名list)：精确对齐=输出名∈同花顺375概念集合。'''
    rows, _ = em_sector_fund_flow('concept', topn=topn, drop_style=True, ths_name=True)
    miss = [r['name_raw'] for r in rows if r['name'] not in THS_CONCEPT_NAMES]
    return len(rows) - len(miss), len(rows), miss


def em_sector_fund_flow(kind="concept", topn=15, direction="in", drop_style=True,
                        ths_name=True, hosts=None, timeout=12):
    """
    东财板块资金流排名（延时节点优先，海外 Actions 可用；盘后复盘无延时影响）。
    kind:      'concept'=题材概念(t:3，对应同花顺搜得到的题材，推荐) /
               'industry'=申万行业(t:2，一/二/三级混排，建议再用 collapse_industry_level 折叠) /
               'region'=地域(t:1)
    topn:      返回前 N 条。
    direction: 'in'=主力净流入榜(默认) / 'out'=主力净流出榜。
    drop_style: 仅 kind='concept' 生效，剔除融资融券/基金重仓/大盘股/业绩预增这类"非题材标签"。
    ths_name:   仅 kind='concept' 生效，把名字归一为同花顺搜索名（光通信模块→光模块、CPO概念→CPO）。
    返回 [{code,name_raw,name,net_yi,ratio_pct,change_pct}]，按主力净额排序；全源失败返回 []。
    注意：延时节点数据有约15分钟延迟，仅适合盘后/复盘，不用于盘中实时下单。
    """
    t = _EM_BOARD_KIND.get(kind, "3")
    po = 0 if direction == "out" else 1          # 东财：po=1 降序(净流入大→小)，po=0 升序(净流出最负在前)
    fields = "f12,f14,f3,f62,f184"
    rows, used_host, total = [], None, 10 ** 9
    for host in (hosts or EM_FUND_HOSTS):
        pn = 1
        while pn <= 8:                            # 单页上限100，分页拉到够数或拉完（最多8页≈800）
            url_tail = (f"/api/qt/clist/get?pn={pn}&pz=100&po={po}&np=1&fltt=2&invt=2&fid=f62"
                        f"&fs=m:90+t:{t}&fields={fields}")
            try:
                txt = _http_get(host + url_tail,
                                headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"},
                                timeout=timeout, retry=1)
                j = json.loads(txt)
                data = j.get("data") or {}
                diff = data.get("diff") or []
                total = data.get("total", total)
                if not diff:
                    break
                used_host = host
                for x in diff:
                    raw_name = str(x.get("f14", "")).strip()
                    if kind == "concept" and drop_style and _is_style_concept(raw_name):
                        continue

                    def num(k, x=x):
                        v = x.get(k)
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            return 0.0
                    rows.append({
                        "code": x.get("f12", ""),
                        "name_raw": raw_name,
                        "name": (_to_ths_name(raw_name) if (kind == "concept" and ths_name) else raw_name),
                        "net_yi": num("f62") / 1e8,        # 元 -> 亿
                        "ratio_pct": num("f184"),          # 主力净占比 %
                        "change_pct": num("f3"),           # 板块涨跌幅 %
                    })
                if len(rows) >= topn or pn * 100 >= total:
                    break
                pn += 1
                time.sleep(0.1)
            except Exception:
                break
        if rows:
            break                                 # 该节点取到数据就不再换源
    # 流出榜按净额升序（最负在前）；流入榜按净额降序，保险再排一次。
    rows.sort(key=lambda r: r["net_yi"], reverse=(direction != "out"))
    return rows[:topn], used_host


# ==================================== 6.5) 同花顺资金流【主源·原生分类名，反反爬】
# 反反爬原理：同花顺 data.10jqka.com.cn 资金接口要 hexin-v cookie（由官方 ths.js 的 v() 函数
# 算出的 token）。用 py_mini_racer 执行 ths.js 得到 v，带 Cookie:v=... 即可正常访问（实测200）。
# ths.js 直接读已安装 akshare 自带的那份（requirements 已含 akshare+py_mini_racer，无需新增依赖）。
# 只取第1页50条（复盘只取TOP15，零翻页、规避同花顺快速翻页限流）：流入取涨幅降序页、流出取
# 涨幅升序页，两页合并去重后本地按净额排序。缺依赖/被限流时返回[]，自动回退东财，绝不拖崩复盘。
_THS_FUNDS_URL = "http://data.10jqka.com.cn/funds/{kind}/field/tradezdf/order/{order}/page/1/ajax/1/"
_THS_KIND = {"concept": "gnzjl", "industry": "hyzjl"}
_THS_V = {"v": None}
# 同花顺资金页里混入的风格/宽基指数/持股类"假题材"黑名单（不是可交易题材）
THS_STYLE_TAGS = {
    "同花顺中特估100", "同花顺漂亮100", "高股息精选", "证金持股", "汇金持股", "社保险资重仓",
    "社保重仓", "基金重仓", "QFII重仓", "机构重仓", "融资融券", "沪股通", "深股通", "港股通",
    "MSCI中国", "MSCI概念", "富时罗素", "标普道琼斯A股", "创业板综", "上证50", "上证180",
    "沪深300", "中证100", "中证500", "中证1000", "行业龙头", "百元股", "低价股", "高市盈率",
    "低市盈率", "破净股", "次新股", "预盈预增", "业绩预增", "高派息",
}
_THS_STYLE_RE = re.compile(r"(持股|重仓|股通|精选|指数|综指|MSCI|罗素|标普|道琼斯)")


def _ths_hexin_v(force=False):
    """生成并缓存同花顺 hexin-v；缺依赖/失败返回 None（上层自动回退东财）。"""
    if not force and _THS_V["v"]:
        return _THS_V["v"]
    try:
        from py_mini_racer import MiniRacer
        from akshare.datasets import get_ths_js
        js = MiniRacer()
        js.eval(open(get_ths_js("ths.js"), encoding="utf-8").read())
        _THS_V["v"] = js.call("v")
        return _THS_V["v"]
    except Exception as e:
        print(f"[同花顺] hexin-v 生成失败，将回退东财: {e}")
        return None


def _ths_funds_rows(kind, order):
    """取同花顺资金流一页50条（order=desc涨幅降序/asc涨幅升序），正则解析，零pandas依赖。"""
    seg = _THS_KIND.get(kind, "gnzjl")
    v = _ths_hexin_v()
    if not v:
        return []
    url = _THS_FUNDS_URL.format(kind=seg, order=order)
    headers = {"User-Agent": UA, "Cookie": f"v={v}",
               "Referer": f"http://data.10jqka.com.cn/funds/{seg}/"}
    txt = None
    for attempt in range(3):                     # 被限流就重试，必要时强制重新生成 v
        try:
            t = _http_get(url, headers=headers, decode="gbk", timeout=12, retry=0)
            if "<tr" in t:
                txt = t
                break
        except Exception:
            pass
        time.sleep(1.0 + attempt)
        _ths_hexin_v(force=True)
    if not txt:
        return []
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", txt, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 8:
            continue

        def cell(i):
            return re.sub(r"<[^>]+>", "", tds[i]).strip()

        def num(i):
            try:
                return float(cell(i).replace(",", "").replace("%", ""))
            except (ValueError, IndexError):
                return 0.0

        name = cell(1)
        if not name:
            continue
        if kind == "concept" and (name in THS_STYLE_TAGS or _THS_STYLE_RE.search(name)):
            continue
        out.append({
            "name": name, "change_pct": num(3),
            "in_yi": num(4), "out_yi": num(5), "net_yi": num(6),
            "count": int(num(7)),
            "leader": (re.sub(r"<[^>]+>", "", tds[8]).strip() if len(tds) > 8 else ""),
        })
    return out


def ths_sector_fund_flow(kind="concept", topn=15, direction="in"):
    """
    同花顺板块资金流【主源，名字100%同花顺原生】。kind='concept'题材(gnzjl)/'industry'行业(hyzjl)。
    涨幅降序+升序两页合并去重，本地按净额排序取TOP。返回 (rows, '同花顺')；全失败返回 ([], None)。
    口径：同花顺资金页是【全口径资金净额=流入-流出】，与东财"主力净额(超大单+大单)"口径不同，
    数值不可直接划等号，只用于资金方向/强弱排名。
    """
    try:
        desc = _ths_funds_rows(kind, "desc")
        time.sleep(0.3)
        asc = _ths_funds_rows(kind, "asc")
        pool, seen = [], set()
        for r in desc + asc:
            if r["name"] not in seen:
                seen.add(r["name"])
                pool.append(r)
        if not pool:
            return [], None
        pool.sort(key=lambda x: x["net_yi"], reverse=(direction != "out"))
        return pool[:topn], "同花顺"
    except Exception as e:
        print(f"[同花顺] {kind} 资金流失败: {e}")
        return [], None


def ths_hot_concepts(topn=15, direction="in"):
    """同花顺题材概念资金榜文本行（原生概念名+公司家数+领涨股），失败返回[]。"""
    rows, _ = ths_sector_fund_flow("concept", topn, direction)
    sign = "净流入" if direction != "out" else "净流出"
    out = []
    for i, r in enumerate(rows):
        lead = f"，领涨{r['leader']}" if r.get("leader") else ""
        out.append(f'{i+1}. {r["name"]}：资金{sign}{abs(r["net_yi"]):.2f}亿'
                   f'（{r["count"]}家，板块{r["change_pct"]:+.2f}%{lead}）')
    return out


def ths_industry_lines(topn=15, direction="in"):
    """同花顺行业资金榜文本行（原生行业名），失败返回[]。"""
    rows, _ = ths_sector_fund_flow("industry", topn, direction)
    sign = "净流入" if direction != "out" else "净流出"
    return [f'{i+1}. {r["name"]}：资金{sign}{abs(r["net_yi"]):.2f}亿'
            f'（{r["count"]}家，{r["change_pct"]:+.2f}%）' for i, r in enumerate(rows)]


def _em_concept_lines(topn, direction, **kw):
    rows, _ = em_sector_fund_flow("concept", topn=topn, direction=direction, **kw)
    sign = "净流入" if direction != "out" else "净流出"
    return [f'{i+1}. {r["name"]}：主力{sign}{abs(r["net_yi"]):.2f}亿'
            f'（占比{r["ratio_pct"]:.2f}%，板块{r["change_pct"]:+.2f}%）'
            for i, r in enumerate(rows)]


def hot_concepts(topn=15, direction="in", prefer="ths", **kw):
    """题材概念资金榜文本行：默认【同花顺主源·原生名】，失败自动回退东财；prefer='em'强制东财。"""
    if prefer != "em":
        lines = ths_hot_concepts(topn, direction)
        if lines:
            return lines
    return _em_concept_lines(topn, direction, **kw)


def hot_concepts_ex(topn=15, direction="in", **kw):
    """同 hot_concepts，但返回 (文本行list, 数据源标注)，供报告写明口径。"""
    lines = ths_hot_concepts(topn, direction)
    if lines:
        return lines, "同花顺(资金净额口径)"
    return _em_concept_lines(topn, direction, **kw), "东方财富(主力净额口径·兜底)"


# -------------------------------------------- 7) 行业层级折叠（吞掉"电子/通信"这种一级大筐）
# 申万2021 共31个一级行业（行业池 t:2 里它们以"裸名"出现，是最笼统的大筐）。
SW_L1 = {
    "农林牧渔", "煤炭", "石油石化", "基础化工", "钢铁", "有色金属", "电子", "电力设备", "汽车",
    "机械设备", "国防军工", "建筑材料", "建筑装饰", "交通运输", "仓储物流", "房地产", "银行",
    "非银金融", "家用电器", "食品饮料", "纺织服饰", "轻工制造", "医药生物", "公用事业", "环保",
    "美容护理", "商贸零售", "社会服务", "计算机", "传媒", "通信", "综合",
}
# 一级 -> 其下二级+三级名（裸名，匹配时统一去掉Ⅱ/Ⅲ后缀）。电子/通信做全到三级（截图重灾区），
# 其余覆盖到二级与常见三级；作用是判断"这张大筐在榜里有没有更细的子孙同时在榜"。
SW_DESCENDANTS = {
    "电子": ["半导体", "分立器件", "半导体材料", "数字芯片设计", "模拟芯片设计", "集成电路制造",
            "集成电路封测", "半导体设备", "集成电路", "元件", "印制电路板", "被动元件",
            "光学光电子", "面板", "显示器件", "LED", "光学元件", "消费电子", "品牌消费电子",
            "消费电子零部件及组装", "电子化学品", "其他电子"],
    "通信": ["通信服务", "电信运营商", "通信工程及服务", "通信应用增值服务", "通信设备",
            "通信网络设备及器件", "通信线缆及配套", "通信终端及配件", "其他通信设备"],
    "计算机": ["计算机设备", "安防设备", "其他计算机设备", "IT服务", "软件开发",
              "垂直应用软件", "横向通用软件", "云计算", "互联网服务"],
    "传媒": ["游戏", "社交", "数字媒体", "视频媒体", "音频媒体", "图片媒体", "文字媒体",
            "门户网站", "其他数字媒体", "广告营销", "营销代理", "广告媒体", "影视院线",
            "影视动漫制作", "院线", "出版", "教育出版", "大众出版", "其他出版", "电视广播", "互联网传媒"],
    "机械设备": ["通用设备", "专用设备", "其他专用设备", "自动化设备", "机器人", "工控设备",
              "激光设备", "其他自动化设备", "工程机械", "工程机械整机", "工程机械器件",
              "轨交设备", "铁路设备", "仪器仪表", "电工仪器仪表", "金属制品", "运输设备",
              "摩托车及其他", "能源及重型设备", "楼宇设备", "机床工具", "印刷包装机械",
              "农用机械", "纺织服装设备", "火电设备"],
    "电力设备": ["电池", "锂电池", "电池化学品", "锂电专用设备", "燃料电池", "蓄电池及其他电池",
              "光伏设备", "硅料硅片", "光伏电池组件", "逆变器", "光伏辅材", "光伏加工设备",
              "光伏主材", "综合电力设备商", "风电设备", "风电零部件", "风电整机", "电网设备",
              "输变电设备", "配电设备", "电网自动化设备", "其他电源设备", "电机"],
    "有色金属": ["工业金属", "铜", "铝", "铅锌", "小金属", "其他小金属", "钨", "钼", "锡",
              "稀土", "贵金属", "黄金", "白银", "能源金属", "锂", "钴", "镍",
              "金属新材料", "其他金属新材料", "磁性材料"],
    "非银金融": ["证券", "保险", "多元金融", "金融控股", "期货", "信托", "租赁", "资产管理", "金融信息服务"],
    "银行": ["国有大型银行", "股份制银行", "城商行", "农商行", "其他银行"],
    "医药生物": ["化学制药", "化学制剂", "原料药", "中药", "生物制品", "血液制品", "疫苗",
              "其他生物制品", "医药商业", "医药流通", "线下药店", "互联网药店", "医疗器械",
              "医疗设备", "医疗耗材", "体外诊断", "医疗服务", "诊断服务", "医疗研发外包", "医院", "其他医疗服务"],
    "汽车": ["乘用车", "电动乘用车", "综合乘用车", "商用车", "商用载货车", "商用载客车",
            "汽车零部件", "车身附件及饰件", "底盘与发动机系统", "轮胎轮毂", "汽车电子电气系统",
            "其他汽车零部件", "汽车服务", "汽车综合服务", "汽车经销商", "摩托车", "摩托车及其他"],
    "家用电器": ["白色家电", "空调", "冰洗", "黑色家电", "彩电", "其他黑色家电", "小家电",
              "厨房小家电", "清洁小家电", "个护小家电", "厨卫电器", "厨房电器", "卫浴电器",
              "照明设备", "家电零部件", "其他家电"],
    "食品饮料": ["白酒", "非白酒", "啤酒", "其他酒类", "饮料乳品", "软饮料", "乳品", "食品加工",
              "预加工食品", "保健品", "其他食品", "休闲食品", "零食", "烘焙食品", "熟食",
              "调味发酵品", "肉制品"],
    "基础化工": ["化学原料", "氯碱", "纯碱", "无机盐", "氮肥", "磷肥及磷化工", "化学纤维",
              "涤纶", "粘胶纤维", "氨纶", "锦纶", "其他化学纤维", "化学制品", "有机硅", "氟化工",
              "聚氨酯", "钛白粉", "炭黑", "农化制品", "农药", "纺织化学制品", "胶黏剂及胶带",
              "涂料油墨", "涂料", "民爆制品", "塑料", "合成树脂", "改性塑料", "其他塑料制品",
              "塑料包装", "橡胶", "橡胶助剂", "其他橡胶制品", "复合肥", "非金属材料"],
    "国防军工": ["航天装备", "航空装备", "航海装备", "军工电子", "地面兵装"],
    "交通运输": ["铁路公路", "高速公路", "铁路运输", "航空机场", "航空运输", "机场", "航运港口",
              "航运", "港口", "物流", "原材料供应链服务", "端到端供应链服务", "快递", "跨境物流",
              "公路货运", "公交"],
    "煤炭": ["煤炭开采", "焦煤", "动力煤", "煤化工"],
    "石油石化": ["油气开采", "油服工程", "油田服务", "油气及炼化工程", "炼油化工", "炼化及贸易",
              "油品石化贸易", "其他石化"],
    "钢铁": ["普钢", "特钢", "长材", "板材", "钢铁管材", "冶钢原料", "冶钢辅料"],
    "公用事业": ["电力", "火力发电", "水力发电", "光伏发电", "风力发电", "核力发电", "其他能源发电",
              "电能综合服务", "热力服务", "水务及水治理", "燃气"],
    "环保": ["环境治理", "固废治理", "大气治理", "综合环境治理", "环保设备"],
    "房地产": ["房地产开发", "住宅开发", "产业地产", "商业地产", "房地产服务", "物业管理",
            "房产租赁经纪", "房地产综合服务", "商业物业经营"],
    "建筑材料": ["水泥", "水泥制造", "水泥制品", "玻璃玻纤", "玻璃制造", "玻纤制造", "装修建材",
              "防水材料", "管材", "耐火材料", "磨具磨料"],
    "建筑装饰": ["房屋建设", "基建市政工程", "基础建设", "国际工程", "化学工程", "专业工程",
              "其他专业工程", "工程咨询服务", "装修装饰", "园林工程", "钢结构"],
    "农林牧渔": ["种植业", "种子", "粮食种植", "其他种植业", "养殖业", "生猪养殖", "肉鸡养殖",
              "其他养殖", "渔业", "海洋捕捞", "水产养殖", "农产品加工", "粮油加工", "果蔬加工",
              "其他农产品加工", "饲料", "畜禽饲料", "水产饲料"],
    "商贸零售": ["一般零售", "百货", "超市", "多业态零售", "专业连锁", "互联网电商", "综合电商",
              "跨境电商", "电商服务", "旅游零售", "贸易"],
    "社会服务": ["教育", "学历教育", "培训教育", "教育运营及其他", "体育", "本地生活服务",
              "专业服务", "人力资源服务", "检测服务", "会展服务", "其他专业服务", "旅游及景区",
              "自然景区", "人工景区", "旅游综合", "酒店餐饮", "酒店", "餐饮"],
    "美容护理": ["个护用品", "生活用纸", "洗护用品", "化妆品", "化妆品制造及其他", "品牌化妆品",
              "医疗美容", "医美耗材", "医美服务"],
    "纺织服饰": ["纺织制造", "棉纺", "印染", "服装家纺", "家纺", "非运动服装", "运动服装",
              "鞋帽及其他", "其他纺织", "饰品", "钟表珠宝", "多品类奢侈品", "其他饰品"],
    "轻工制造": ["造纸", "大宗用纸", "特种纸", "家居用品", "成品家居", "定制家居", "瓷砖地板",
              "卫浴制品", "其他家居用品", "文娱用品", "文化用品", "娱乐用品", "包装印刷",
              "金属包装", "纸包装", "综合包装", "印刷"],
}
# 子孙名 -> 一级 反查表（裸名）
_DESC2L1 = {}
for _l1, _kids in SW_DESCENDANTS.items():
    for _k in _kids:
        _DESC2L1[_k] = _l1


def collapse_industry_level(rows, name_key="name_raw", drop_top=True):
    """
    行业资金榜(t:2)层级折叠：东财把申万一/二/三级混在一个池里，导致"电子"(一级大筐)和它底下的
    "半导体/元件/印制电路板"(二/三级)同时上榜，既笼统又重复计算（一级≈子孙之和）。
    规则：某一级行业在榜时，只要同榜还出现了它任意一个更细的子孙，就剔除这条一级大筐（保留细分）；
          若该一级在榜、却没有任何子孙在榜，则保留（避免整块消失）。
    rows:     em_sector_fund_flow('industry') 的返回，或任意含 name_key 的 dict 列表。
    drop_top: True=折叠大筐(默认，推荐)；False=只给每行标注 level 不删除。
    返回处理后的 list（保持原顺序）。
    """
    def bare(n):
        return _LEVEL_SUFFIX.sub("", str(n).strip()).strip()

    present = {bare(r.get(name_key, "")) for r in rows}
    out = []
    for r in rows:
        b = bare(r.get(name_key, ""))
        is_l1 = b in SW_L1
        if is_l1:
            kids = set(SW_DESCENDANTS.get(b, []))
            has_child = bool(present & kids)
            r = dict(r); r["level"] = "L1"
            if drop_top and has_child:
                continue                      # 有更细的子孙在榜 → 吞掉大筐
        elif b in _DESC2L1:
            r = dict(r); r["level"] = "L2/L3"
        out.append(r)
    return out


def industry_fund_flow(topn=15, direction="in", collapse=True, prefer="ths", **kw):
    """
    行业资金榜文本行。默认【同花顺行业主源·原生名】（同花顺行业本就是细分，无需折叠）；
    同花顺不可用时回退东财申万行业池，collapse=True 折叠一级大筐只留细分。
    """
    if prefer != "em":
        lines = ths_industry_lines(topn, direction)
        if lines:
            return lines
    rows, _ = em_sector_fund_flow("industry", topn=100, direction=direction, **kw)
    if collapse:
        rows = collapse_industry_level(rows)[:topn]
    else:
        rows = rows[:topn]
    sign = "净流入" if direction != "out" else "净流出"
    return [f'{i+1}. {r["name_raw"]}：主力{sign}{abs(r["net_yi"]):.2f}亿'
            f'（占比{r["ratio_pct"]:.2f}%，{r["change_pct"]:+.2f}%）'
            for i, r in enumerate(rows)]


def industry_fund_flow_ex(topn=15, direction="in", collapse=True, **kw):
    """同 industry_fund_flow，返回 (文本行list, 数据源标注)。"""
    lines = ths_industry_lines(topn, direction)
    if lines:
        return lines, "同花顺行业(资金净额口径)"
    return industry_fund_flow(topn, direction, collapse, prefer="em", **kw), "东方财富申万行业(主力净额·兜底)"


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

    print("\n【三】申万行业层级去重（一级/二级/三级混表，每个行业只留一条，优先二级）")
    demo_rows = [{"name": "非银金融", "out": 39.84}, {"name": "证券Ⅲ", "out": 22.17},
                 {"name": "证券Ⅱ", "out": 22.17}, {"name": "银行", "out": 16.95},
                 {"name": "银行Ⅱ", "out": 16.95}, {"name": "保险Ⅱ", "out": 13.27},
                 {"name": "保险Ⅲ", "out": 13.27}, {"name": "有色金属", "out": 16.64}]
    for r in dedupe_sw_industry(demo_rows):
        print("  保留:", r)

    print("\n【四】整段复盘报告一键全局标价格（散落在任意榜单的股票全部标出）")
    fake_report = (
        "2026-09-07 复盘\n"
        "涨停股：603330.SH、002631.SZ、600613.SH（15:30 统计，主力净流入39.84亿）\n"
        "龙虎榜：601108.SH、002176.SZ\n"
        "重点监控：603221.SH\n"
    )
    print(enrich_report_text(fake_report, inline="first", with_extra=False))

    print("\n【五】只给指定榜单(红黑榜/好卖型)标价，再把栏目名改成同花顺风格")
    named_report = (
        "2026-09-07 复盘\n"
        "■ 红黑榜\n红榜：603330.SH、002631.SZ\n黑榜：600613.SH\n"
        "■ 好卖型TOP\n002176.SZ、601108.SH、603221.SH\n"
        "■ 主力净流出TOP3\n600048.SH 净流出39.84亿\n"
    )
    named = enrich_named_sections(named_report, ["红黑榜", "好卖型"],
                                  with_extra=False, append_summary=False)
    print(rename_section_titles(named))

    print("\n【六】题材概念资金榜（同花顺搜得到的题材名，已剔融资融券/基金重仓/大盘股等假题材）")
    for line in hot_concepts(15):
        print("  ", line)

    print("\n【七】行业资金榜：原始(混着电子/通信大筐) -> 折叠后(只留细分)")
    raw, host = em_sector_fund_flow("industry", topn=15)
    print("  数据源节点：", host or "全部失败")
    folded = collapse_industry_level(raw)
    for i in range(max(len(raw), len(folded))):
        a = f'{raw[i]["name_raw"]} {raw[i]["net_yi"]:.1f}亿' if i < len(raw) else ""
        b = f'{folded[i]["name_raw"]} {folded[i]["net_yi"]:.1f}亿' if i < len(folded) else ""
        print(f"   原:{a:<22} -> 折叠后:{b}")

    print("\n【八】一键收尾 finalize_report：括号代码全文标价 + 优选股票纯代码清单")
    pick_report = (
        "【打板多空风向标】\n"
        "■ 强势聚焦（优选打板池）\n"
        " 百大集团(600865) 一般零售 评分:9 | 早盘封板、3连板\n"
        " 天沃科技(002564) 专用设备 评分:9 | 2连板\n"
        " 龙版传媒(605577) 出版 评分:8 | 6连板\n"
        "■ 走弱回避\n 平安银行(000001) 银行 评分:3 | 炸板\n"
    )
    print(finalize_report(pick_report, picked_suffix=True))
