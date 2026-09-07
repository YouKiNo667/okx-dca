#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OKX 定投记录工具
================
本地运行的定投记账工具：
  - 连接 OKX API（只读）读取成交流水、持仓余额
  - 自动抓取行情，计算总投入 / 当前市值 / 累计收益 / 年化收益率(XIRR)
  - 图表展示投入与回收走势、持仓构成
  - 流水增删改查、手动补录（OKX 账单 API 只保留近 3 个月，更早的自己补）
只用 Python 标准库，零依赖。密钥和数据都存在本文件同目录下，不上传任何地方。

用法:
    python okx_dca.py
然后浏览器会自动打开 http://127.0.0.1:8787
"""

import base64
import hashlib
import hmac
import json
import os
import random
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as urlerr
from urllib import parse as urlparse
from urllib import request as urlreq

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DATA_PATH = os.path.join(BASE_DIR, "data.json")

HOST = "127.0.0.1"
PORT = 8787
API_BASE = "https://www.okx.com"
DAY_MS = 86400000
STABLES = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "PYUSD", "USD1", "USDE"}

# ---------------------------------------------------------------- 工具函数


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_config():
    cfg = {"apiKey": "", "secretKey": "", "passphrase": "", "proxy": "", "flag": "0"}
    cfg.update(load_json(CONFIG_PATH, {}))
    return cfg


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- OKX API


class OkxError(Exception):
    pass


def _iso_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def okx_call(method, path, params=None, body=None, auth=True):
    """调用 OKX API v5，返回 data 字段。签名方式见官方文档 OK-ACCESS-SIGN。"""
    cfg = load_config()
    query = ("?" + urlparse.urlencode(params)) if params else ""
    request_path = path + query
    url = API_BASE + request_path

    data_bytes = json.dumps(body).encode("utf-8") if body else None
    req = urlreq.Request(url, data=data_bytes, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "okx-dca-local/1.0")

    if auth:
        if not cfg.get("apiKey"):
            raise OkxError("尚未配置 API Key，请先在页面右上角「设置」里填写")
        ts = _iso_ts()
        msg = ts + method + request_path + (json.dumps(body) if body else "")
        sign = base64.b64encode(
            hmac.new(cfg["secretKey"].encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).digest()
        ).decode()
        req.add_header("OK-ACCESS-KEY", cfg["apiKey"])
        req.add_header("OK-ACCESS-SIGN", sign)
        req.add_header("OK-ACCESS-TIMESTAMP", ts)
        req.add_header("OK-ACCESS-PASSPHRASE", cfg["passphrase"])
        if cfg.get("flag") == "1":
            req.add_header("x-simulated-trading", "1")

    if cfg.get("proxy"):
        proxy = cfg["proxy"]
        if "://" not in proxy:
            proxy = "http://" + proxy
        opener = urlreq.build_opener(urlreq.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urlreq.build_opener()  # 留空时 urllib 自动使用 Windows 系统代理

    try:
        with opener.open(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urlerr.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            raise OkxError("HTTP %s: %s" % (e.code, e.reason))
    except urlerr.URLError as e:
        raise OkxError("网络连接失败: %s（如需代理请在设置里填写，如 http://127.0.0.1:7890）" % e.reason)
    except Exception as e:
        raise OkxError("请求异常: %s" % e)

    if payload.get("code") != "0":
        raise OkxError("OKX 返回错误 code=%s msg=%s（data=%s）"
                       % (payload.get("code"), payload.get("msg"), str(payload.get("data"))[:200]))
    return payload.get("data") or []


def fetch_tickers():
    return okx_call("GET", "/api/v5/market/tickers", {"instType": "SPOT"}, auth=False)


def build_price_map(tickers, manual):
    """ccy -> USDT 价格。稳定币记 1，手动覆盖优先。"""
    pm = {c: 1.0 for c in STABLES}
    for t in tickers:
        inst = t.get("instId", "")
        if not inst.endswith("-USDT"):
            continue
        base = inst[:-5]
        try:
            px = float(t.get("last") or 0)
        except ValueError:
            continue
        if px > 0 and base not in pm:
            pm[base] = px
    # USDT 交易对缺失的币，退而求其次用 USDC 对
    for t in tickers:
        inst = t.get("instId", "")
        if not inst.endswith("-USDC"):
            continue
        base = inst[:-5]
        try:
            px = float(t.get("last") or 0)
        except ValueError:
            continue
        if px > 0 and base not in pm:
            pm[base] = px
    for ccy, px in (manual or {}).items():
        try:
            pm[ccy] = float(px)
        except (TypeError, ValueError):
            pass
    return pm


def fetch_recent_bills():
    """账单（近 3 个月，逐页向更早翻）。type=2 为成交。"""
    out, after, pages = [], None, 0
    while pages < 150:
        params = {"limit": "100"}
        if after:
            params["after"] = after
        data = okx_call("GET", "/api/v5/account/bills-archive", params)
        if not data:
            break
        out.extend(data)
        after = min(data, key=lambda d: int(d.get("billId", "0"))).get("billId")
        pages += 1
        if len(data) < 100:
            break
    return out


def bills_to_records(bills, price_map):
    """把成交类账单转成记录。买入 subType=1，卖出 subType=2。"""
    seen, records, skipped = set(), [], {}
    for b in bills:
        bid = b.get("billId")
        if not bid or bid in seen:
            continue
        seen.add(bid)
        if b.get("type") != "2" or b.get("instType") != "SPOT":
            continue
        st = b.get("subType")
        if st == "1":
            side = "buy"
        elif st == "2":
            side = "sell"
        else:
            key = "subType=" + str(st)
            skipped[key] = skipped.get(key, 0) + 1
            continue
        inst = b.get("instId", "")
        if "-" not in inst:
            continue
        base, quote = inst.split("-", 1)
        try:
            qty = float(b.get("sz") or 0)
            price = float(b.get("px") or 0)
        except ValueError:
            qty = price = 0.0
        if qty <= 0 or price <= 0:
            skipped["缺价格/数量"] = skipped.get("缺价格/数量", 0) + 1
            continue
        ts = int(b.get("fillTime") or b.get("ts") or 0)
        try:
            fee = float(b.get("fee") or 0)
        except ValueError:
            fee = 0.0
        records.append({
            "id": "okx:" + bid, "billId": bid, "ts": ts, "instId": inst,
            "base": base, "quote": quote, "side": side, "qty": qty, "price": price,
            "fee": fee, "note": "", "source": "okx",
        })
    return records, skipped


def fetch_balance():
    data = okx_call("GET", "/api/v5/account/balance")
    holdings = []
    if data:
        for d in data[0].get("details", []):
            try:
                eq = float(d.get("eq") or 0)
            except ValueError:
                eq = 0.0
            if eq > 1e-12:
                holdings.append({"ccy": d.get("ccy", ""), "eq": eq})
    holdings.sort(key=lambda h: -h["eq"])
    return holdings


def apply_overrides(records, overrides):
    out = []
    for r in records:
        ov = overrides.get(r.get("billId") or "") if r.get("source") == "okx" else None
        if ov:
            if ov.get("hidden"):
                continue
            r = {**r, **{k: v for k, v in ov.items() if k != "hidden"}}
        out.append(r)
    return out


def do_sync():
    data = load_json(DATA_PATH, {})
    if data.get("demo"):
        data = {}
    tickers = fetch_tickers()
    pm = build_price_map(tickers, data.get("pricesManual", {}))
    bills = fetch_recent_bills()
    records, skipped = bills_to_records(bills, pm)
    holdings = fetch_balance()

    manual = [r for r in data.get("records", []) if r.get("source") == "manual"]
    overrides = data.get("overrides", {})
    merged = apply_overrides(records, overrides)
    data["records"] = manual + merged
    data["holdings"] = holdings
    data["prices"] = pm
    data["lastSync"] = now_ms()
    data["lastError"] = ""
    data["demo"] = False
    extra = ", ".join("%s×%d" % (k, v) for k, v in sorted(skipped.items())) if skipped else ""
    data["lastSyncInfo"] = ("本次同步 %d 笔成交" % len(merged)) + ("；未计入: " + extra if extra else "")
    save_json(DATA_PATH, data)
    return len(merged), data.get("lastSyncInfo")


# ---------------------------------------------------------------- 统计


def record_amount(r, price_map):
    """成交额(USDT)。USDT 计价直接算，其他计价币按当前行情折算。"""
    amt = r.get("qty", 0.0) * r.get("price", 0.0)
    quote = r.get("quote", "USDT")
    if quote != "USDT":
        amt *= price_map.get(quote, 1.0)
    return amt


def xirr(flows):
    """不规则现金流年化收益率。flows: [(ts_ms, amount)]，买入为负、回收为正。"""
    if len(flows) < 2:
        return None
    flows = sorted(flows)
    t0 = flows[0][0]
    xs = [(t - t0) / (365.0 * DAY_MS) for t, _ in flows]
    amts = [a for _, a in flows]
    if not (min(amts) < 0 < max(amts)):
        return None

    def npv(r):
        total = 0.0
        for a, x in zip(amts, xs):
            total += a / ((1.0 + r) ** x)
        return total

    lo, hi = -0.9999, 1.0
    flo = npv(lo)
    tries = 0
    while npv(hi) * flo > 0 and tries < 60:
        hi *= 2
        tries += 1
    if npv(hi) * flo > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        fm = npv(mid)
        if fm == 0:
            return mid
        if fm * flo > 0:
            lo, flo = mid, fm
        else:
            hi = mid
    return (lo + hi) / 2.0


def bucket_series(records, start_ms, price_map):
    """按天累计：投入、回收。返回 [ {t, invested, recovered} ]，最多约 160 个点。"""
    buys = {}
    sells = {}
    for r in records:
        ts = r.get("ts", 0)
        if start_ms and ts < start_ms:
            continue
        day = ts // DAY_MS * DAY_MS
        amt = record_amount(r, price_map)
        d = buys if r.get("side") == "buy" else sells
        d[day] = d.get(day, 0.0) + amt
    if not buys and not sells:
        return []
    lo = min(list(buys) + list(sells))
    hi = now_ms() // DAY_MS * DAY_MS
    inv = rec = 0.0
    pts = []
    day = lo
    while day <= hi:
        inv += buys.get(day, 0.0)
        rec += sells.get(day, 0.0)
        pts.append({"t": day, "invested": inv, "recovered": rec})
        day += DAY_MS
    if len(pts) > 160:
        stride = -(-len(pts) // 160)
        pts = pts[::stride] + [pts[-1]] if stride > 1 else pts
    return pts


def compute_state(days=None):
    data = load_json(DATA_PATH, {})
    price_map = data.get("prices", {})
    manual_prices = data.get("pricesManual", {})
    for c, p in manual_prices.items():
        try:
            price_map[c] = float(p)
        except (TypeError, ValueError):
            pass
    records = apply_overrides(data.get("records", []), data.get("overrides", {}))
    records = [r for r in records if r.get("ts")]
    records.sort(key=lambda r: r["ts"])
    for r in records:
        r["amount"] = record_amount(r, price_map)

    holdings = data.get("holdings", [])
    cash, portfolio, unknown = 0.0, 0.0, []
    holding_rows = []
    for h in holdings:
        ccy, eq = h["ccy"], h["eq"]
        if ccy in STABLES:
            cash += eq
            holding_rows.append({"ccy": ccy, "qty": eq, "price": 1.0, "value": eq, "cash": True})
            continue
        px = price_map.get(ccy)
        if px is None:
            unknown.append(ccy)
            holding_rows.append({"ccy": ccy, "qty": eq, "price": None, "value": None, "cash": False})
        else:
            portfolio += eq * px
            holding_rows.append({"ccy": ccy, "qty": eq, "price": px, "value": eq * px, "cash": False})

    invested = sum(r["amount"] for r in records if r["side"] == "buy")
    recovered = sum(r["amount"] for r in records if r["side"] == "sell")
    pnl = portfolio + recovered - invested
    roi = (pnl / invested) if invested > 0 else None
    flows = [(r["ts"], -r["amount"] if r["side"] == "buy" else r["amount"]) for r in records]
    if portfolio > 0:
        flows.append((now_ms(), portfolio))
    rate = xirr(flows)

    start_ms = (now_ms() - days * DAY_MS) if days else None
    series = bucket_series(records, start_ms, price_map)
    table = [r for r in records if (not start_ms or r["ts"] >= start_ms)]
    table = table[-2000:][::-1]

    hc_rows = [x for x in holding_rows if x["value"]]
    hc_rows.sort(key=lambda x: -x["value"])
    if len(hc_rows) > 7:
        rest = sum(x["value"] for x in hc_rows[6:])
        hc_rows = hc_rows[:6] + [{"ccy": "其他", "value": rest, "qty": None, "price": None, "cash": False}]
    total_hc = sum(x["value"] for x in hc_rows) or 1.0
    for x in hc_rows:
        x["share"] = x["value"] / total_hc

    hidden_count = sum(1 for ov in data.get("overrides", {}).values() if ov.get("hidden"))
    return {
        "configured": bool(load_config().get("apiKey")),
        "demo": bool(data.get("demo")),
        "lastSync": data.get("lastSync"),
        "lastSyncInfo": data.get("lastSyncInfo", ""),
        "lastError": data.get("lastError", ""),
        "hiddenCount": hidden_count,
        "stats": {
            "invested": invested, "recovered": recovered, "portfolio": portfolio,
            "cash": cash, "pnl": pnl, "roi": roi, "xirr": rate,
            "buyCount": sum(1 for r in records if r["side"] == "buy"),
            "sellCount": sum(1 for r in records if r["side"] == "sell"),
            "unknown": unknown,
        },
        "series": series,
        "holdingsChart": hc_rows,
        "holdings": holding_rows,
        "records": table,
        "recordTotal": len(records),
    }


# ---------------------------------------------------------------- 演示数据


def make_demo_data():
    rnd = random.Random(20260907)
    assets = [
        ("BTC", 32000.0, 50.0),
        ("ETH", 1900.0, 30.0),
        ("SOL", 32.0, 20.0),
        ("PEPE", 0.0000085, 10.0),
    ]
    weeks = 78  # 约 18 个月
    start = now_ms() - weeks * 7 * DAY_MS
    records = []
    qty_sum = {a[0]: 0.0 for a in assets}
    px = {a[0]: a[1] for a in assets}
    for w in range(weeks):
        for name, base, weekly in assets:
            drift = {"BTC": 0.0045, "ETH": 0.004, "SOL": 0.006, "PEPE": 0.003}[name]
            px[name] = max(px[name] * (1 + (rnd.random() - 0.47) * 0.09 + drift), base * 0.2)
            t = start + w * 7 * DAY_MS + rnd.randrange(0, 3 * DAY_MS)
            qty = weekly / px[name]
            qty_sum[name] += qty
            records.append({
                "id": "okx:demo%s%d" % (name, w), "billId": "demo%s%d" % (name, w),
                "ts": t, "instId": name + "-USDT", "base": name, "quote": "USDT",
                "side": "buy", "qty": qty, "price": px[name], "fee": 0.0,
                "note": "每周定投", "source": "okx",
            })
            if rnd.random() < 0.03 and qty_sum[name] * px[name] > 200:
                sell_qty = qty_sum[name] * 0.2
                qty_sum[name] -= sell_qty
                records.append({
                    "id": "okx:demos%s%d" % (name, w), "billId": "demos%s%d" % (name, w),
                    "ts": t + DAY_MS, "instId": name + "-USDT", "base": name, "quote": "USDT",
                    "side": "sell", "qty": sell_qty, "price": px[name] * (1 + rnd.random() * 0.01),
                    "fee": 0.0, "note": "止盈", "source": "okx",
                })
    holdings = [{"ccy": n, "eq": q} for n, q in qty_sum.items() if q > 0]
    holdings.append({"ccy": "USDT", "eq": 123.45})
    data = {
        "demo": True,
        "lastSync": now_ms(),
        "lastError": "",
        "lastSyncInfo": "演示数据：约 18 个月的模拟定投记录",
        "records": records,
        "overrides": {},
        "pricesManual": {},
        "holdings": holdings,
        "prices": {n: px[n] for n, _, _ in assets},
    }
    return data


# ---------------------------------------------------------------- CSV 导出


def export_csv():
    state = compute_state()
    rows = ["时间,交易对,方向,数量,价格,金额(USDT),手续费,备注,来源"]
    for r in state["records"]:
        rows.append(",".join([
            datetime.fromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d %H:%M:%S"),
            r.get("instId", ""), "买入" if r["side"] == "buy" else "卖出",
            repr(r.get("qty", 0)), repr(r.get("price", 0)), "%.2f" % r.get("amount", 0),
            repr(r.get("fee", 0)), '"%s"' % (r.get("note", "").replace('"', '""')),
            "OKX" if r["source"] == "okx" else "手动",
        ]))
    return "﻿" + "\r\n".join(rows)


# ---------------------------------------------------------------- HTTP 服务


class Handler(BaseHTTPRequestHandler):
    server_version = "OkxDCA/1.0"

    def log_message(self, *args):
        pass

    def _send(self, code, ctype, body):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False))

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def do_GET(self):
        parsed = urlparse.urlparse(self.path)
        try:
            if parsed.path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE)
            elif parsed.path == "/api/state":
                q = urlparse.parse_qs(parsed.query)
                days = int(q.get("days", ["0"])[0]) or None
                self._json(compute_state(days))
            elif parsed.path == "/api/export.csv":
                self._send(200, "text/csv; charset=utf-8", export_csv())
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # 兜底，避免线程崩掉
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        path = urlparse.urlparse(self.path).path
        body = self._read_body()
        try:
            if path == "/api/config":
                cfg = load_config()
                for k in ("apiKey", "secretKey", "passphrase", "proxy", "flag"):
                    if k in body:
                        cfg[k] = str(body[k]).strip()
                save_json(CONFIG_PATH, cfg)
                try:
                    okx_call("GET", "/api/v5/account/config")
                    self._json({"ok": True, "msg": "连接成功，密钥可用"})
                except OkxError as e:
                    self._json({"ok": False, "msg": "密钥已保存，但连接失败：%s" % e})
            elif path == "/api/sync":
                try:
                    added, info = do_sync()
                    self._json({"ok": True, "added": added, "info": info})
                except OkxError as e:
                    data = load_json(DATA_PATH, {})
                    data["lastError"] = str(e)
                    save_json(DATA_PATH, data)
                    self._json({"ok": False, "error": str(e)})
            elif path == "/api/demo":
                save_json(DATA_PATH, make_demo_data())
                self._json({"ok": True})
            elif path == "/api/clear":
                save_json(DATA_PATH, {})
                self._json({"ok": True})
            elif path == "/api/records/add":
                add_manual_record(body)
                self._json({"ok": True})
            elif path == "/api/records/update":
                update_record(body)
                self._json({"ok": True})
            elif path == "/api/records/delete":
                delete_record(body.get("id", ""))
                self._json({"ok": True})
            elif path == "/api/records/unhide":
                data = load_json(DATA_PATH, {})
                ovs = data.get("overrides", {})
                for k in list(ovs):
                    ovs[k].pop("hidden", None)
                    if not ovs[k]:
                        del ovs[k]
                save_json(DATA_PATH, data)
                self._json({"ok": True})
            elif path == "/api/prices/set":
                data = load_json(DATA_PATH, {})
                pm = data.setdefault("pricesManual", {})
                ccy = str(body.get("ccy", "")).strip()
                if ccy:
                    try:
                        pm[ccy] = float(body.get("price"))
                    except (TypeError, ValueError):
                        pm.pop(ccy, None)
                save_json(DATA_PATH, data)
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)


def add_manual_record(body):
    inst = str(body.get("instId", "")).strip().upper() or "UNKNOWN-USDT"
    if "-" not in inst:
        inst = inst + "-USDT"
    base, quote = inst.split("-", 1)
    ts = int(float(body.get("ts") or now_ms()))
    if ts < 10**12:  # 秒级时间戳兜底
        ts *= 1000
    rec = {
        "id": "m:" + uuid.uuid4().hex[:12], "ts": ts, "instId": inst,
        "base": base, "quote": quote,
        "side": "sell" if body.get("side") == "sell" else "buy",
        "qty": float(body.get("qty") or 0), "price": float(body.get("price") or 0),
        "fee": 0.0, "note": str(body.get("note", ""))[:200], "source": "manual",
    }
    data = load_json(DATA_PATH, {})
    data.setdefault("records", []).append(rec)
    save_json(DATA_PATH, data)


def update_record(body):
    rid = str(body.get("id", ""))
    fields = {}
    for k in ("ts", "qty", "price", "note", "side", "instId"):
        if k in body:
            fields[k] = body[k]
    if "ts" in fields and float(fields["ts"]) < 10**12:
        fields["ts"] = float(fields["ts"]) * 1000
    data = load_json(DATA_PATH, {})
    for r in data.get("records", []):
        if r.get("id") == rid:
            if r.get("source") == "manual":
                for k, v in fields.items():
                    if k in ("qty", "price"):
                        r[k] = float(v)
                    elif k == "ts":
                        r[k] = int(v)
                    else:
                        r[k] = v
            else:
                ov = data.setdefault("overrides", {}).setdefault(r.get("billId", ""), {})
                for k, v in fields.items():
                    ov[k] = int(v) if k == "ts" else v
            break
    save_json(DATA_PATH, data)


def delete_record(rid):
    data = load_json(DATA_PATH, {})
    recs = data.get("records", [])
    for r in recs:
        if r.get("id") == rid:
            if r.get("source") == "manual":
                recs.remove(r)
            else:
                data.setdefault("overrides", {}).setdefault(r.get("billId", ""), {})["hidden"] = True
            break
    save_json(DATA_PATH, data)


# ---------------------------------------------------------------- 页面

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OKX 定投记录</title>
<style>
  :root { color-scheme: light; }
  body {
    margin: 0; background: var(--plane); color: var(--ink);
    font: 14px/1.6 system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }
  .viz-root {
    --plane: #f9f9f7; --surface: #fcfcfb;
    --ink: #0b0b0b; --ink2: #52514e; --muted: #898781;
    --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
    --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a;
    --good: #006300; --bad: #d03b3b;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) .viz-root {
      color-scheme: dark;
      --plane: #0d0d0d; --surface: #1a1a19;
      --ink: #ffffff; --ink2: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
      --s1: #3987e5; --s2: #d95926; --s3: #199e70;
      --good: #0ca30c; --bad: #e66767;
    }
  }
  :root[data-theme="dark"] .viz-root {
    color-scheme: dark;
    --plane: #0d0d0d; --surface: #1a1a19;
    --ink: #ffffff; --ink2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --s1: #3987e5; --s2: #d95926; --s3: #199e70;
    --good: #0ca30c; --bad: #e66767;
  }
  .viz-root { min-height: 100vh; }
  main { max-width: 1080px; margin: 0 auto; padding: 20px 20px 48px; }
  header { display: flex; align-items: center; gap: 10px; padding: 6px 0 14px; }
  header h1 { font-size: 18px; margin: 0 auto 0 0; font-weight: 650; }
  button, select, input {
    font: inherit; color: inherit; border-radius: 8px;
    border: 1px solid var(--ring); background: var(--surface);
  }
  button { padding: 6px 14px; cursor: pointer; }
  button:hover { border-color: var(--axis); }
  button.primary { background: var(--s1); border-color: var(--s1); color: #fff; }
  button.ghost { padding: 4px 10px; font-size: 13px; }
  .card {
    background: var(--surface); border: 1px solid var(--ring);
    border-radius: 12px; padding: 16px 18px; margin-bottom: 16px;
  }
  .card h2 { font-size: 14px; font-weight: 650; margin: 0 0 10px; color: var(--ink); }
  .muted { color: var(--muted); font-size: 12.5px; }
  .sub { color: var(--ink2); font-size: 12.5px; }
  /* KPI */
  #kpi { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
  .tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 12px 16px; }
  .tile .lab { font-size: 12.5px; color: var(--ink2); }
  .tile .val { font-size: 26px; font-weight: 600; margin-top: 2px; }
  .tile .delta { font-size: 12.5px; margin-top: 2px; }
  .up { color: var(--good); } .down { color: var(--bad); }
  /* 筛选行 */
  .filterrow { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
  .filterrow .spacer { flex: 1; }
  .seg { display: inline-flex; border: 1px solid var(--ring); border-radius: 8px; overflow: hidden; }
  .seg button { border: 0; border-radius: 0; background: var(--surface); padding: 5px 12px; font-size: 13px; }
  .seg button.on { background: var(--s1); color: #fff; }
  /* 图表 */
  .chartbox { position: relative; }
  svg.chart { display: block; width: 100%; }
  .legend { display: flex; gap: 16px; font-size: 12.5px; color: var(--ink2); margin-bottom: 4px; }
  .legend .key { display: inline-block; width: 14px; height: 0; border-top: 2px solid; vertical-align: middle; margin-right: 5px; }
  .tooltip {
    position: absolute; pointer-events: none; display: none; z-index: 5;
    background: var(--surface); border: 1px solid var(--ring); border-radius: 8px;
    box-shadow: 0 4px 14px rgba(0,0,0,0.14); padding: 8px 10px; font-size: 12.5px; min-width: 130px;
  }
  .tooltip .tv { font-weight: 600; color: var(--ink); }
  .tooltip .tl { color: var(--ink2); }
  /* 横向条形 */
  .brow { display: grid; grid-template-columns: 76px 1fr 96px; gap: 10px; align-items: center; padding: 3px 0; }
  .brow:hover .bfill { opacity: .85; }
  .blab { color: var(--ink2); font-size: 13px; text-align: right; }
  .btrack { height: 18px; }
  .bfill { height: 18px; background: var(--s1); border-radius: 0 4px 4px 0; min-width: 2px; }
  .bval { font-size: 12.5px; color: var(--ink2); font-variant-numeric: tabular-nums; }
  /* 表格 */
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { color: var(--muted); font-weight: 500; text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
  td { padding: 6px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr:hover td { background: color-mix(in srgb, var(--s1) 6%, transparent); }
  .sidebuy, .sidesell { font-weight: 600; }
  .sidebuy::before, .sidesell::before { content: ""; display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .sidebuy::before { background: var(--s1); } .sidesell::before { background: var(--s2); }
  .tblwrap { overflow-x: auto; }
  /* 表单 */
  .formgrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
  .formgrid label { display: block; font-size: 12px; color: var(--ink2); margin-bottom: 3px; }
  .formgrid input, .formgrid select { width: 100%; box-sizing: border-box; padding: 6px 8px; }
  #setup input { width: 100%; box-sizing: border-box; padding: 6px 8px; margin-bottom: 8px; }
  #banner { border-radius: 10px; padding: 10px 14px; margin-bottom: 16px; font-size: 13px;
            border: 1px solid var(--ring); background: var(--surface); }
  #banner.err { border-color: var(--bad); color: var(--bad); }
  dialog { border: 1px solid var(--ring); border-radius: 12px; background: var(--surface); color: var(--ink);
           padding: 18px; width: 440px; max-width: 92vw; }
  dialog::backdrop { background: rgba(0,0,0,0.35); }
  footer { color: var(--muted); font-size: 12px; margin-top: 24px; line-height: 1.8; }
  a { color: var(--s1); }
  @media (max-width: 760px) { #kpi { grid-template-columns: repeat(2, 1fr); } }
</style>
</head>
<body class="viz-root">
<main>
  <header>
    <h1>OKX 定投记录</h1>
    <button class="ghost" id="btn-theme" title="切换明暗">🌓</button>
    <button class="ghost" id="btn-setup">设置</button>
  </header>

  <section id="setup" class="card" hidden>
    <h2>连接 OKX</h2>
    <p class="sub" style="margin-top:0">
      在 OKX 网页版 <b>用户中心 → API → 创建 API</b> 创建一个密钥，权限<b>只勾「读取」</b>（不要勾交易和提币），
      把三个值填在下面。密钥只保存在本机 config.json，不会上传到任何地方。
    </p>
    <div class="formgrid">
      <div style="grid-column: 1 / -1"><label>API Key</label><input id="c-key" autocomplete="off"></div>
      <div style="grid-column: 1 / -1"><label>Secret Key</label><input id="c-secret" type="password" autocomplete="off"></div>
      <div style="grid-column: 1 / -1"><label>Passphrase</label><input id="c-pass" type="password" autocomplete="off"></div>
      <div><label>环境</label><select id="c-flag"><option value="0">实盘</option><option value="1">模拟盘</option></select></div>
      <div><label>代理（可选，如 http://127.0.0.1:7890）</label><input id="c-proxy" placeholder="留空 = 系统代理/直连"></div>
    </div>
    <p style="margin: 10px 0 0">
      <button class="primary" id="btn-savecfg">保存并测试连接</button>
      <button id="btn-demo">先看看演示数据</button>
      <span id="cfgmsg" class="sub"></span>
    </p>
  </section>

  <div id="banner" hidden></div>

  <section id="kpi">
    <div class="tile"><div class="lab">总投入</div><div class="val" id="k-invest">—</div><div class="delta sub" id="k-invest-sub"></div></div>
    <div class="tile"><div class="lab">当前市值</div><div class="val" id="k-value">—</div><div class="delta sub" id="k-value-sub"></div></div>
    <div class="tile"><div class="lab">累计收益</div><div class="val" id="k-pnl">—</div><div class="delta" id="k-pnl-sub"></div></div>
    <div class="tile"><div class="lab">年化收益率 (XIRR)</div><div class="val" id="k-xirr">—</div><div class="delta sub">按全部记录计算</div></div>
  </section>

  <div class="filterrow">
    <span class="seg" id="range-seg"></span>
    <span class="spacer"></span>
    <button id="btn-add">手动记一笔</button>
    <a id="btn-csv" href="/api/export.csv"><button>导出 CSV</button></a>
    <button class="primary" id="btn-sync">同步 OKX</button>
  </div>

  <section class="card">
    <h2>投入与回收走势</h2>
    <div class="legend" id="line-legend"></div>
    <div class="chartbox" id="linebox">
      <svg class="chart" id="linechart" height="260" tabindex="0" role="img" aria-label="累计投入与累计回收折线图"></svg>
      <div class="tooltip" id="line-tip"></div>
    </div>
  </section>

  <section class="card">
    <h2>持仓构成（当前市值）</h2>
    <div id="barchart"></div>
  </section>

  <section class="card">
    <h2>持仓明细</h2>
    <div class="tblwrap"><table id="holdtbl"></table></div>
    <p class="muted" id="holdnote"></p>
  </section>

  <section class="card">
    <h2>流水记录 <span class="muted" id="recmeta"></span></h2>
    <div class="tblwrap"><table id="rectbl"></table></div>
    <p class="muted" id="recnote"></p>
  </section>

  <footer>
    · OKX 账单 API 只保留近 3 个月，更早的定投请用「手动记一笔」补录，持仓以账户余额为准不受影响。<br>
    · 金额 = 数量 × 成交价，未计手续费；非 USDT 计价的成交按当前行情折算。<br>
    · XIRR（资金加权年化）：把每笔买入记为流出、卖出记为流入、当前市值记为今日流入后求解年化利率，比「总收益 ÷ 总投入」更真实。<br>
    · 所有数据存于本目录 data.json，删除文件即清空；config.json 存放你的 API 密钥，注意不要外传。
  </footer>
</main>

<dialog id="dlg">
  <h2 id="dlg-title" style="margin:0 0 12px;font-size:15px">手动记一笔</h2>
  <div class="formgrid">
    <div><label>时间</label><input id="f-ts" type="datetime-local"></div>
    <div><label>交易对（如 BTC-USDT）</label><input id="f-inst" placeholder="BTC-USDT"></div>
    <div><label>方向</label><select id="f-side"><option value="buy">买入</option><option value="sell">卖出</option></select></div>
    <div><label>数量</label><input id="f-qty" type="number" step="any" min="0"></div>
    <div><label>成交价 (USDT)</label><input id="f-px" type="number" step="any" min="0"></div>
    <div style="grid-column:1/-1"><label>备注</label><input id="f-note" maxlength="200"></div>
  </div>
  <p style="margin:14px 0 0;display:flex;gap:8px;justify-content:flex-end">
    <button id="f-cancel">取消</button>
    <button class="primary" id="f-save">保存</button>
  </p>
</dialog>

<script>
"use strict";
let S = null;          // 当前 state
let rangeDays = 0;     // 0 = 全部
let editId = null;     // 编辑中的记录 id
const $ = (id) => document.getElementById(id);
const DAY = 86400000;

/* ---------- 主题 ---------- */
function applyTheme(t) {
  if (t === "dark" || t === "light") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}
applyTheme(localStorage.getItem("dca-theme") || "");
$("btn-theme").onclick = () => {
  const cur = document.documentElement.dataset.theme
    || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  localStorage.setItem("dca-theme", next);
  applyTheme(next);
  if (S) renderAll();
};

/* ---------- 格式化 ---------- */
const fmtMoney = (v) => v == null ? "—" : v.toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const fmtMoney0 = (v) => v == null ? "—" : v.toLocaleString("zh-CN", {maximumFractionDigits: 0});
const fmtPct = (v, signed) => v == null ? "—" :
  (signed && v > 0 ? "+" : "") + (v * 100).toFixed(2) + "%";
function fmtQty(v) {
  if (v == null) return "—";
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a >= 1e6 || a < 0.0001) return v.toExponential(4);
  return v.toLocaleString("zh-CN", {maximumFractionDigits: a >= 100 ? 4 : 8});
}
const fmtDate = (ms, withTime) => {
  const d = new Date(ms);
  const p = (x) => String(x).padStart(2, "0");
  return withTime ? d.getFullYear() + "-" + p(d.getMonth()+1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes())
                  : d.getFullYear() + "-" + p(d.getMonth()+1) + "-" + p(d.getDate());
};

/* ---------- 数据加载 ---------- */
async function api(path, body) {
  const opt = body ? {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)}
                    : {};
  const r = await fetch(path, opt);
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return j;
}
async function refresh() {
  try {
    S = await api("/api/state" + (rangeDays ? "?days=" + rangeDays : ""));
    showBanner();
    renderAll();
  } catch (e) {
    showBanner("加载失败: " + e.message, true);
  }
}
function showBanner(msg, isErr) {
  const b = $("banner");
  if (msg) { b.hidden = false; b.className = isErr ? "err" : ""; b.textContent = msg; return; }
  if (!S) { b.hidden = true; return; }
  if (S.demo) { b.hidden = false; b.className = ""; b.textContent = "当前显示的是演示数据。配置好 API Key 后点「同步 OKX」即可换成真实数据。"; return; }
  if (S.lastError) { b.hidden = false; b.className = "err"; b.textContent = "上次同步失败: " + S.lastError; return; }
  b.hidden = true;
}

/* ---------- 渲染 ---------- */
function renderAll() {
  if (!S) return;
  renderKpi();
  renderSeries();
  renderBars();
  renderHoldings();
  renderRecords();
  $("btn-sync").disabled = !S.configured;
}

function renderKpi() {
  const st = S.stats;
  $("k-invest").textContent = fmtMoney(st.invested);
  $("k-invest-sub").textContent = "买入 " + st.buyCount + " 笔 · 回收 " + fmtMoney(st.recovered);
  const val = st.portfolio;
  $("k-value").textContent = fmtMoney(val);
  $("k-value-sub").textContent = st.cash > 0 ? "另有现金 " + fmtMoney(st.cash) : "不含稳定币现金";
  const pnl = $("k-pnl");
  pnl.textContent = (st.pnl > 0 ? "+" : "") + fmtMoney(st.pnl);
  pnl.className = "val " + (st.pnl > 0 ? "up" : st.pnl < 0 ? "down" : "");
  const sub = $("k-pnl-sub");
  sub.textContent = "收益率 " + fmtPct(st.roi, true);
  sub.className = "delta " + (st.pnl > 0 ? "up" : st.pnl < 0 ? "down" : "");
  $("k-xirr").textContent = fmtPct(st.xirr, true);
  const u = st.unknown && st.unknown.length ? " · " + st.unknown.length + " 个币无行情" : "";
  $("k-xirr").title = u;
}

/* ----- 折线图（累计投入 / 累计回收）----- */
const NS = "http://www.w3.org/2000/svg";
function el(tag, attrs) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}
function niceTicks(max, n) {
  if (max <= 0) return [0, 1];
  const rough = max / n;
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  let step = mag;
  for (const m of [1, 2, 2.5, 5, 10]) if (rough <= m * mag) { step = m * mag; break; }
  const top = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = 0; v <= top + 1e-9; v += step) ticks.push(v);
  return ticks;
}
let linePts = [], lineX = null;
function renderSeries() {
  const svg = $("linechart");
  const box = $("linebox");
  svg.innerHTML = "";
  linePts = S.series || [];
  const lg = $("line-legend");
  lg.innerHTML = "";
  const defs = [["累计投入", getCss("--s1")], ["累计回收", getCss("--s2")]];
  for (const [name, color] of defs) {
    const sp = document.createElement("span");
    const key = document.createElement("span");
    key.className = "key"; key.style.borderTopColor = color;
    sp.appendChild(key); sp.appendChild(document.createTextNode(name));
    lg.appendChild(sp);
  }
  if (linePts.length < 2) {
    const t = el("text", {x: 20, y: 40, fill: getCss("--muted"), "font-size": 13});
    t.textContent = "暂无数据";
    svg.appendChild(t);
    return;
  }
  const W = box.clientWidth || 900, H = 260;
  const m = {l: 58, r: 18, t: 12, b: 26};
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  svg.setAttribute("width", W); svg.setAttribute("height", H);
  const maxV = Math.max(...linePts.map(p => Math.max(p.invested, p.recovered)));
  const ticks = niceTicks(maxV, 4);
  const yMax = ticks[ticks.length - 1];
  const t0 = linePts[0].t, t1 = linePts[linePts.length - 1].t;
  const X = (t) => m.l + (t - t0) / (t1 - t0 || 1) * (W - m.l - m.r);
  const Y = (v) => H - m.b - v / (yMax || 1) * (H - m.t - m.b);
  lineX = X;
  // 网格 + y 轴刻度
  for (const v of ticks) {
    svg.appendChild(el("line", {x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v),
      stroke: v === 0 ? getCss("--axis") : getCss("--grid"), "stroke-width": 1}));
    const tx = el("text", {x: m.l - 8, y: Y(v) + 4, "text-anchor": "end", "font-size": 11,
      fill: getCss("--muted"), "font-variant-numeric": "tabular-nums"});
    tx.textContent = fmtMoney0(v);
    svg.appendChild(tx);
  }
  // x 轴刻度（约 5 个）
  const nx = Math.min(5, linePts.length);
  for (let i = 0; i < nx; i++) {
    const p = linePts[Math.round(i * (linePts.length - 1) / (nx - 1))];
    const tx = el("text", {x: X(p.t), y: H - 8, "text-anchor": i === 0 ? "start" : i === nx - 1 ? "end" : "middle",
      "font-size": 11, fill: getCss("--muted")});
    tx.textContent = fmtDate(p.t);
    svg.appendChild(tx);
  }
  // 两条线 + 端点
  const draw = (key, color) => {
    const d = linePts.map((p, i) => (i ? "L" : "M") + X(p.t).toFixed(1) + "," + Y(p[key]).toFixed(1)).join("");
    svg.appendChild(el("path", {d, fill: "none", stroke: color, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round"}));
    const last = linePts[linePts.length - 1];
    svg.appendChild(el("circle", {cx: X(last.t), cy: Y(last[key]), r: 4.5, fill: color,
      stroke: getCss("--surface"), "stroke-width": 2}));
    const lb = el("text", {x: X(last.t) - 8, y: Y(last[key]) - 8, "text-anchor": "end",
      "font-size": 11.5, fill: getCss("--ink2")});
    lb.textContent = fmtMoney0(last[key]);
    svg.appendChild(lb);
  };
  draw("invested", getCss("--s1"));
  draw("recovered", getCss("--s2"));
  // 悬浮层
  const cross = el("line", {y1: m.t, y2: H - m.b, stroke: getCss("--axis"), "stroke-width": 1, visibility: "hidden"});
  svg.appendChild(cross);
  const overlay = el("rect", {x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent"});
  svg.appendChild(overlay);
  const tip = $("line-tip");
  const nearest = (mx) => {
    const t = t0 + (mx - m.l) / (W - m.l - m.r) * (t1 - t0);
    let bi = 0, bd = Infinity;
    linePts.forEach((p, i) => { const d = Math.abs(p.t - t); if (d < bd) { bd = d; bi = i; } });
    return bi;
  };
  const showIdx = (i, clientX) => {
    const p = linePts[i];
    cross.setAttribute("x1", X(p.t)); cross.setAttribute("x2", X(p.t));
    cross.setAttribute("visibility", "visible");
    tip.innerHTML = "";
    const head = document.createElement("div");
    head.className = "tv"; head.textContent = fmtDate(p.t);
    tip.appendChild(head);
    const rows = [["累计投入", p.invested, getCss("--s1")], ["累计回收", p.recovered, getCss("--s2")],
                  ["净投入", p.invested - p.recovered, null]];
    for (const [name, v, c] of rows) {
      const row = document.createElement("div");
      if (c) { const k = document.createElement("span"); k.className = "key"; k.style.borderTopColor = c; k.style.marginRight = "5px"; row.appendChild(k); }
      else { const k = document.createElement("span"); k.style.display = "inline-block"; k.style.width = "19px"; row.appendChild(k); }
      const val = document.createElement("span"); val.className = "tv"; val.textContent = fmtMoney(v);
      const lab = document.createElement("span"); lab.className = "tl"; lab.textContent = " " + name;
      row.appendChild(val); row.appendChild(lab);
      tip.appendChild(row);
    }
    tip.style.display = "block";
    const bw = box.clientWidth, tw = tip.offsetWidth || 150;
    const px = Math.min(Math.max(X(p.t) + 12, 0), bw - tw - 4);
    tip.style.left = px + "px";
    tip.style.top = "20px";
  };
  const hide = () => { cross.setAttribute("visibility", "hidden"); tip.style.display = "none"; };
  overlay.addEventListener("pointermove", (ev) => {
    const r = svg.getBoundingClientRect();
    showIdx(nearest((ev.clientX - r.left) * W / r.width), ev.clientX);
  });
  overlay.addEventListener("pointerleave", hide);
  svg.addEventListener("keydown", (ev) => {
    if (!linePts.length) return;
    const cur = tip.style.display === "block" ? (svg._idx || 0) : 0;
    if (ev.key === "ArrowRight") { svg._idx = Math.min(cur + 1, linePts.length - 1); showIdx(svg._idx); ev.preventDefault(); }
    if (ev.key === "ArrowLeft")  { svg._idx = Math.max(cur - 1, 0); showIdx(svg._idx); ev.preventDefault(); }
    if (ev.key === "Escape") hide();
  });
  svg.addEventListener("blur", hide);
}
function getCss(v) { return getComputedStyle(document.body).getPropertyValue(v).trim(); }

/* ----- 持仓构成条形 ----- */
function renderBars() {
  const box = $("barchart");
  box.innerHTML = "";
  const rows = S.holdingsChart || [];
  if (!rows.length) { box.innerHTML = '<p class="muted">暂无持仓数据</p>'; return; }
  const maxV = rows[0].value || 1;
  for (const r of rows) {
    const row = document.createElement("div"); row.className = "brow";
    const lab = document.createElement("div"); lab.className = "blab"; lab.textContent = r.ccy;
    const track = document.createElement("div"); track.className = "btrack";
    const fill = document.createElement("div"); fill.className = "bfill";
    fill.style.width = Math.max(r.value / maxV * 100, 0.8) + "%";
    fill.title = r.ccy + " " + fmtMoney(r.value) + "（" + fmtPct(r.share) + "）";
    track.appendChild(fill);
    const val = document.createElement("div"); val.className = "bval"; val.textContent = fmtMoney(r.value);
    row.appendChild(lab); row.appendChild(track); row.appendChild(val);
    box.appendChild(row);
  }
}

/* ----- 持仓明细 ----- */
function renderHoldings() {
  const tbl = $("holdtbl");
  tbl.innerHTML = "";
  const thead = document.createElement("thead");
  thead.innerHTML = "<tr><th>币种</th><th class='num'>数量</th><th class='num'>单价 (USDT)</th><th class='num'>市值 (USDT)</th><th class='num'>占比</th></tr>";
  tbl.appendChild(thead);
  const tb = document.createElement("tbody");
  const rows = [...(S.holdings || [])].sort((a, b) => (b.value || 0) - (a.value || 0));
  const total = rows.reduce((s, r) => s + (r.value || 0), 0) || 1;
  for (const r of rows) {
    const tr = document.createElement("tr");
    const td = (txt, cls) => { const c = document.createElement("td"); if (cls) c.className = cls; c.textContent = txt; return c; };
    tr.appendChild(td(r.ccy + (r.cash ? "（现金）" : "")));
    tr.appendChild(td(fmtQty(r.qty), "num"));
    if (r.price == null) {
      const c = document.createElement("td"); c.className = "num";
      const inp = document.createElement("input");
      inp.type = "number"; inp.step = "any"; inp.placeholder = "填个价";
      inp.style.width = "90px"; inp.style.padding = "2px 6px";
      const btn = document.createElement("button"); btn.className = "ghost"; btn.textContent = "保存";
      btn.onclick = async () => {
        await api("/api/prices/set", {ccy: r.ccy, price: parseFloat(inp.value)});
        refresh();
      };
      c.appendChild(inp); c.appendChild(btn);
      tr.appendChild(c);
      tr.appendChild(td("无行情", "num"));
      tr.appendChild(td("—", "num"));
    } else {
      tr.appendChild(td(fmtQty(r.price), "num"));
      tr.appendChild(td(fmtMoney(r.value), "num"));
      tr.appendChild(td(fmtPct((r.value || 0) / total), "num"));
    }
    tb.appendChild(tr);
  }
  tbl.appendChild(tb);
  const u = S.stats.unknown;
  $("holdnote").textContent = u && u.length
    ? "「无行情」的币种在 OKX 现货没有 USDT/USDC 交易对，手动填一个单价即可计入市值（存于 data.json）。"
    : "";
}

/* ----- 流水表 ----- */
function renderRecords() {
  const tbl = $("rectbl");
  tbl.innerHTML = "";
  const thead = document.createElement("thead");
  thead.innerHTML = "<tr><th>时间</th><th>交易对</th><th>方向</th><th class='num'>数量</th><th class='num'>价格</th><th class='num'>金额 (USDT)</th><th>备注</th><th>操作</th></tr>";
  tbl.appendChild(thead);
  const tb = document.createElement("tbody");
  for (const r of (S.records || [])) {
    const tr = document.createElement("tr");
    const td = (txt, cls) => { const c = document.createElement("td"); if (cls) c.className = cls; c.textContent = txt; return c; };
    tr.appendChild(td(fmtDate(r.ts, true)));
    tr.appendChild(td(r.instId));
    const sd = document.createElement("td");
    sd.className = r.side === "buy" ? "sidebuy" : "sidesell";
    sd.textContent = r.side === "buy" ? "买入" : "卖出";
    tr.appendChild(sd);
    tr.appendChild(td(fmtQty(r.qty), "num"));
    tr.appendChild(td(fmtQty(r.price), "num"));
    tr.appendChild(td(fmtMoney(r.amount), "num"));
    tr.appendChild(td(r.note || ""));
    const op = document.createElement("td");
    const be = document.createElement("button"); be.className = "ghost"; be.textContent = "编辑";
    be.onclick = () => openDlg(r);
    const bd = document.createElement("button"); bd.className = "ghost"; bd.textContent = "删除";
    bd.onclick = async () => {
      if (!confirm("删除这条记录？" + (r.source === "okx" ? "（同步记录会被隐藏，重新同步不会恢复；可在「恢复隐藏」后随下次同步回来）" : ""))) return;
      await api("/api/records/delete", {id: r.id});
      refresh();
    };
    op.appendChild(be); op.appendChild(document.createTextNode(" ")); op.appendChild(bd);
    tr.appendChild(op);
    tb.appendChild(tr);
  }
  tbl.appendChild(tb);
  const total = S.recordTotal || 0;
  const shown = (S.records || []).length;
  const bits = ["共 " + total + " 条，当前显示 " + shown + " 条"];
  if (S.hiddenCount > 0) {
    bits.push("已隐藏 " + S.hiddenCount + " 条");
  }
  $("recmeta").textContent = "（" + bits.join(" · ") + "）";
  const note = $("recnote");
  note.innerHTML = "";
  if (S.hiddenCount > 0) {
    const a = document.createElement("a");
    a.href = "#"; a.textContent = "恢复全部已隐藏的同步记录";
    a.onclick = async (e) => { e.preventDefault(); await api("/api/records/unhide", {}); refresh(); };
    note.appendChild(a);
  }
}

/* ---------- 弹窗（手动记录 / 编辑） ---------- */
function openDlg(rec) {
  editId = rec ? rec.id : null;
  $("dlg-title").textContent = rec ? "编辑记录" : "手动记一笔";
  const d = rec ? new Date(rec.ts) : new Date();
  const p = (x) => String(x).padStart(2, "0");
  $("f-ts").value = d.getFullYear() + "-" + p(d.getMonth()+1) + "-" + p(d.getDate()) + "T" + p(d.getHours()) + ":" + p(d.getMinutes());
  $("f-inst").value = rec ? rec.instId : "";
  $("f-inst").disabled = !!rec && rec.source === "okx";
  $("f-side").value = rec ? rec.side : "buy";
  $("f-qty").value = rec ? rec.qty : "";
  $("f-px").value = rec ? rec.price : "";
  $("f-note").value = rec ? (rec.note || "") : "";
  $("dlg").showModal();
}
$("btn-add").onclick = () => openDlg(null);
$("f-cancel").onclick = () => $("dlg").close();
$("f-save").onclick = async () => {
  const ts = new Date($("f-ts").value).getTime();
  const payload = {
    ts, instId: $("f-inst").value.trim(), side: $("f-side").value,
    qty: parseFloat($("f-qty").value), price: parseFloat($("f-px").value),
    note: $("f-note").value.trim(),
  };
  if (!payload.qty || !payload.price || !payload.instId) { alert("数量、价格、交易对都要填"); return; }
  try {
    if (editId) await api("/api/records/update", {id: editId, ...payload});
    else await api("/api/records/add", payload);
    $("dlg").close();
    refresh();
  } catch (e) { alert("保存失败: " + e.message); }
};

/* ---------- 时间范围 ---------- */
const RANGES = [["近30天", 30], ["近90天", 90], ["近1年", 365], ["全部", 0]];
function renderRange() {
  const seg = $("range-seg");
  seg.innerHTML = "";
  for (const [lab, d] of RANGES) {
    const b = document.createElement("button");
    b.textContent = lab;
    if (rangeDays === d) b.className = "on";
    b.onclick = () => { rangeDays = d; renderRange(); refresh(); };
    seg.appendChild(b);
  }
}

/* ---------- 顶部按钮 ---------- */
$("btn-sync").onclick = async () => {
  $("btn-sync").disabled = true;
  showBanner("同步中…");
  try {
    const r = await api("/api/sync", {});
    if (r.ok) { showBanner(r.info || "同步完成"); await refresh(); }
    else showBanner("同步失败: " + r.error, true);
  } catch (e) { showBanner("同步失败: " + e.message, true); }
  $("btn-sync").disabled = false;
};
$("btn-setup").onclick = () => { $("setup").hidden = !$("setup").hidden; };
$("btn-savecfg").onclick = async () => {
  $("cfgmsg").textContent = "测试中…";
  try {
    const r = await api("/api/config", {
      apiKey: $("c-key").value, secretKey: $("c-secret").value,
      passphrase: $("c-pass").value, flag: $("c-flag").value, proxy: $("c-proxy").value,
    });
    $("cfgmsg").textContent = r.msg;
    if (r.ok) { await refresh(); $("setup").hidden = true; }
  } catch (e) { $("cfgmsg").textContent = "失败: " + e.message; }
};
$("btn-demo").onclick = async () => {
  await api("/api/demo", {});
  $("cfgmsg").textContent = "";
  $("setup").hidden = true;
  await refresh();
};

/* ---------- 启动 ---------- */
renderRange();
refresh();
window.addEventListener("resize", () => { if (S) renderSeries(); });
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 启动


def main():
    port = PORT
    srv = None
    for p in range(PORT, PORT + 10):
        try:
            srv = ThreadingHTTPServer((HOST, p), Handler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        print("端口 %d-%d 都被占用了，退出。" % (PORT, PORT + 9))
        sys.exit(1)
    url = "http://%s:%d" % (HOST, port)
    print("OKX 定投记录工具")
    print("  地址: %s   (Ctrl+C 退出)" % url)
    print("  数据: %s" % DATA_PATH)
    print("  密钥: %s" % CONFIG_PATH)
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
