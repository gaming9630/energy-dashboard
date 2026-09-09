#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
דשבורד אנרגיה — קובץ אחד, שרת + איסוף + ממשק
==============================================
הרצה:
    python3 energy_dashboard.py --eia-key YOUR_KEY
    פתח:  http://localhost:8000

מפתח EIA חינמי: https://www.eia.gov/opendata/register.php
בלי pip install. פייתון 3.8+ בלבד.

מקורות:
  Yahoo Finance  — חוזי Brent / RBOB / ULSD / TTF  (ללא מפתח)
  Stooq          — גיבוי לחוזים                      (ללא מפתח)
  EIA v2         — קמעונאות, ספוט, מלאים, ייצוא      (מפתח חינמי)

בדיקה בלי רשת:   python3 energy_dashboard.py --selftest
"""

import argparse, http.cookiejar, json, os, re, sys, threading, time
from concurrent import futures
import urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "data.json")
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 10
MAX_WAIT = 3  # תקרת המתנה בין ניסיונות, בשניות

METRIC_IDS = [
    "crack321",
    "brent",
    "ttf",
    "gasoline",
    "diesel",
    "kerosene",
    "hormuzTankers",
    "shipsHit",
    "hormuzVolume",
    "usExports",
    "crudeStocks",
    "distillateStocks",
]

MANUAL_ONLY = ("hormuzTankers", "shipsHit", "hormuzVolume")

_log = []


def log(ok, src, msg):
    _log.append(
        {
            "ok": bool(ok),
            "src": src,
            "msg": str(msg),
            "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    print(("  ok   " if ok else " FAIL  ") + src + "  ->  " + str(msg), flush=True)


# ════════════════════════════════════════════════════════════ רשת
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))


def http_get(url, label, retries=1, quiet=False, referer=None):
    """GET עם עוגיות, backoff, ודיווח מפורש על כל כישלון."""
    last = None
    for attempt in range(retries + 1):
        headers = {
            "User-Agent": UA,
            "Accept": "text/csv,application/json,text/html,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "close",
        }
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(url, headers=headers)
        try:
            with _OPENER.open(req, timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            try:
                body = e.read().decode("utf-8", "replace")[:120].replace("\n", " ")
                if body.strip():
                    last += " | " + body
            except Exception:
                pass
            if e.code == 429:
                time.sleep(min(MAX_WAIT, 1 + attempt))
                continue
            if e.code in (401, 403, 404):
                break
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        if attempt < retries:
            time.sleep(min(MAX_WAIT, 0.8 * (attempt + 1)))
    if not quiet:
        log(False, label, last or "נכשל")
    return None


def warm_cookies(url, label):
    """פנייה מקדימה שרק אוספת עוגיות. כישלון כאן אינו קריטי."""
    http_get(url, label, retries=0, quiet=True)


def is_reachable(url):
    """בודק חיבור בלבד: גם שגיאת HTTP (403/404/...) נחשבת 'מגיב', כי השרת ענה.
    רק כישלון חיבור (טיימאאוט, DNS, סירוב) נחשב 'לא מגיב'."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with _OPENER.open(req, timeout=TIMEOUT):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


# ════════════════════════════════════════════════════════════ חישובים
def pct(now, then):
    if now is None or then in (None, 0):
        return None
    return round((now - then) / abs(then) * 100.0, 1)


def pick_back(points, days, tol=14):
    """הנקודה הקרובה ביותר ל-N ימים לפני התצפית האחרונה."""
    if not points:
        return None
    last = datetime.strptime(points[-1]["date"], "%Y-%m-%d")
    target = last - timedelta(days=days)
    best, diff = None, None
    for p in points:
        d = abs((datetime.strptime(p["date"], "%Y-%m-%d") - target).days)
        if diff is None or d < diff:
            best, diff = p, d
    return best if diff is not None and diff <= tol else None


def entry(points, source, digits=2):
    if not points:
        return None
    last = points[-1]
    w, y = pick_back(points, 7), pick_back(points, 365)
    return {
        "value": round(last["close"], digits),
        "asOf": last["date"],
        "source": source,
        "weeklyPct": pct(last["close"], w["close"]) if w else None,
        "yearlyPct": pct(last["close"], y["close"]) if y else None,
    }


def crack_321(gasoline_gal, distillate_gal, crude_bbl):
    """מרווח 3-2-1 ב-$/חבית. מחירי מוצרים ב-$/גלון, נפט ב-$/חבית."""
    return (2.0 * gasoline_gal + distillate_gal) * 42.0 / 3.0 - crude_bbl


def align(*series):
    """מחזיר רשימת תאריכים שמשותפים לכל הסדרות, ומילון תאריך->ערך לכל אחת."""
    maps = [{p["date"]: p["close"] for p in s} for s in series]
    common = set(maps[0])
    for m in maps[1:]:
        common &= set(m)
    return sorted(common), maps


def crack_series(gas_pts, dist_pts, crude_pts):
    """בונה סדרת מרווח יומית מהתאריכים המשותפים לשלוש הסדרות."""
    dates, maps = align(gas_pts, dist_pts, crude_pts)
    g, d, c = maps
    return [{"date": dt, "close": crack_321(g[dt], d[dt], c[dt])} for dt in dates]


# ════════════════════════════════════════════════════════════ Yahoo Finance
# הנקודה הזו מחזירה 429 בלי עוגייה ו-crumb. לכן: חימום -> crumb -> נתונים.
YAHOO = {
    "brent": ("BZ=F", "Brent front-month"),
    "rbob": ("RB=F", "RBOB gasoline"),
    "ulsd": ("HO=F", "ULSD / heating oil"),
    "ttf": ("TTF=F", "Dutch TTF gas"),
}
_CRUMB = None
_YAHOO_READY = False
_CONTRACT_MONTH = {}  # symbol -> "Oct 26" וכד', רק כשיאהו חושף חודש מפורש (לא Brent/TTF)


def yahoo_auth():
    """אוסף עוגיות ומביא crumb. מספיק פעם אחת לכל ריצה."""
    global _CRUMB, _YAHOO_READY
    if _YAHOO_READY:
        return _CRUMB
    _YAHOO_READY = True
    for host in ("https://finance.yahoo.com/", "https://fc.yahoo.com/"):
        warm_cookies(host, "Yahoo warmup")
        if len(_JAR):
            break
    if not len(_JAR):
        log(False, "Yahoo auth", "לא התקבלו עוגיות")
        return None
    c = http_get(
        "https://query2.finance.yahoo.com/v1/test/getcrumb",
        "Yahoo crumb",
        retries=1,
        quiet=True,
        referer="https://finance.yahoo.com/",
    )
    if c and 0 < len(c.strip()) <= 32 and "<" not in c:
        _CRUMB = c.strip()
        log(True, "Yahoo auth", "crumb התקבל (%d עוגיות)" % len(_JAR))
    else:
        log(True, "Yahoo auth", "%d עוגיות, בלי crumb — ננסה בלעדיו" % len(_JAR))
    return _CRUMB


def yahoo(symbol, label):
    crumb = yahoo_auth()
    base = "/v8/finance/chart/" + urllib.parse.quote(symbol) + "?range=1y&interval=1d"
    if crumb:
        base += "&crumb=" + urllib.parse.quote(crumb)
    txt = None
    for host in (
        "https://query2.finance.yahoo.com",
        "https://query1.finance.yahoo.com",
    ):
        txt = http_get(
            host + base,
            "Yahoo " + label,
            retries=1,
            quiet=True,
            referer="https://finance.yahoo.com/quote/" + urllib.parse.quote(symbol),
        )
        if txt:
            break
    if not txt:
        log(False, "Yahoo " + label, "שתי הנקודות נכשלו (429/חסימה)")
        return []
    try:
        j = json.loads(txt)
        res = j["chart"]["result"][0]
        ts, cl = res["timestamp"], res["indicators"]["quote"][0]["close"]
    except Exception as e:
        log(False, "Yahoo " + label, "מבנה לא צפוי: %s | %s" % (e, txt[:90]))
        return []
    meta_name = (res.get("meta") or {}).get("shortName") or ""
    m = re.search(r"([A-Z][a-z]{2} \d{2})\s*$", meta_name.strip())
    if m:
        _CONTRACT_MONTH[symbol] = m.group(1)
    pts = [
        {"date": datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"), "close": float(c)}
        for t, c in zip(ts, cl)
        if c is not None
    ]
    pts.sort(key=lambda p: p["date"])
    if pts:
        log(
            True,
            "Yahoo " + label,
            "%.4f @ %s (%d ימים)" % (pts[-1]["close"], pts[-1]["date"], len(pts)),
        )
    else:
        log(False, "Yahoo " + label, "אין נקודות בתשובה")
    return pts


# ════════════════════════════════════════════════════════════ Stooq (גיבוי)
STOOQ = {"brent": "cb.f", "rbob": "rb.f", "ulsd": "ho.f", "ttf": "ttf.f"}


def _stooq_rows(txt):
    pts = []
    for line in txt.strip().splitlines()[1:]:
        c = line.split(",")
        if len(c) >= 5 and len(c[0]) == 10 and c[0][4] == "-":
            try:
                pts.append({"date": c[0], "close": float(c[4])})
            except ValueError:
                pass
    pts.sort(key=lambda p: p["date"])
    return pts


def _is_block_page(txt):
    head = txt.lstrip()[:200].lower()
    return head.startswith("<!doctype") or "<html" in head or "robots" in head


def stooq(symbol, label):
    """היסטוריה יומית. אם Stooq מחזיר דף חסימה, נופלים לציטוט אחרון בלבד."""
    d2 = date.today().strftime("%Y%m%d")
    d1 = (date.today() - timedelta(days=400)).strftime("%Y%m%d")
    hist = [
        "https://stooq.com/q/d/l/?s=%s&d1=%s&d2=%s&i=d" % (symbol, d1, d2),
        "https://stooq.com/q/d/l/?s=%s&i=d" % symbol,
        "https://stooq.pl/q/d/l/?s=%s&i=d" % symbol,
    ]
    blocked = False
    for url in hist:
        txt = http_get(
            url,
            "Stooq " + label,
            retries=0,
            quiet=True,
            referer="https://stooq.com/q/?s=" + symbol,
        )
        if not txt:
            continue
        if _is_block_page(txt):
            blocked = True
            continue
        pts = _stooq_rows(txt)
        if pts:
            log(
                True,
                "Stooq " + label,
                "%.4f @ %s (%d ימים)" % (pts[-1]["close"], pts[-1]["date"], len(pts)),
            )
            return pts

    # מסלול אחרון: ציטוט יחיד. אין היסטוריה, ולכן אין שינוי שבועי/שנתי — אבל יש מחיר.
    for host in ("https://stooq.com", "https://stooq.pl"):
        txt = http_get(
            host + "/q/l/?s=%s&f=sd2t2ohlcv&h&e=csv" % symbol,
            "Stooq " + label,
            retries=0,
            quiet=True,
            referer="https://stooq.com/q/?s=" + symbol,
        )
        if txt and not _is_block_page(txt) and "," in txt:
            rows = txt.strip().splitlines()
            if len(rows) >= 2:
                c = rows[1].split(",")
                try:
                    d, close = c[1], float(c[6])
                    if len(d) == 10 and close > 0:
                        log(
                            True,
                            "Stooq " + label,
                            "%.4f @ %s (ציטוט בלבד, בלי היסטוריה)" % (close, d),
                        )
                        return [{"date": d, "close": close}]
                except (IndexError, ValueError):
                    pass

    log(False, "Stooq " + label, "חסימת בוטים (דף HTML)" if blocked else "אין תשובה")
    return []


# ════════════════════════════════════════════════════════════ TradingEconomics (גיבוי אחרון ל-TTF)
# אין ל-TTF (גז הולנדי) שום סדרה חינמית רשמית (לא EIA, לא Yahoo/Stooq כשחסומים).
# הדף הציבורי הזה חושף בתגית meta description ערך יומי + שינוי שנתי, בלי היסטוריה מלאה —
# לכן אין שינוי שבועי, וזו סריקה לא-רשמית שעלולה להישבר אם הניסוח באתר ישתנה.
def te_ttf():
    txt = http_get(
        "https://tradingeconomics.com/commodity/eu-natural-gas",
        "TradingEconomics TTF",
        retries=1,
        quiet=True,
    )
    if not txt:
        log(False, "TradingEconomics TTF", "אין תשובה")
        return None
    m = re.search(r'name="description"\s+content="([^"]+)"', txt)
    desc = m.group(1) if m else ""
    mv = re.search(r"([\d][\d,.]*)\s*EUR/MWh on ([A-Za-z]+ \d{1,2}, \d{4})", desc)
    if not mv:
        log(False, "TradingEconomics TTF", "מבנה לא צפוי בדף")
        return None
    try:
        value = float(mv.group(1).replace(",", ""))
        d = datetime.strptime(mv.group(2), "%B %d, %Y").strftime("%Y-%m-%d")
    except ValueError as e:
        log(False, "TradingEconomics TTF", "פירוק תאריך/ערך נכשל: %s" % e)
        return None
    yearly = None
    my = re.search(r"(up|down) ([\d.]+)% compared to the same time last year", desc)
    if my:
        yearly = float(my.group(2)) * (-1 if my.group(1) == "down" else 1)
    log(
        True,
        "TradingEconomics TTF",
        "%.2f EUR/MWh @ %s (סריקה, בלי שינוי שבועי)" % (value, d),
    )
    return {"value": round(value, 2), "asOf": d, "yearlyPct": yearly}


# ════════════════════════════════════════════════════════════ EIA v2
# משתמשים ב-/v2/seriesid/ שפותר מזהה סדרה ישן אוטומטית —
# בלי לנחש route ו-facet, שזו נקודת השבירה הנפוצה ב-API הזה.
EIA_IDS = {
    "brentSpot": ("PET.RBRTE.D", "Brent ספוט"),
    "gasRetail": ("PET.EMM_EPMR_PTE_NUS_DPG.W", "בנזין קמעונאי"),
    "dieselRetail": ("PET.EMD_EPD2D_PTE_NUS_DPG.W", "דיזל קמעונאי"),
    "jetSpot": ("PET.EER_EPJK_PF4_RGC_DPG.D", "קרוסין ספוט USGC"),
    "gasSpotUSGC": ("PET.EER_EPMRU_PF4_RGC_DPG.D", "בנזין ספוט USGC"),
    "ulsdSpotUSGC": ("PET.EER_EPD2DXL0_PF4_RGC_DPG.D", "ULSD ספוט USGC"),
    "exports": ("PET.WTTEXUS2.W", "ייצוא נפט ומוצרים"),
    "crudeStockLevel": ("PET.WCESTUS1.W", "מלאי גולמי"),
    "distStockLevel": ("PET.WDISTUS1.W", "מלאי תזקיקים"),
}


def eia(key, name, scale=1.0):
    sid, label = EIA_IDS[name]
    q = urllib.parse.urlencode(
        {
            "api_key": key,
            "length": "500",
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
        }
    )
    txt = http_get("https://api.eia.gov/v2/seriesid/%s?%s" % (sid, q), "EIA " + label)
    if not txt:
        return []
    try:
        j = json.loads(txt)
    except Exception as e:
        log(False, "EIA " + label, "JSON שבור: %s" % e)
        return []
    if isinstance(j, dict) and j.get("error"):
        log(False, "EIA " + label, j["error"])
        return []
    rows = (j.get("response") or {}).get("data") or []
    if not rows:
        log(False, "EIA " + label, "אין נתונים למזהה " + sid)
        return []
    pts = []
    for r in rows:
        per = r.get("period")
        val = r.get("value")
        if val is None:
            for k, v in r.items():
                if k not in ("period", "series", "series-description") and isinstance(
                    v, (int, float)
                ):
                    val = v
                    break
        try:
            pts.append({"date": str(per)[:10], "close": float(val) * scale})
        except (TypeError, ValueError):
            pass
    pts.sort(key=lambda p: p["date"])
    if pts:
        log(
            True,
            "EIA " + label,
            "%.4f @ %s (%d נקודות)" % (pts[-1]["close"], pts[-1]["date"], len(pts)),
        )
    return pts


# ════════════════════════════════════════════════════════════ איסוף
def blank():
    return {
        "value": None,
        "asOf": None,
        "weeklyPct": None,
        "yearlyPct": None,
        "source": None,
    }


def collect(eia_key):
    global _log
    _log = []
    out = {k: blank() for k in METRIC_IDS}
    t0 = time.time()

    # ---------- 1. חוזים: Yahoo, עם Stooq כגיבוי (במקביל) ----------
    yahoo_auth()  # לחיצת יד אחת, לפני הפיצול לת'רדים

    def one_future(key):
        sym, label = YAHOO[key]
        pts = yahoo(sym, label)
        if not pts:
            pts = stooq(STOOQ[key], key)
        return key, pts

    fut = {}
    with futures.ThreadPoolExecutor(max_workers=4) as ex:
        for key, pts in ex.map(one_future, list(YAHOO)):
            fut[key] = pts

    e = entry(fut.get("brent"), "חוזה Brent קרוב", 2)
    if e:
        out["brent"] = e

    e = entry(fut.get("ttf"), "חוזה TTF קרוב", 2)
    if e:
        out["ttf"] = e
    else:
        te = te_ttf()
        if te:
            out["ttf"] = {
                "value": te["value"],
                "asOf": te["asOf"],
                "weeklyPct": None,
                "yearlyPct": te["yearlyPct"],
                "source": "TradingEconomics · סריקה (בלי שינוי שבועי)",
            }
        else:
            log(
                False,
                "TTF",
                "כשל בכל שלושת המקורות (Yahoo, Stooq, TradingEconomics) — נדרשת הזנה ידנית",
            )

    # ---------- 2. EIA ----------
    if not eia_key:
        log(False, "EIA", "לא הוזן מפתח (--eia-key) — 6 אריחים יישארו ריקים")
    else:
        specs = [
            ("gasoline", "gasRetail", 1.0, 3, 'EIA · ממוצע קמעונאי ארה"ב'),
            ("diesel", "dieselRetail", 1.0, 3, 'EIA · ממוצע קמעונאי ארה"ב'),
            ("kerosene", "jetSpot", 1.0, 3, "EIA · ספוט מפרץ מקסיקו"),
            ("usExports", "exports", 0.001, 2, "EIA · שבועי"),
        ]
        wanted = [(t, n, sc) for t, n, sc, _, _ in specs]
        wanted += [
            ("_crude", "crudeStockLevel", 0.001),
            ("_dist", "distStockLevel", 0.001),
            ("_gasSpot", "gasSpotUSGC", 1.0),
            ("_ulsdSpot", "ulsdSpotUSGC", 1.0),
            ("_brentSpot", "brentSpot", 1.0),
        ]
        got = {}
        with futures.ThreadPoolExecutor(max_workers=5) as ex:
            jobs = {ex.submit(eia, eia_key, n, sc): t for t, n, sc in wanted}
            for job in futures.as_completed(jobs):
                got[jobs[job]] = job.result()

        for target, name, scale, digits, src in specs:
            e = entry(got.get(target) or [], src, digits)
            if e:
                out[target] = e

        for target, cache in (("crudeStocks", "_crude"), ("distillateStocks", "_dist")):
            pts = got.get(cache) or []
            if len(pts) >= 2:
                change = pts[-1]["close"] - pts[-2]["close"]
                out[target] = {
                    "value": round(change, 2),
                    "asOf": pts[-1]["date"],
                    "weeklyPct": None,
                    "yearlyPct": None,
                    "source": 'EIA · שינוי שבועי (רמה %.1f מלמ"ח)' % pts[-1]["close"],
                }
                log(True, target, "%+.2f מיליון חביות" % change)

        # אם שני מקורות החוזים נפלו, EIA עדיין נותן Brent (ספוט, בפיגור יום-יומיים)
        if out["brent"]["value"] is None:
            e = entry(got.get("_brentSpot") or [], "EIA · Brent ספוט", 2)
            if e:
                out["brent"] = e
                log(True, "גיבוי", "Brent נלקח מ-EIA ספוט")

        # ---------- 3. מרווח זיקוק ----------
        # עדיפות: הגדרת EIA — ספוט USGC מול Brent ספוט.
        g = got.get("_gasSpot") or []
        d = got.get("_ulsdSpot") or []
        c = got.get("_brentSpot") or []
        if g and d and c:
            ser = crack_series(g, d, c)
            e = entry(ser, "מחושב · ספוט USGC מול Brent (EIA)", 1)
            if e:
                out["crack321"] = e
                log(True, "מרווח זיקוק", "%.1f $/חבית @ %s" % (e["value"], e["asOf"]))

    # גיבוי למרווח: חוזי RBOB/ULSD/Brent
    if out["crack321"]["value"] is None and all(
        fut.get(k) for k in ("rbob", "ulsd", "brent")
    ):
        months = {
            _CONTRACT_MONTH[YAHOO[k][0]]
            for k in ("rbob", "ulsd")
            if YAHOO[k][0] in _CONTRACT_MONTH
        }
        month_tag = " (%s)" % " / ".join(sorted(months)) if months else ""
        ser = crack_series(fut["rbob"], fut["ulsd"], fut["brent"])
        e = entry(ser, "מחושב · חוזי RBOB/ULSD מול Brent" + month_tag, 1)
        if e:
            out["crack321"] = e
            log(True, "מרווח זיקוק", "%.1f $/חבית (מחוזים)" % e["value"])

    for k in MANUAL_ONLY:
        if out[k]["value"] is None:
            out[k]["source"] = "דורש הזנה ידנית"

    filled = sum(1 for k in METRIC_IDS if out[k]["value"] is not None)
    return {
        "metrics": out,
        "log": _log,
        "filled": filled,
        "total": len(METRIC_IDS),
        "elapsed": round(time.time() - t0, 1),
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def load_saved():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save(payload):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def merge_previous(new):
    """שומר ערכים קיימים (בעיקר ידניים) עבור אריחים שהמשיכה החזירה ריקים."""
    old = load_saved()
    if not old:
        return new
    kept = 0
    for k, v in (old.get("metrics") or {}).items():
        if (
            k in new["metrics"]
            and new["metrics"][k]["value"] is None
            and v.get("value") is not None
        ):
            new["metrics"][k] = v
            kept += 1
    if kept:
        log(True, "מיזוג", "%d ערכים קודמים נשמרו" % kept)
        new["log"] = _log
    new["filled"] = sum(1 for k in METRIC_IDS if new["metrics"][k]["value"] is not None)
    return new


def refresh(eia_key):
    print("\n" + "=" * 62, flush=True)
    print("משיכת נתונים  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"), flush=True)
    print("=" * 62, flush=True)
    payload = merge_previous(collect(eia_key))
    save(payload)
    print("-" * 62, flush=True)
    print(
        "%d/%d אריחים מלאים  (%.1fs)"
        % (payload["filled"], payload["total"], payload["elapsed"]),
        flush=True,
    )
    return payload


# ════════════════════════════════════════════════════════════ הממשק
PAGE = r"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>דשבורד אנרגיה</title>
<link href="https://fonts.googleapis.com/css2?family=Assistant:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  :root{--navy:#12293d;--navy-2:#1c3a53;--rule:#a9b8c6;--ink:#12283a;--ink-soft:#5c6f80;
        --box:#fdf7e6;--box-line:#8d9aa6;--up:#6cae4a;--down:#e0855c;--flat:#9aa7b2;--mark:#fdf07f}
  *{box-sizing:border-box} html,body{margin:0;padding:0}
  body{background:var(--navy);background-image:radial-gradient(circle at 20% 0%,#1d4160 0%,#12293d 55%,#0c1c2b 100%);
    font-family:"Assistant","Segoe UI","Heebo",Arial,sans-serif;color:var(--ink);min-height:100vh;padding:14px}
  .shell{max-width:1180px;margin:0 auto}
  .bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;margin-bottom:10px}
  .stamp{font-size:13px;color:#a9c0d4;display:flex;gap:9px;align-items:center;flex-wrap:wrap}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--up);display:inline-block}
  .dot.stale{background:#e0b05c}.dot.err{background:#e0855c}
  .actions{display:flex;gap:8px;flex-wrap:wrap}
  button{font-family:inherit;font-size:14px;font-weight:700;border:1px solid #3a5f80;background:#1c3a53;
    color:#e8f1f8;padding:8px 14px;border-radius:8px;cursor:pointer}
  button:hover{background:#26506f} button:disabled{opacity:.5;cursor:default}
  button.primary{background:#2f7ab5;border-color:#4c9bd8} button.primary:hover{background:#3a8ccb}
  button:focus-visible{outline:2px solid #8fd0ff;outline-offset:2px}
  .panel{background:#fff;border:3px solid var(--navy-2);border-radius:4px;padding:0 0 6px;box-shadow:0 18px 40px rgba(0,0,0,.35)}
  h1{margin:0;padding:16px 20px 12px;font-size:clamp(26px,4.2vw,40px);font-weight:800;letter-spacing:-.5px;text-align:center}
  .grid{display:grid;grid-template-columns:repeat(3,1fr)}
  .cell{border-top:2px solid var(--rule);border-left:1px solid var(--rule);padding:14px 12px 16px;
    display:flex;flex-direction:column;align-items:center;text-align:center;min-height:192px}
  .cell:nth-child(3n){border-left:none}
  .cell h2{margin:0;font-size:clamp(16px,2.1vw,21px);font-weight:800;line-height:1.2}
  .sub{margin-top:3px;font-size:12.5px;color:var(--ink-soft);line-height:1.35}
  .asof{margin-top:3px;font-size:12px;color:var(--ink-soft)}
  .asof b{background:var(--mark);padding:0 4px;border-radius:3px;font-weight:700;color:#3a3000}
  .row{display:flex;align-items:center;justify-content:center;gap:6px;margin-top:auto;padding-top:10px;width:100%}
  .valbox{border:2px solid var(--box-line);background:var(--box);border-radius:10px;min-width:104px;padding:8px 10px;
    font-size:clamp(21px,3.2vw,30px);font-weight:800;display:flex;align-items:center;justify-content:center;
    direction:ltr;white-space:nowrap}
  .valbox.empty{color:#b7bfc6;font-size:19px}
  .valbox.good{background:#cfe6b8;border-color:#7ba055}
  .valbox.warn{background:#f4cdb6;border-color:#c98b64}
  .valbox input{width:100%;border:none;background:transparent;font:inherit;text-align:center;direction:ltr;color:inherit;padding:0}
  .arrow{display:flex;flex-direction:column;align-items:center;gap:2px;min-width:62px}
  .arrow svg{width:26px;height:44px}
  .arrow .pct{font-size:14.5px;font-weight:800;direction:ltr}
  .arrow .lbl{font-size:13px;font-weight:600}
  .arrow.na svg{opacity:.22}.arrow.na .pct{color:var(--flat)}
  .src{margin-top:8px;font-size:11.5px;color:#8595a3;min-height:14px}
  .foot{margin-top:10px;color:#9db4c8;font-size:12.5px;display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap}
  .note{margin:0 0 10px;padding:9px 12px;border-radius:8px;background:#243f57;border:1px solid #3d6a85;color:#d8e8f4;font-size:13px}
  .note.err{background:#4a2a25;border-color:#8a5040;color:#f6ddd3}
  .hide{display:none}
  .diag{margin-top:10px;background:#0d1e2c;border:1px solid #2c4a63;border-radius:8px;padding:10px 12px;color:#c3d6e6}
  .diag h3{margin:0 0 6px;font-size:13px;color:#8fd0ff}
  .diag ul{margin:0;padding-inline-start:18px}
  .diag li{margin:3px 0;direction:ltr;text-align:left;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11.5px;word-break:break-word}
  .okc{color:#84d05f}.badc{color:#ef9a76}
  @media (max-width:820px){.grid{grid-template-columns:repeat(2,1fr)}.cell:nth-child(3n){border-left:1px solid var(--rule)}.cell:nth-child(2n){border-left:none}}
  @media (max-width:540px){body{padding:8px}.grid{grid-template-columns:1fr}.cell{border-left:none;min-height:0}.arrow{min-width:56px}}
</style>
</head>
<body>
<div class="shell">
  <div class="bar">
    <div class="stamp"><span id="dot" class="dot stale"></span><span id="status">טוען…</span></div>
    <div class="actions">
      <button id="diagBtn">אבחון</button>
      <button id="editBtn">עריכה ידנית</button>
      <button id="refreshBtn" class="primary">רענן נתונים</button>
    </div>
  </div>
  <div id="note" class="note hide"></div>
  <div class="panel">
    <h1>דשבורד אנרגיה</h1>
    <div class="grid" id="grid"></div>
  </div>
  <div class="foot">
    <div>Yahoo Finance (חוזים) · EIA (קמעונאות, ספוט, מלאים, ייצוא)</div>
    <div id="updatedAt">—</div>
  </div>
  <div id="diag" class="diag hide"><h3>אבחון מקורות</h3><ul id="diagList"><li>—</li></ul></div>
</div>
<script>
const METRICS=[
 {id:'crack321',        t:'מרווח זיקוק (3-2-1)',           s:'$ לחבית',                     f:v=>'$ '+v.toFixed(1)},
 {id:'brent',           t:'מחיר נפט (Brent)',               s:'חוזה קרוב',                   f:v=>'$ '+v.toFixed(2)},
 {id:'ttf',             t:'מחיר גז אירופאי (TTF)',          s:'חוזה קרוב',                   f:v=>'€ '+v.toFixed(2)},
 {id:'gasoline',        t:'מחיר בנזין',                     s:'(ממוצע ארה״ב)',               f:v=>'$ '+v.toFixed(2)},
 {id:'diesel',          t:'מחיר דיזל',                      s:'(ממוצע ארה״ב)',               f:v=>'$ '+v.toFixed(2)},
 {id:'kerosene',        t:'מחיר קרוסין',                    s:'(מפרץ מקסיקו)',               f:v=>'$ '+v.toFixed(2)},
 {id:'hormuzTankers',   t:'מס׳ מיכליות שעברו בהורמוז',      s:'',        week:true, noPct:true, f:v=>String(Math.round(v))},
 {id:'shipsHit',        t:'מס׳ ספינות שנפגעו',              s:'',        week:true, noPct:true, f:v=>String(Math.round(v))},
 {id:'hormuzVolume',    t:'מח״י נפט ומוצריו שיצאו בהורמוז', s:'',        week:true, noPct:true, f:v=>v.toFixed(1)},
 {id:'usExports',       t:'ייצוא נפט אמריקני',              s:'(גולמי ומוצרים, מח״י, ארה״ב)', tone:'good', f:v=>v.toFixed(2)},
 {id:'crudeStocks',     t:'מאגרי נפט גולמי',                s:'(מיליוני חביות, ארה״ב)',      noPct:true, tone:'sign', f:v=>(v>0?'+':'')+v.toFixed(2)},
 {id:'distillateStocks',t:'מאגרי תזקיקים',                  s:'(מיליוני חביות, ארה״ב)',      noPct:true, tone:'sign', f:v=>(v>0?'+':'')+v.toFixed(2)}];

/* גוון תיבת הערך בשורה התחתונה, כמו במקור */
function tone(m,d){
 if(!m.tone||d.value===null||d.value===undefined) return '';
 if(m.tone==='good') return 'good';
 return Number(d.value)<0?'good':'warn';   // משיכה ממלאי = ירוק, צבירה = כתום
}

const blank=()=>({value:null,asOf:null,weeklyPct:null,yearlyPct:null,source:null});
let DATA={}; METRICS.forEach(m=>DATA[m.id]=blank());
let editMode=false, diagLines=[];
const $=i=>document.getElementById(i), grid=$('grid');

function arrowSVG(d){
 if(d==='up')   return '<svg viewBox="0 0 26 44"><path d="M13 2 25 20h-7v22H8V20H1z" fill="var(--up)" stroke="#4a7d32" stroke-width="1.2" stroke-linejoin="round"/></svg>';
 if(d==='down') return '<svg viewBox="0 0 26 44"><path d="M13 42 1 24h7V2h10v22h7z" fill="var(--down)" stroke="#b3603c" stroke-width="1.2" stroke-linejoin="round"/></svg>';
 return '<svg viewBox="0 0 26 44"><rect x="3" y="20" width="20" height="5" rx="2" fill="var(--flat)"/></svg>';}
function pctBlock(p,l){
 if(p===null||p===undefined||isNaN(p))
  return '<div class="arrow na">'+arrowSVG('flat')+'<div class="pct">—</div><div class="lbl">'+l+'</div></div>';
 const d=p>0.05?'up':(p<-0.05?'down':'flat');
 const c=d==='up'?'var(--up)':d==='down'?'var(--down)':'var(--flat)';
 return '<div class="arrow">'+arrowSVG(d)+'<div class="pct" style="color:'+c+'">'+(p>0?'+':'')+p.toFixed(1)+'%</div><div class="lbl">'+l+'</div></div>';}
const weekRange=i=>{const e=new Date(i);if(isNaN(e))return heDate(i);
 const s=new Date(e.getTime()-6*864e5);
 return s.getDate()+'-'+e.getDate()+'.'+(e.getMonth()+1);};
const heDate=i=>{const p=String(i||'').split('-');return p.length===3?(+p[2])+'/'+(+p[1]):(i||'');};
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function render(){
 grid.innerHTML='';
 for(const m of METRICS){
  const d=DATA[m.id]||blank();
  const has=d.value!==null&&d.value!==undefined&&!isNaN(d.value);
  const c=document.createElement('div'); c.className='cell';
  const inner=editMode
   ?'<input type="number" step="any" value="'+(has?d.value:'')+'" data-id="'+m.id+'" aria-label="'+esc(m.t)+'">'
   :(has?m.f(Number(d.value)):'אין נתון');
  const stamp = !d.asOf ? '<div class="asof">&nbsp;</div>'
   : m.week ? '<div class="asof">שבוע <b>'+weekRange(d.asOf)+'</b></div>'
            : '<div class="asof">נכון לתאריך <b>'+heDate(d.asOf)+'</b></div>';
  c.innerHTML='<h2>'+m.t+'</h2><div class="sub">'+(m.s||'&nbsp;')+'</div>'+stamp+
   '<div class="row">'+(m.noPct?'':pctBlock(d.yearlyPct,'שנתי'))+
   '<div class="valbox '+tone(m,d)+' '+((has||editMode)?'':'empty')+'">'+inner+'</div>'+
   (m.noPct?'':pctBlock(d.weeklyPct,'שבועי'))+'</div>'+
   '<div class="src">'+(d.source?'מקור: '+esc(d.source):'')+'</div>';
  grid.appendChild(c);}
 if(editMode) grid.querySelectorAll('input[data-id]').forEach(inp=>{
  inp.addEventListener('change',async e=>{
   const id=e.target.dataset.id, v=e.target.value===''?null:Number(e.target.value);
   DATA[id]=Object.assign({},DATA[id],{value:v,source:'הזנה ידנית',asOf:new Date().toISOString().slice(0,10)});
   try{ await fetch('/api/manual',{method:'POST',headers:{'Content-Type':'application/json'},
     body:JSON.stringify({[id]:v})}); note('נשמר.',false); }
   catch(err){ note('השמירה לשרת נכשלה: '+err.message,true); }});});}

function setStatus(k,t){$('dot').className='dot'+(k==='ok'?'':k==='err'?' err':' stale');$('status').textContent=t;}
function note(m,e){const n=$('note');n.textContent=m;n.className='note'+(e?' err':'');}
function hideNote(){$('note').className='note hide';}
function localTime(t){const d=t?new Date(t):null;return d&&!isNaN(d)?d.toLocaleTimeString('he-IL'):(t||'');}
function paintDiag(){$('diagList').innerHTML=diagLines.length
 ?diagLines.map(l=>'<li class="'+(l.ok?'okc':'badc')+'">'+esc(localTime(l.t)+' '+l.src+' -> '+l.msg)+'</li>').join('')
 :'<li>אין רשומות.</li>';}

function apply(p){
 if(!p||!p.metrics) return 0;
 let n=0;
 for(const m of METRICS){const v=p.metrics[m.id];
  DATA[m.id]=v||blank();
  if(v&&v.value!==null&&v.value!==undefined) n++;}
 diagLines=p.log||[];
 if(p.updatedAt) $('updatedAt').textContent='עודכן: '+new Date(p.updatedAt).toLocaleString('he-IL');
 return n;}

async function load(path,label){
 const b=$('refreshBtn'); b.disabled=true; b.textContent='מרענן…'; hideNote(); setStatus('stale',label);
 try{
  const r=await fetch(path,{cache:'no-store'});
  if(!r.ok) throw new Error('HTTP '+r.status);
  const p=await r.json();
  const n=apply(p); render(); paintDiag();
  if(n===METRICS.length) setStatus('ok','נתונים חיים · '+n+'/'+METRICS.length);
  else{setStatus('stale',n+'/'+METRICS.length+' אריחים מלאים');
       note((METRICS.length-n)+' אריחים ריקים. פתח "אבחון" לסיבה, או מלא ב"עריכה ידנית".',false);}
 }catch(e){
  render(); paintDiag(); setStatus('err','השרת לא הגיב');
  note('אין תשובה מהשרת ('+e.message+'). ודא שהסקריפט רץ ושפתחת את הכתובת שהוא הדפיס.',true);}
 b.disabled=false; b.textContent='רענן נתונים';}

$('refreshBtn').addEventListener('click',()=>load('/api/refresh','מושך מהמקורות…'));
$('diagBtn').addEventListener('click',()=>{const d=$('diag');
 d.className=d.className.includes('hide')?'diag':'diag hide'; paintDiag();});
$('editBtn').addEventListener('click',e=>{editMode=!editMode;
 e.target.textContent=editMode?'סיים עריכה':'עריכה ידנית';
 e.target.classList.toggle('primary',editMode); render();});

render();
load('/api/data','טוען…');
setInterval(()=>{if(!editMode) load('/api/data','מסנכרן…');}, 5*60*1000);
</script>
</body>
</html>
"""


# ════════════════════════════════════════════════════════════ שרת
class Handler(BaseHTTPRequestHandler):
    eia_key = ""
    server_version = "EnergyDash/1.0"

    def _send(self, body, ctype, code=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, obj, code=200):
        self._send(
            json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", code
        )

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(PAGE, "text/html; charset=utf-8")
        if path == "/api/data":
            saved = load_saved()
            if saved is None:
                saved = refresh(self.eia_key)
            return self._json(saved)
        if path == "/api/refresh":
            try:
                return self._json(refresh(self.eia_key))
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        self._send("404", "text/plain; charset=utf-8", 404)

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/manual":
            return self._send("404", "text/plain; charset=utf-8", 404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            payload = load_saved() or {"metrics": {k: blank() for k in METRIC_IDS}}
            for k, v in body.items():
                if k in payload["metrics"]:
                    payload["metrics"][k] = {
                        "value": v,
                        "asOf": date.today().isoformat(),
                        "weeklyPct": None,
                        "yearlyPct": None,
                        "source": "הזנה ידנית",
                    }
            payload["filled"] = sum(
                1 for k in METRIC_IDS if payload["metrics"][k]["value"] is not None
            )
            save(payload)
            return self._json({"ok": True, "filled": payload["filled"]})
        except Exception as e:
            return self._json({"error": str(e)}, 400)

    def log_message(self, *a):
        pass


def auto_loop(eia_key, minutes):
    while True:
        time.sleep(max(5, minutes) * 60)
        try:
            refresh(eia_key)
        except Exception as e:
            print("רענון אוטומטי נכשל:", e, flush=True)


def next_run_at(hhmm):
    """התאריך/שעה הבאים שבהם hh:mm (שעון מקומי) מתרחש, החל מעכשיו."""
    hh, mm = (int(x) for x in hhmm.split(":"))
    now = datetime.now()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def daily_loop(eia_key, hhmm):
    """מרענן פעם ביום, בשעה קבועה — אחרי שכל השווקים הרלוונטיים כבר נסגרו."""
    while True:
        target = next_run_at(hhmm)
        time.sleep(max(1, (target - datetime.now()).total_seconds()))
        try:
            refresh(eia_key)
        except Exception as e:
            print("רענון יומי נכשל:", e, flush=True)


# ════════════════════════════════════════════════════════════ בדיקה עצמית
def selftest():
    """מריץ את כל שרשרת האיסוף מול רשת מדומה — בלי גישה לאינטרנט."""
    print("בדיקה עצמית (רשת מדומה)\n" + "-" * 40)

    def fake_yahoo(base, days=365):
        t0 = int(time.time()) - days * 86400
        ts, cl = [], []
        for i in range(days):
            ts.append(t0 + i * 86400)
            cl.append(base * (1 + i * 0.0005))
        return json.dumps(
            {
                "chart": {
                    "result": [
                        {"timestamp": ts, "indicators": {"quote": [{"close": cl}]}}
                    ]
                }
            }
        )

    def fake_eia(base, step=1, n=400):
        d0 = date.today() - timedelta(days=n)
        data = [
            {
                "period": (d0 + timedelta(days=i)).isoformat(),
                "value": base * (1 + i * 0.0004),
            }
            for i in range(0, n, step)
        ]
        return json.dumps({"response": {"data": list(reversed(data))}})

    def router(url, label, retries=2, quiet=False, referer=None):
        if "getcrumb" in url:
            return "testcrumb"
        if "finance.yahoo.com/" in url and "query" not in url:
            return "<html>warmup</html>"
        if "query1.finance.yahoo.com" in url:
            if "BZ%3DF" in url or "BZ=F" in url:
                return fake_yahoo(88.28)
            if "RB%3DF" in url or "RB=F" in url:
                return fake_yahoo(2.45)
            if "HO%3DF" in url or "HO=F" in url:
                return fake_yahoo(2.98)
            if "TTF%3DF" in url or "TTF=F" in url:
                return fake_yahoo(67.50)
        if "api.eia.gov" in url:
            if "RBRTE" in url:
                return fake_eia(88.28)
            if "EMM_EPMR" in url:
                return fake_eia(4.006, 7)
            if "EMD_EPD2D" in url:
                return fake_eia(5.454, 7)
            if "EER_EPJK" in url:
                return fake_eia(3.57)
            if "EER_EPMRU" in url:
                return fake_eia(2.62)
            if "EPD2DXL0" in url:
                return fake_eia(3.12)
            if "WTTEXUS2" in url:
                return fake_eia(11560, 7)
            if "WCESTUS1" in url:
                return fake_eia(428900, 7)
            if "WDISTUS1" in url:
                return fake_eia(118000, 7)
        return None

    global http_get, DATA_FILE
    real = http_get
    http_get = router
    DATA_FILE = os.path.join(HERE, "_selftest_data.json")
    try:
        p = refresh("TESTKEY")
        print(
            "\n%-19s %-11s %-12s %-9s %-9s %s"
            % ("אריח", "ערך", "תאריך", "שבועי", "שנתי", "מקור")
        )
        print("-" * 100)
        for k in METRIC_IDS:
            m = p["metrics"][k]
            print(
                "%-19s %-11s %-12s %-9s %-9s %s"
                % (
                    k,
                    m["value"],
                    m["asOf"] or "-",
                    m["weeklyPct"],
                    m["yearlyPct"],
                    m["source"] or "-",
                )
            )
        assert p["metrics"]["brent"]["value"] is not None, "Brent ריק"
        assert p["metrics"]["ttf"]["value"] is not None, "TTF ריק"
        assert p["metrics"]["crack321"]["value"] is not None, "מרווח ריק"
        assert p["filled"] == 9, "ציפינו ל-9 אריחים אוטומטיים, קיבלנו %d" % p["filled"]
        print("\nמסלול ראשי: %d/%d אריחים." % (p["filled"], p["total"]))

        # --- תרחיש 2: Yahoo מת לגמרי, הכל חייב לעבור ל-Stooq ול-EIA ---
        def no_yahoo(url, label, retries=2, quiet=False, referer=None):
            if "yahoo.com" in url:
                return None
            if "stooq" in url:
                d0 = date.today() - timedelta(days=365)
                rows = ["Date,Open,High,Low,Close,Volume"]
                base = 88.28 if "cb.f" in url else 67.5 if "ttf.f" in url else 2.5
                for i in range(365):
                    rows.append(
                        "%s,0,0,0,%.4f,0"
                        % (
                            (d0 + timedelta(days=i)).isoformat(),
                            base * (1 + i * 0.0005),
                        )
                    )
                return "\n".join(rows)
            return router(url, label, retries, quiet, referer)

        globals()["http_get"] = no_yahoo
        global _YAHOO_READY
        _YAHOO_READY = False
        p2 = refresh("TESTKEY")
        assert p2["metrics"]["brent"]["value"] is not None, "גיבוי Brent נכשל"
        assert p2["metrics"]["ttf"]["value"] is not None, "גיבוי TTF נכשל"
        print("גיבוי Stooq: %d/%d אריחים." % (p2["filled"], p2["total"]))

        # --- תרחיש 3: רק EIA חי, בלי אף מקור חוזים ---
        def eia_only(url, label, retries=2, quiet=False, referer=None):
            if "api.eia.gov" in url:
                return router(url, label, retries, quiet, referer)
            return None

        globals()["http_get"] = eia_only
        _YAHOO_READY = False
        os.remove(DATA_FILE) if os.path.exists(DATA_FILE) else None
        p3 = refresh("TESTKEY")
        assert p3["metrics"]["brent"]["value"] is not None, "Brent לא נפל ל-EIA ספוט"
        assert p3["metrics"]["crack321"]["value"] is not None, "מרווח נכשל ב-EIA בלבד"
        print("EIA בלבד: %d/%d אריחים (TTF יורד לידני)." % (p3["filled"], p3["total"]))

        print("\nכל שלושת התרחישים עברו.")
    finally:
        http_get = real
        if os.path.exists(DATA_FILE):
            os.remove(DATA_FILE)
        DATA_FILE = os.path.join(HERE, "data.json")


# ════════════════════════════════════════════════════════════ אבחון מקורות
def diagnose(eia_key):
    """בודק כל מקור בנפרד מול הרשת האמיתית ומדפיס מה חזר."""
    print("\nאבחון מקורות — " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 66)

    print("\n[1] קישוריות בסיסית (גם תשובת שגיאה = 'מגיב' — השרת כן ענה)")
    print(
        "    api.eia.gov      : "
        + ("מגיב" if is_reachable("https://api.eia.gov/v2/") else "לא מגיב")
    )
    print(
        "    stooq.com        : "
        + ("מגיב" if is_reachable("https://stooq.com/") else "לא מגיב")
    )
    print(
        "    finance.yahoo.com: "
        + ("מגיב" if is_reachable("https://finance.yahoo.com/") else "לא מגיב")
    )

    print("\n[2] Yahoo — עוגיות ו-crumb")
    crumb = yahoo_auth()
    print("    עוגיות שנאספו: %d" % len(_JAR))
    print("    crumb        : " + (crumb if crumb else "לא התקבל"))

    print("\n[3] חוזים")
    for key, (sym, label) in YAHOO.items():
        pts = yahoo(sym, label)
        if not pts:
            pts = stooq(STOOQ[key], key + " (Stooq)")
        if pts:
            print("    %-6s %.4f @ %s" % (key, pts[-1]["close"], pts[-1]["date"]))
        elif key == "ttf":
            te = te_ttf()
            print(
                "    %-6s %s"
                % (
                    key,
                    (
                        "%.2f @ %s (TradingEconomics, בלי שינוי שבועי)"
                        % (te["value"], te["asOf"])
                    )
                    if te
                    else "אין נתון (גם TradingEconomics נכשל)",
                )
            )
        else:
            print("    %-6s אין נתון" % key)

    print("\n[4] EIA")
    if not eia_key:
        print("    אין מפתח. זה המקור שמכסה 8 מ-12 האריחים.")
        print("    הוצאת מפתח: https://www.eia.gov/opendata/register.php")
    else:
        for name, (sid, label) in EIA_IDS.items():
            pts = eia(eia_key, name)
            print(
                "    %-16s %-30s %s"
                % (
                    name,
                    sid,
                    ("%.4f @ %s" % (pts[-1]["close"], pts[-1]["date"]))
                    if pts
                    else "אין נתון",
                )
            )

    print("\n" + "=" * 66)
    print("אריח ריק אחרי כל זה = המקור שלו מופיע למעלה כ'אין נתון'.")
    print("מזהה סדרת EIA שנשבר -> לתקן ב-EIA_IDS בראש הקובץ.\n")


# ════════════════════════════════════════════════════════════ main
def main():
    ap = argparse.ArgumentParser(description="דשבורד אנרגיה")
    ap.add_argument(
        "--eia-key",
        default=os.environ.get("EIA_API_KEY", ""),
        help="מפתח EIA חינמי (או משתנה סביבה EIA_API_KEY)",
    )
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument(
        "--daily-at",
        default="06:00",
        metavar="HH:MM",
        help="שעה קבועה לרענון יומי אחד, שעון מקומי (ברירת מחדל 06:00 — אחרי שכל השווקים הרלוונטיים נסגרו)",
    )
    ap.add_argument(
        "--every",
        type=int,
        default=None,
        help="לרענן כל N דקות במקום פעם ביום (מבטל את --daily-at)",
    )
    ap.add_argument("--once", action="store_true", help="משיכה בלבד, בלי שרת")
    ap.add_argument("--selftest", action="store_true", help="בדיקה עם רשת מדומה")
    ap.add_argument(
        "--diag", action="store_true", help="בדיקת כל מקור מול הרשת האמיתית"
    )
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    if a.diag:
        return diagnose(a.eia_key)

    if not a.eia_key:
        print("\n" + "!" * 62)
        print("  לא הוזן מפתח EIA.")
        print("  זה המקור היחיד לבנזין, דיזל, קרוסין, ייצוא, שני המלאים")
        print("  ומרווח הזיקוק — 8 מתוך 12 האריחים.")
        print("    מפתח חינמי: https://www.eia.gov/opendata/register.php")
        print("    ואז:  python3 %s --eia-key KEY\n" % os.path.basename(__file__))

    refresh(a.eia_key)
    if a.once:
        print("נכתב: " + DATA_FILE)
        return

    Handler.eia_key = a.eia_key
    if a.every:
        threading.Thread(
            target=auto_loop, args=(a.eia_key, a.every), daemon=True
        ).start()
        schedule_msg = "רענון אוטומטי כל %d דקות." % a.every
    else:
        threading.Thread(
            target=daily_loop, args=(a.eia_key, a.daily_at), daemon=True
        ).start()
        schedule_msg = "רענון קבוע פעם ביום, בשעה %s (סגירת השווקים)." % a.daily_at
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    print("\n" + "=" * 62)
    print("  הדשבורד רץ:   http://localhost:%d" % a.port)
    print("  " + schedule_msg + "  Ctrl+C ליציאה.")
    print("=" * 62 + "\n", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nהופסק.")


if __name__ == "__main__":
    main()
