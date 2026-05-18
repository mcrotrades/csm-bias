"""
Currency Strength Meter — yFinance Edition v10
===============================================
Drop-in replacement for the MT5 edition.
All logic is IDENTICAL — only the data source changed.

Changes from MT5 edition:
  • MetaTrader5 removed — replaced with yfinance
  • Forex symbols use Yahoo Finance format: EURUSD=X
  • No MT5 terminal required — runs on Linux, Android, Render, cloud
  • Added retry logic for failed symbol fetches
  • scheduler built-in for 5x daily auto-broadcast (PHT times)

Requirements:
    pip install yfinance requests pytz schedule
"""

import os
import sys
import time
import traceback
import requests
import pytz
import schedule
import yfinance as yf
from datetime import datetime, timezone

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csm_log.txt")

class Tee:
    def __init__(self, filepath):
        self.file   = open(filepath, "a", encoding="utf-8")
        self.stdout = sys.stdout
    def write(self, msg):
        self.stdout.write(msg)
        self.file.write(msg)
        self.file.flush()
    def flush(self):
        self.stdout.flush()
        self.file.flush()
    def close(self):
        self.file.close()

def init_logging():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stdout = Tee(LOG_FILE)
    print(f"\n{'='*60}")
    print(f"  RUN STARTED: {now}")
    print(f"{'='*60}")

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8781053968:AAE2f3-DtO7Cl6r41lGkZ97QqKPlu6y2GjE")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "6579085899") #@Ardhie110388
TELEGRAM_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2", "")

TELEGRAM_CHANNELS  = [cid for cid in [TELEGRAM_CHAT_ID, TELEGRAM_CHAT_ID_2] if cid]

EMA_LEN  = 20
ATR_LEN  = 14
BB_LEN   = 20
BB_MULT  = 2.0
ATR_AVG  = 20
ATR_LOW  = 0.80
ATR_HIGH = 1.20
PAIR     = "EURUSD"

BM_D1_THRESH = 4
BM_H4_THRESH = 3
BM_H1_THRESH = 2

# ──────────────────────────────────────────────
# BAR COUNTS PER TIMEFRAME
# yfinance period strings to get enough bars
# ──────────────────────────────────────────────
YF_CONFIG = {
    "H1": {"interval": "1h",  "period": "60d"},   # ~1440 H1 bars
    "H4": {"interval": "1h",  "period": "60d"},   # will resample to H4
    "D1": {"interval": "1d",  "period": "2y"},    # ~500 D1 bars
}

# ──────────────────────────────────────────────
# 28-PAIR UNIVERSE
# ──────────────────────────────────────────────
PAIRS = [
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD",
    "USDJPY", "USDCHF", "USDCAD",
    "EURGBP", "EURAUD", "EURNZD", "EURJPY", "EURCHF", "EURCAD",
    "GBPAUD", "GBPNZD", "GBPJPY", "GBPCHF", "GBPCAD",
    "AUDNZD", "AUDJPY", "AUDCHF", "AUDCAD",
    "NZDJPY", "NZDCHF", "NZDCAD",
    "CHFJPY", "CADJPY",
    "CADCHF",
]

CURRENCIES = ["EUR", "USD", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD"]

FLAGS = {
    "EUR": "🇪🇺", "USD": "🇺🇸", "GBP": "🇬🇧", "JPY": "🇯🇵",
    "CHF": "🇨🇭", "AUD": "🇦🇺", "NZD": "🇳🇿", "CAD": "🇨🇦",
}

MAX_PAIRS = {
    "EUR": 7, "USD": 7, "GBP": 7, "JPY": 7,
    "CHF": 7, "AUD": 7, "NZD": 7, "CAD": 7,
}

PAIR_CURRENCIES = {
    "EURUSD": ("EUR","USD"), "GBPUSD": ("GBP","USD"),
    "AUDUSD": ("AUD","USD"), "NZDUSD": ("NZD","USD"),
    "USDJPY": ("USD","JPY"), "USDCHF": ("USD","CHF"),
    "USDCAD": ("USD","CAD"), "EURGBP": ("EUR","GBP"),
    "EURAUD": ("EUR","AUD"), "EURNZD": ("EUR","NZD"),
    "EURJPY": ("EUR","JPY"), "EURCHF": ("EUR","CHF"),
    "EURCAD": ("EUR","CAD"), "GBPAUD": ("GBP","AUD"),
    "GBPNZD": ("GBP","NZD"), "GBPJPY": ("GBP","JPY"),
    "GBPCHF": ("GBP","CHF"), "GBPCAD": ("GBP","CAD"),
    "AUDNZD": ("AUD","NZD"), "AUDJPY": ("AUD","JPY"),
    "AUDCHF": ("AUD","CHF"), "AUDCAD": ("AUD","CAD"),
    "NZDJPY": ("NZD","JPY"), "NZDCHF": ("NZD","CHF"),
    "NZDCAD": ("NZD","CAD"), "CHFJPY": ("CHF","JPY"),
    "CADJPY": ("CAD","JPY"), "CADCHF": ("CAD","CHF"),
}

# ──────────────────────────────────────────────
# INDICATOR FUNCTIONS  (unchanged from MT5 version)
# ──────────────────────────────────────────────
def calc_ema(values, length):
    if len(values) < length:
        return None
    alpha = 2.0 / (length + 1)
    ema = values[0]
    for price in values[1:]:
        ema = alpha * price + (1 - alpha) * ema
    return ema

def bull_flag_ema(closes, length):
    if len(closes) < length:
        return False
    ema = calc_ema(closes, length)
    if ema is None:
        return False
    return closes[-1] > ema

def calc_atr(highs, lows, closes, length=14):
    if len(highs) < length + 1:
        return None
    tr_values = []
    for i in range(1, len(highs)):
        h_l  = highs[i] - lows[i]
        h_cp = abs(highs[i] - closes[i-1])
        l_cp = abs(lows[i]  - closes[i-1])
        tr_values.append(max(h_l, h_cp, l_cp))
    if len(tr_values) < length:
        return None
    alpha = 1.0 / length
    atr   = tr_values[0]
    for tr in tr_values[1:]:
        atr = alpha * tr + (1 - alpha) * atr
    return atr

def calc_atr_series(highs, lows, closes, length=14):
    if len(highs) < length + 1:
        return []
    tr_values = []
    for i in range(1, len(highs)):
        h_l  = highs[i] - lows[i]
        h_cp = abs(highs[i] - closes[i-1])
        l_cp = abs(lows[i]  - closes[i-1])
        tr_values.append(max(h_l, h_cp, l_cp))
    if len(tr_values) < length:
        return []
    alpha  = 1.0 / length
    atr    = tr_values[0]
    series = [atr]
    for tr in tr_values[1:]:
        atr = alpha * tr + (1 - alpha) * atr
        series.append(atr)
    return series

def calc_std(values, length):
    if len(values) < length:
        return None
    recent   = values[-length:]
    mean     = sum(recent) / length
    variance = sum((x - mean) ** 2 for x in recent) / length
    return variance ** 0.5

def atr_regime(atr_val, atr_series, avg_bars=20):
    if atr_val is None or len(atr_series) < avg_bars:
        return None, "N/A", "⚪"
    recent_avg = sum(atr_series[-avg_bars:]) / avg_bars
    if recent_avg == 0:
        return None, "N/A", "⚪"
    ratio = atr_val / recent_avg
    if ratio < ATR_LOW:
        return round(ratio, 2), "LOW", "🔴"
    elif ratio > ATR_HIGH:
        return round(ratio, 2), "HIGH", "🟢"
    else:
        return round(ratio, 2), "NORMAL", "🟡"

def calc_bbw(closes, length=20, mult=2.0):
    if len(closes) < length * 2:
        return None, False
    bbw_values = []
    for i in range(length, len(closes) + 1):
        window = closes[i-length:i]
        mid    = sum(window) / length
        std    = calc_std(closes[:i], length)
        if std is None or mid == 0:
            continue
        upper = mid + mult * std
        lower = mid - mult * std
        bbw   = (upper - lower) / mid * 100
        bbw_values.append(bbw)
    if not bbw_values:
        return None, False
    latest     = bbw_values[-1]
    recent_min = min(bbw_values[-length:]) if len(bbw_values) >= length else min(bbw_values)
    is_squeeze = latest <= recent_min * 1.05
    return round(latest, 2), is_squeeze

# ──────────────────────────────────────────────
# DATA FETCH — yfinance  (replaces MT5 fetch)
# ──────────────────────────────────────────────
def yf_symbol(pair):
    """Convert EURUSD → EURUSD=X for Yahoo Finance."""
    return f"{pair}=X"

def fetch_ohlc_yf(pair, tf, retries=3):
    """
    Fetch OHLC data from Yahoo Finance.
    tf: 'H1', 'H4', or 'D1'
    Returns dict with open/high/low/close lists, or None on failure.
    """
    symbol = yf_symbol(pair)

    if tf == "H1":
        interval, period = "1h", "60d"
    elif tf == "H4":
        interval, period = "1h", "60d"   # fetch 1h, resample to 4h
    else:  # D1
        interval, period = "1d", "2y"

    for attempt in range(retries):
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(interval=interval, period=period, auto_adjust=True)

            if df is None or df.empty:
                time.sleep(1)
                continue

            # Resample H1 data → H4
            if tf == "H4":
                df = df.resample("4h").agg({
                    "Open":   "first",
                    "High":   "max",
                    "Low":    "min",
                    "Close":  "last",
                    "Volume": "sum",
                }).dropna()

            return {
                "open":  df["Open"].tolist(),
                "high":  df["High"].tolist(),
                "low":   df["Low"].tolist(),
                "close": df["Close"].tolist(),
            }

        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                print(f"  ⚠ {pair} {tf} fetch failed: {e}")
    return None

def fetch_all_data():
    """
    Fetch all 28 pairs across H1/H4/D1.
    Returns (signals, volatility) — same structure as MT5 version.
    """
    signals    = {}
    volatility = {}

    print(f"Fetching market data for {len(PAIRS)} pairs from Yahoo Finance…")

    for i, pair in enumerate(PAIRS, 1):
        print(f"  [{i:>2}/{len(PAIRS)}] {pair}", end="  ")
        signals[pair] = {}

        # ── EMA bull flags per TF ──────────────
        for tf in ("H1", "H4", "D1"):
            data = fetch_ohlc_yf(pair, tf)
            if data and len(data["close"]) >= EMA_LEN:
                bull = bull_flag_ema(data["close"], EMA_LEN)
            else:
                bull = False
            signals[pair][tf] = bull

        # ── Volatility: D1 ATR + BBW ───────────
        data_d1 = fetch_ohlc_yf(pair, "D1")
        if data_d1 and len(data_d1["close"]) >= max(ATR_LEN, BB_LEN * 2):
            atr_d1    = calc_atr(data_d1["high"], data_d1["low"], data_d1["close"], ATR_LEN)
            series_d1 = calc_atr_series(data_d1["high"], data_d1["low"], data_d1["close"], ATR_LEN)
            bbw_val, is_sq = calc_bbw(data_d1["close"], BB_LEN, BB_MULT)
        else:
            atr_d1, series_d1, bbw_val, is_sq = None, [], None, False

        ratio_d1, regime_d1, emoji_d1 = atr_regime(atr_d1, series_d1, ATR_AVG)

        # ── Volatility: H4 ATR ─────────────────
        data_h4 = fetch_ohlc_yf(pair, "H4")
        if data_h4 and len(data_h4["close"]) >= max(ATR_LEN, ATR_AVG + ATR_LEN):
            atr_h4    = calc_atr(data_h4["high"], data_h4["low"], data_h4["close"], ATR_LEN)
            series_h4 = calc_atr_series(data_h4["high"], data_h4["low"], data_h4["close"], ATR_LEN)
        else:
            atr_h4, series_h4 = None, []

        ratio_h4, regime_h4, emoji_h4 = atr_regime(atr_h4, series_h4, ATR_AVG)

        volatility[pair] = {
            "atr":             round(atr_d1, 5) if atr_d1 is not None else None,
            "atr_ratio":       ratio_d1,
            "regime":          regime_d1,
            "regime_emoji":    emoji_d1,
            "atr_h4":          round(atr_h4, 5) if atr_h4 is not None else None,
            "atr_ratio_h4":    ratio_h4,
            "regime_h4":       regime_h4,
            "regime_emoji_h4": emoji_h4,
            "bbw":             bbw_val,
            "squeeze":         is_sq,
        }

        sq_tag = "🔴SQ" if is_sq else ""
        print(
            f"D1:{emoji_d1}{regime_d1}(x{ratio_d1})  "
            f"H4:{emoji_h4}{regime_h4}(x{ratio_h4})  "
            f"BBW={bbw_val}%  {sq_tag}"
        )

    return signals, volatility

# ──────────────────────────────────────────────
# SCORE CALCULATION  (unchanged)
# ──────────────────────────────────────────────
def b2s(b):
    return 1 if b else -1

def calc_scores(signals):
    scores = {}
    for tf in ("H1", "H4", "D1"):
        def g(p, _tf=tf):
            return b2s(signals[p][_tf])

        tf_vals = [
            # EUR
            (g("EURUSD") + g("EURGBP") + g("EURAUD") + g("EURNZD")
             + g("EURJPY") + g("EURCHF") + g("EURCAD")),
            # USD
            (-g("EURUSD") - g("GBPUSD") - g("AUDUSD") - g("NZDUSD")
             + g("USDJPY") + g("USDCHF") + g("USDCAD")),
            # GBP
            (g("GBPUSD") - g("EURGBP") + g("GBPAUD") + g("GBPNZD")
             + g("GBPJPY") + g("GBPCHF") + g("GBPCAD")),
            # JPY
            (-g("USDJPY") - g("EURJPY") - g("GBPJPY") - g("AUDJPY")
             - g("NZDJPY") - g("CHFJPY") - g("CADJPY")),
            # CHF
            (-g("USDCHF") - g("EURCHF") - g("GBPCHF") + g("CHFJPY")
             - g("AUDCHF") - g("NZDCHF") - g("CADCHF")),
            # AUD
            (g("AUDUSD") - g("EURAUD") - g("GBPAUD") + g("AUDNZD")
             + g("AUDJPY") + g("AUDCHF") + g("AUDCAD")),
            # NZD
            (g("NZDUSD") - g("EURNZD") - g("GBPNZD") - g("AUDNZD")
             + g("NZDJPY") + g("NZDCHF") + g("NZDCAD")),
            # CAD
            (-g("USDCAD") - g("EURCAD") - g("GBPCAD") - g("AUDCAD")
             - g("NZDCAD") + g("CADJPY") + g("CADCHF")),
        ]
        for ccy, val in zip(CURRENCIES, tf_vals):
            scores.setdefault(ccy, {})[tf] = val

    for ccy in CURRENCIES:
        s          = scores[ccy]
        s["total"] = s["H1"] * 1 + s["H4"] * 2 + s["D1"] * 3
        mx         = MAX_PAIRS[ccy] * 6
        s["norm"]  = round(s["total"] / mx * 10) if mx else 0

    return scores

# ──────────────────────────────────────────────
# RANKING & DISPLAY HELPERS  (unchanged)
# ──────────────────────────────────────────────
def rank_currencies(scores):
    return sorted(CURRENCIES, key=lambda c: scores[c]["norm"], reverse=True)

def alignment_label(ccy, scores):
    s  = scores[ccy]
    d  = 1 if s["norm"] >= 0 else -1
    ag = sum(1 for tf in ("H1", "H4", "D1") if s[tf] * d > 0)
    return ["✗ 0/3", "⚠ 1/3", "⚡ 2/3", "✅ 3/3"][ag]

def bias_label(norm):
    if norm > 0: return "STRONG ▲"
    if norm < 0: return "WEAK ▼"
    return "NEUTRAL"

def state_label(bn, qn):
    if bn > 0 and qn < 0: return "▲ IDEAL LONG ★★★"
    if bn < 0 and qn > 0: return "▼ IDEAL SHORT ★★★"
    if bn > 0 and qn > 0: return "▲ CHOPPY LONG ★☆☆" if bn > qn else "▼ CHOPPY SHORT ★☆☆"
    if bn < 0 and qn < 0: return "▼ CHOPPY SHORT ★☆☆" if bn < qn else "▲ CHOPPY LONG ★☆☆"
    return "◆ NEUTRAL —"

def divergence_label(div):
    a = abs(div)
    if a >= 6: return "HIGH CONVICTION ✅"
    if a >= 3: return "MODERATE ⚡"
    return "LOW — AVOID ⚠"

def bar(norm):
    n = min(10, max(0, abs(norm)))
    return "█" * n + "░" * (10 - n)

def to_pips(atr, pair):
    pip_size = 0.01 if "JPY" in pair else 0.0001
    return round(atr / pip_size, 1)

def fmt_atr(pair, volatility):
    v = volatility.get(pair, {})
    a = v.get("atr")
    if a is None:
        return "N/A"
    d1_str = ""
    h4_str = ""
    if v.get("regime") and v.get("regime") != "N/A":
        d1_str = f"  D1:{v['regime_emoji']}{v['regime']}"
        if v.get("atr_ratio"):
            d1_str += f"(x{v['atr_ratio']:.2f})"
    if v.get("regime_h4") and v.get("regime_h4") != "N/A":
        h4_str = f"  H4:{v['regime_emoji_h4']}{v['regime_h4']}"
        if v.get("atr_ratio_h4"):
            h4_str += f"(x{v['atr_ratio_h4']:.2f})"
    return f"{a:.5f} ({to_pips(a, pair)} pips){d1_str}{h4_str}"

def fmt_bbw(pair, volatility):
    v  = volatility.get(pair, {})
    bw = v.get("bbw")
    sq = v.get("squeeze", False)
    if bw is None:
        return "N/A"
    tag = " 🔴SQUEEZE" if sq else (" 🟢EXPAND" if bw > 3 else "")
    return f"{bw:.2f}%{tag}"

# ──────────────────────────────────────────────
# BIG MONEY RADAR  (unchanged)
# ──────────────────────────────────────────────
def bm_direction(score, thresh):
    if score >= thresh:  return "BULL"
    if score <= -thresh: return "BEAR"
    return "NEUTRAL"

def usd_jpy_flow_state(scores):
    usd_d1 = scores["USD"]["D1"]
    jpy_d1 = scores["JPY"]["D1"]
    usd_dir = bm_direction(usd_d1, BM_D1_THRESH)
    jpy_dir = bm_direction(jpy_d1, BM_D1_THRESH)

    if usd_dir == "BULL" and jpy_dir == "BEAR":
        return ("RISK-ON 🟢", "🟢",
                "Carry trade ACTIVE — capital flowing into markets",
                "Favor: AUDJPY▲ NZDJPY▲ GBPJPY▲  |  Avoid JPY longs")
    elif usd_dir == "BEAR" and jpy_dir == "BULL":
        return ("RISK-OFF 🔴", "🔴",
                "Flight to safety — capital retreating",
                "Favor: JPY longs across board  |  Short AUD NZD GBP")
    elif usd_dir == "BULL" and jpy_dir == "BULL":
        return ("USD DOMINANCE ⚡", "⚡",
                "Dollar wrecking ball — everything falls vs USD incl JPY",
                "USD longs only clean trade — other signals may be noise")
    elif usd_dir == "BEAR" and jpy_dir == "BEAR":
        return ("TRANSITIONAL ⏸", "⏸",
                "Both anchors weak — no clear institutional commitment",
                "Reduce exposure — wait for USD or JPY to take a side")
    else:
        return ("NEUTRAL —", "⚪",
                "No strong institutional signal from USD/JPY",
                "Use individual CSM scores — no macro filter active")

def institutional_alignment(scores):
    aligned = []
    for ccy in CURRENCIES:
        s      = scores[ccy]
        d1_dir = bm_direction(s["D1"], BM_D1_THRESH)
        h4_dir = bm_direction(s["H4"], BM_H4_THRESH)
        if d1_dir == "BULL" and h4_dir == "BULL":
            aligned.append((ccy, "BULL", s["D1"], s["H4"]))
        elif d1_dir == "BEAR" and h4_dir == "BEAR":
            aligned.append((ccy, "BEAR", s["D1"], s["H4"]))
    return aligned

def divergence_alerts(scores):
    alerts = []
    for ccy in CURRENCIES:
        s      = scores[ccy]
        d1_dir = bm_direction(s["D1"], BM_D1_THRESH)
        h1_dir = bm_direction(s["H1"], BM_H1_THRESH)
        if d1_dir == "BULL" and h1_dir == "BEAR":
            alerts.append((ccy, "ACCUMULATION", s["D1"], s["H1"],
                           "D1 bullish, H1 bearish — smart money accumulating"))
        elif d1_dir == "BEAR" and h1_dir == "BULL":
            alerts.append((ccy, "DISTRIBUTION", s["D1"], s["H1"],
                           "D1 bearish, H1 bullish — smart money distributing"))
    return alerts

def high_conviction_pairs(scores, aligned_currencies):
    aligned_bull = {ccy for ccy, dir_, _, _ in aligned_currencies if dir_ == "BULL"}
    aligned_bear = {ccy for ccy, dir_, _, _ in aligned_currencies if dir_ == "BEAR"}
    hc_pairs = []
    seen = set()
    for pair in PAIRS:
        base  = pair[:3]
        quote = pair[3:]
        if base not in CURRENCIES or quote not in CURRENCIES:
            continue
        key = tuple(sorted([base, quote]))
        if key in seen:
            continue
        seen.add(key)
        if base in aligned_bull and quote in aligned_bear:
            hc_pairs.append((pair, "LONG", base, quote))
        elif base in aligned_bear and quote in aligned_bull:
            hc_pairs.append((pair, "SHORT", base, quote))
    return hc_pairs

def fmt_big_money_section(scores):
    lines = []
    lines.append("🏦 *BIG MONEY RADAR*")
    lines.append("─────────────────────")

    state, emoji, desc, bias = usd_jpy_flow_state(scores)
    usd_d1 = scores["USD"]["D1"]
    jpy_d1 = scores["JPY"]["D1"]
    lines.append(f"*Flow State:* `{state}`")
    lines.append(f"USD D1:`{usd_d1:+}`  JPY D1:`{jpy_d1:+}`")
    lines.append(f"_{desc}_")
    lines.append(f"📌 {bias}")
    lines.append("")

    aligned = institutional_alignment(scores)
    lines.append("*✅ Institutional Alignment* _(D1 + H4 agree)_")
    if aligned:
        for ccy, dir_, d1, h4 in aligned:
            arrow = "▲" if dir_ == "BULL" else "▼"
            label = "BULL" if dir_ == "BULL" else "BEAR"
            lines.append(f"  {FLAGS[ccy]}{ccy}: `{arrow} {label}`  D1:`{d1:+}`  H4:`{h4:+}`")
    else:
        lines.append("  _No institutional alignment detected_")
    lines.append("")

    divs = divergence_alerts(scores)
    lines.append("*⚠️ Divergence Alerts* _(D1 vs H1 disagree)_")
    if divs:
        for ccy, alert_type, d1, h1, note in divs:
            icon = "📈" if alert_type == "ACCUMULATION" else "📉"
            lines.append(f"  {icon} {FLAGS[ccy]}{ccy}: `{alert_type}`  D1:`{d1:+}`  H1:`{h1:+}`")
            lines.append(f"  _↳ {note}_")
    else:
        lines.append("  _No divergence patterns detected_")
    lines.append("")

    hc = high_conviction_pairs(scores, aligned)
    lines.append("*🎯 High-Conviction Pairs* _(both currencies institutionally aligned)_")
    if hc:
        for pair, direction, base, quote in hc:
            arrow = "▲" if direction == "LONG" else "▼"
            lines.append(f"  {arrow} `{direction} {pair}`  {FLAGS[base]}{base}✅ vs {FLAGS[quote]}{quote}✅")
    else:
        lines.append("  _No high-conviction pairs — wait for institutional alignment_")

    return "\n".join(lines)

# ──────────────────────────────────────────────
# PAIR BIAS SIGNAL  (unchanged)
# ──────────────────────────────────────────────
def pair_bias_signal(pair, scores):
    if pair not in PAIR_CURRENCIES:
        return "— NEUTRAL", "—", ""
    base, quote = PAIR_CURRENCIES[pair]
    if base not in scores or quote not in scores:
        return "— NEUTRAL", "—", ""

    bn = scores[base]["norm"]
    qn = scores[quote]["norm"]

    if bn > qn:
        direction = "LONG"
        b_d1 = scores[base]["D1"]  > 0
        b_h4 = scores[base]["H4"]  > 0
        b_h1 = scores[base]["H1"]  > 0
        q_d1 = scores[quote]["D1"] < 0
        q_h4 = scores[quote]["H4"] < 0
        q_h1 = scores[quote]["H1"] < 0
    elif qn > bn:
        direction = "SHORT"
        b_d1 = scores[base]["D1"]  < 0
        b_h4 = scores[base]["H4"]  < 0
        b_h1 = scores[base]["H1"]  < 0
        q_d1 = scores[quote]["D1"] > 0
        q_h4 = scores[quote]["H4"] > 0
        q_h1 = scores[quote]["H1"] > 0
    else:
        return "— NEUTRAL", "—", ""

    def arr(flag): return "▲" if flag else "▼"
    base_tfs  = f"D1{arr(b_d1)} H4{arr(b_h4)} H1{arr(b_h1)}"
    quote_tfs = f"D1{arr(q_d1)} H4{arr(q_h4)} H1{arr(q_h1)}"
    detail    = f"{base}:[{base_tfs}] {quote}:[{quote_tfs}]"

    base_agree  = sum([b_d1, b_h4, b_h1])
    quote_agree = sum([q_d1, q_h4, q_h1])

    if base_agree == 3 and quote_agree == 3:
        label = f"✅ STRONG {'BUY' if direction == 'LONG' else 'SELL'}"
        emoji = "✅"
    elif b_d1 and b_h4 and q_d1 and q_h4 and (not b_h1 or not q_h1):
        label = f"⚡ PULLBACK {'LONG' if direction == 'LONG' else 'SHORT'}"
        emoji = "⚡"
    elif b_d1 and q_d1 and (not b_h4 or not q_h4):
        label = "⚠ WAIT — H4 correcting"
        emoji = "⚠"
    elif not b_d1 or not q_d1:
        label = "❌ AVOID — counter-trend"
        emoji = "❌"
    else:
        label = "— NEUTRAL"
        emoji = "—"

    return label, emoji, detail

# ──────────────────────────────────────────────
# TRADE SCORING  (unchanged)
# ──────────────────────────────────────────────
def score_trade(pair, scores, volatility):
    if pair not in PAIR_CURRENCIES:
        return None
    base, quote = PAIR_CURRENCIES[pair]
    if base not in scores or quote not in scores:
        return None

    bn  = scores[base]["norm"]
    qn  = scores[quote]["norm"]
    div = bn - qn

    if bn == 0 and qn == 0:
        return None

    if div > 0:
        direction = "LONG"
    elif div < 0:
        direction = "SHORT"
    else:
        return None

    adiv = abs(div)
    if adiv >= 14:   conv_pts = 30
    elif adiv >= 10: conv_pts = 25
    elif adiv >= 6:  conv_pts = 20
    elif adiv >= 3:  conv_pts = 10
    else:            conv_pts = 0

    d_base   = 1 if bn > 0 else -1
    d_quote  = 1 if qn > 0 else -1
    ag_base  = sum(1 for tf in ("H1","H4","D1") if scores[base][tf]  * d_base  > 0)
    ag_quote = sum(1 for tf in ("H1","H4","D1") if scores[quote][tf] * d_quote > 0)
    align_pts = round((ag_base + ag_quote) / 6 * 20)

    v         = volatility.get(pair, {})
    regime    = v.get("regime", "N/A")
    regime_h4 = v.get("regime_h4", "N/A")
    is_sq     = v.get("squeeze", False)
    bbw       = v.get("bbw")

    if regime == "LOW" and regime_h4 == "LOW" and not is_sq:
        return None

    if regime == "HIGH":     atr_pts = 10
    elif regime == "NORMAL": atr_pts = 7
    elif regime == "LOW":    atr_pts = 3
    else:                    atr_pts = 0

    if regime_h4 == "HIGH":
        atr_pts = min(15, atr_pts + 5)
    elif regime_h4 == "LOW" and regime != "LOW":
        atr_pts = max(0, atr_pts - 2)

    if is_sq:                              bbw_pts = 5
    elif bbw is not None and bbw > 3:      bbw_pts = 15
    elif bbw is not None:                  bbw_pts = 10
    else:                                  bbw_pts = 0

    state = state_label(bn, qn)
    if "IDEAL" in state:    state_pts = 20
    elif "CHOPPY" in state: state_pts = 5
    else:                   state_pts = 0

    total_pts = conv_pts + align_pts + atr_pts + bbw_pts + state_pts

    if total_pts >= 75:   grade = "A ★★★"
    elif total_pts >= 55: grade = "B ★★☆"
    elif total_pts >= 35: grade = "C ★☆☆"
    else:                 grade = "D ✗"

    atr_pips = to_pips(v["atr"], pair) if v.get("atr") else None

    if is_sq and (regime == "LOW" or regime_h4 == "LOW"):
        vol_alert = "⏳ COILING"
    elif is_sq:
        vol_alert = "⚠️ SQUEEZE"
    elif regime == "HIGH" and regime_h4 == "LOW":
        vol_alert = "⏸ FADING"
    elif regime == "HIGH" or regime_h4 == "HIGH":
        vol_alert = "🟢 EXPANDING"
    else:
        vol_alert = ""

    return {
        "pair":            pair,
        "direction":       direction,
        "div":             div,
        "score":           total_pts,
        "grade":           grade,
        "state":           state,
        "regime":          regime,
        "regime_emoji":    v.get("regime_emoji", "⚪"),
        "regime_h4":       regime_h4,
        "regime_emoji_h4": v.get("regime_emoji_h4", "⚪"),
        "bbw":             bbw,
        "squeeze":         is_sq,
        "vol_alert":       vol_alert,
        "atr_pips":        atr_pips,
        "conv_pts":        conv_pts,
        "align_pts":       align_pts,
        "ag_base":         ag_base,
        "ag_quote":        ag_quote,
        "base":            base,
        "quote":           quote,
        "bn":              bn,
        "qn":              qn,
    }

# ──────────────────────────────────────────────
# TRADE SUGGESTIONS  (unchanged)
# ──────────────────────────────────────────────
def build_trade_suggestions(scores, volatility):
    MAJORS  = {"EURUSD","GBPUSD","AUDUSD","NZDUSD","USDJPY","USDCHF","USDCAD"}
    MINORS  = {"EURGBP","EURJPY","EURAUD","EURNZD","EURCHF","EURCAD",
               "GBPJPY","GBPAUD","GBPNZD","GBPCHF","GBPCAD"}
    CROSSES = {"AUDJPY","AUDNZD","AUDCHF","AUDCAD",
               "NZDJPY","NZDCHF","NZDCAD",
               "CHFJPY","CADJPY","CADCHF"}

    groups = {"majors": [], "minors": [], "crosses": []}

    for pair in PAIRS:
        t = score_trade(pair, scores, volatility)
        signal_label, signal_emoji, detail = pair_bias_signal(pair, scores)

        if t is None:
            if pair not in PAIR_CURRENCIES:
                continue
            base, quote = PAIR_CURRENCIES[pair]
            if base not in scores or quote not in scores:
                continue
            t = {
                "pair":            pair,
                "direction":       "—",
                "score":           0,
                "grade":           "—",
                "div":             0,
                "vol_alert":       "🔴 DEAD MKT",
                "atr_pips":        None,
                "bbw":             volatility.get(pair, {}).get("bbw"),
                "regime":          volatility.get(pair, {}).get("regime", "N/A"),
                "regime_emoji":    volatility.get(pair, {}).get("regime_emoji", "⚪"),
                "regime_h4":       volatility.get(pair, {}).get("regime_h4", "N/A"),
                "regime_emoji_h4": volatility.get(pair, {}).get("regime_emoji_h4", "⚪"),
                "base":            base,
                "quote":           quote,
                "ag_base":         0,
                "ag_quote":        0,
            }

        t["signal"]  = signal_label
        t["s_emoji"] = signal_emoji
        t["detail"]  = detail

        if pair in MAJORS:
            groups["majors"].append(t)
        elif pair in MINORS:
            groups["minors"].append(t)
        elif pair in CROSSES:
            groups["crosses"].append(t)

    SIGNAL_ORDER = {"✅": 0, "⚡": 1, "⚠": 2, "❌": 3, "—": 4}
    def sort_key(t):
        return (SIGNAL_ORDER.get(t.get("s_emoji", "—"), 4), -t["score"])

    for key in groups:
        groups[key].sort(key=sort_key)

    return groups

def _fmt_suggestion(i, t):
    arrow   = "▲" if t["direction"] == "LONG" else ("▼" if t["direction"] == "SHORT" else " ")
    bbw_s   = f"{t['bbw']:.1f}%" if t.get("bbw") is not None else "N/A"
    atr_s   = f"{t['atr_pips']:.0f}p" if t.get("atr_pips") else "N/A"
    vol_tag = f"  `{t['vol_alert']}`" if t.get("vol_alert") else ""
    score_s = f"{t['score']}/100" if t["score"] > 0 else "—"

    return (
        f"\n*{i}. {t['signal']}*{vol_tag}\n"
        f"  `{arrow} {t['direction']} {t['pair']}`  {t['grade']}  {score_s}\n"
        f"  _{t['detail']}_\n"
        f"  D1`{t['regime_emoji']}{t['regime']}`  "
        f"H4`{t['regime_emoji_h4']}{t['regime_h4']}`  "
        f"ATR:`{atr_s}`  BBW:`{bbw_s}`"
    )

def fmt_trade_suggestions(groups):
    lines = ["📋 *28-PAIR BIAS SCAN*", ""]
    sections = [
        ("💵 *MAJORS*  _(7 pairs)_",   groups["majors"]),
        ("🔀 *MINORS*  _(11 pairs)_",  groups["minors"]),
        ("➕ *CROSSES* _(10 pairs)_",  groups["crosses"]),
    ]
    for header, items in sections:
        lines.append(header)
        if not items:
            lines.append("_No data_")
        else:
            for i, t in enumerate(items, 1):
                lines.append(_fmt_suggestion(i, t))
        lines.append("")
    lines += [
        "─────────────────────",
        "_✅ Strong · ⚡ Pullback · ⚠ Wait · ❌ Avoid · — Neutral_",
    ]
    return "\n".join(lines)

# ──────────────────────────────────────────────
# TELEGRAM MESSAGES  (unchanged)
# ──────────────────────────────────────────────
def build_message_1(scores, ranked, volatility, pair=PAIR):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    m   = []

    m += [
        f"⚡ *CURRENCY STRENGTH METER v10 YF*  _(1/2)_",
        f"EMA{EMA_LEN} · ATR{ATR_LEN} · BB{BB_LEN} · D1+H4 Regime · Big Money Radar",
        f"`{now}`", "",
    ]

    m.append("```")
    m.append(f"{'#':<3} {'CCY':<6} {'H1':>4} {'H4':>4} {'D1':>4} {'SCR':>5}  {'STRENGTH':<10} {'ALN':<6} BIAS")
    m.append("─" * 66)
    for rank, ccy in enumerate(ranked, 1):
        s      = scores[ccy]
        prefix = "▶" if rank == 1 else ("◀" if rank == 8 else " ")
        m.append(
            f"{prefix}{rank:<2} {FLAGS[ccy]}{ccy:<4} "
            f"{s['H1']:>+4} {s['H4']:>+4} {s['D1']:>+4} "
            f"{s['norm']:>+5}  {bar(s['norm']):<10} "
            f"{alignment_label(ccy, scores):<6} {bias_label(s['norm'])}"
        )
    m.append("```\n")

    p     = pair.upper()
    base  = p[:3]
    quote = p[3:6]
    if base in scores and quote in scores:
        bn  = scores[base]["norm"]
        qn  = scores[quote]["norm"]
        div = bn - qn
        m += [
            f"📊 *PAIR ANALYSIS — {p}*",
            f"State: `{state_label(bn, qn)}`",
            f"{FLAGS[base]}{base}: H1:{scores[base]['H1']:+} H4:{scores[base]['H4']:+} D1:{scores[base]['D1']:+} Scr:{bn:+}",
            f"{FLAGS[quote]}{quote}: H1:{scores[quote]['H1']:+} H4:{scores[quote]['H4']:+} D1:{scores[quote]['D1']:+} Scr:{qn:+}",
            f"Div: `{div:+} → {divergence_label(div)}`",
            f"ATR: `{fmt_atr(p, volatility)}`",
            f"BBW: `{fmt_bbw(p, volatility)}`", "",
        ]

    top  = ranked[0]
    bot  = ranked[-1]
    sprd = scores[top]["norm"] - scores[bot]["norm"]
    m   += [
        f"🏆 *BEST SETUP*",
        f"`▲ LONG {top}{bot}  |  spread:{sprd:+}  |  {top}#1 vs {bot}#8`", "",
    ]

    m.append(fmt_big_money_section(scores))

    return "\n".join(m)

def build_message_2(scores, volatility):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    m   = []
    m  += [
        f"📋 *28-PAIR BIAS SCAN v10 YF*  _(2/2)_",
        f"`{now}`", "",
    ]
    groups = build_trade_suggestions(scores, volatility)
    m.append(fmt_trade_suggestions(groups))
    return "\n".join(m)

# ──────────────────────────────────────────────
# TELEGRAM SEND  (unchanged)
# ──────────────────────────────────────────────
def send_telegram(message, token, chat_id):
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    resp    = requests.post(url, json=payload, timeout=15)
    if resp.ok:
        print("✅ Telegram message sent successfully.")
    else:
        print(f"❌ Telegram error {resp.status_code}: {resp.text}")
    return resp

# ──────────────────────────────────────────────
# CORE RUN FUNCTION
# ──────────────────────────────────────────────
def run_csm():
    """Single full run: fetch → score → broadcast."""
    pht = pytz.timezone("Asia/Manila")
    now_pht = datetime.now(pht).strftime("%Y-%m-%d %H:%M PHT")
    print(f"\n{'='*60}")
    print(f"  CSM RUN: {now_pht}")
    print(f"{'='*60}\n")

    try:
        signals, volatility = fetch_all_data()
        scores  = calc_scores(signals)
        ranked  = rank_currencies(scores)

        msg1 = build_message_1(scores, ranked, volatility, pair=PAIR)
        msg2 = build_message_2(scores, volatility)

        print("\n── Message 1 Preview ────────────────────────────────")
        print(msg1)
        print(f"\n  [Length: {len(msg1)} chars]")
        print("\n── Message 2 Preview ────────────────────────────────")
        print(msg2)
        print(f"\n  [Length: {len(msg2)} chars]")
        print("─────────────────────────────────────────────────────\n")

        if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
            print("⚠  Telegram not configured — skipping send.")
        else:
            for idx, chat_id in enumerate(TELEGRAM_CHANNELS, 1):
                print(f"\n📡 Sending to channel {idx}/{len(TELEGRAM_CHANNELS)}: {chat_id}")
                send_telegram(msg1, TELEGRAM_BOT_TOKEN, chat_id)
                send_telegram(msg2, TELEGRAM_BOT_TOKEN, chat_id)
            print(f"\n✅ Broadcast complete — {len(TELEGRAM_CHANNELS)} channel(s) notified.")

    except Exception:
        print("\n" + "!" * 60)
        print("  FATAL ERROR — full traceback below:")
        print("!" * 60)
        traceback.print_exc()

# ──────────────────────────────────────────────
# SCHEDULER — PHT times: 06:00 10:00 14:00 18:00 22:00
# ──────────────────────────────────────────────
def run_scheduler():
    """
    Runs the bot on a fixed PHT schedule.
    Converts PHT → UTC for the schedule library (which uses local time).
    If your server is already in UTC, PHT = UTC+8, so:
      06:00 PHT = 22:00 UTC (previous day)  → scheduled as 22:00
      10:00 PHT = 02:00 UTC
      14:00 PHT = 06:00 UTC
      18:00 PHT = 10:00 UTC
      22:00 PHT = 14:00 UTC
    """
    print("🕐 Scheduler started — PHT broadcast times: 06:00 10:00 14:00 18:00 22:00")
    print("   (Server runs in UTC — converted automatically)\n")

    # PHT to UTC offset = -8 hours
    schedule.every().day.at("22:00").do(run_csm)  # 06:00 PHT
    schedule.every().day.at("02:00").do(run_csm)  # 10:00 PHT
    schedule.every().day.at("06:00").do(run_csm)  # 14:00 PHT
    schedule.every().day.at("10:00").do(run_csm)  # 18:00 PHT
    schedule.every().day.at("14:00").do(run_csm)  # 22:00 PHT

    while True:
        schedule.run_pending()
        time.sleep(30)

# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    init_logging()
    print("  Currency Strength Meter — yFinance Edition v10")
    print("  EMA20 · ATR14 · BB20 · Big Money Radar · 28-Pair Bias Scan")
    print("  Data source: Yahoo Finance (no MT5 required)")
    print("=" * 60)

    # Check for --once flag for single manual run
    if "--once" in sys.argv:
        print("  Mode: SINGLE RUN (--once flag detected)\n")
        run_csm()
    else:
        print("  Mode: SCHEDULER (5x daily PHT broadcasts)\n")
        # Run once immediately on startup, then schedule
        run_csm()
        run_scheduler()

if __name__ == "__main__":
    main()
