"""
Currency Strength Meter — yFinance Edition v10
===============================================
Message 1: CSM Scoreboard (current)
Message 2: Historical Scoreboard — identical layout to Message 1
           Shows 4 snapshots (3 past hours + now) each with full
           H1 / H4 / D1 scores recalculated from historical bars.
"""

import os
import sys
import time
import traceback
import requests
import pytz
import schedule
import yfinance as yf
import pandas as pd
from datetime import datetime, timezone, timedelta

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8781053968:AAE2f3-DtO7Cl6r41lGkZ97QqKPlu6y2GjE")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "-1003786027315") #MCROTRADES
TELEGRAM_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2", "-1003946103311") #Circle of Traders
TELEGRAM_CHANNELS  = [cid for cid in [TELEGRAM_CHAT_ID, TELEGRAM_CHAT_ID_2] if cid]

EMA_LEN  = 20
ATR_LEN  = 14
BB_LEN   = 20
BB_MULT  = 2.0
ATR_AVG  = 20
ATR_LOW  = 0.80
ATR_HIGH = 1.20

# ──────────────────────────────────────────────
# 28-PAIR UNIVERSE
# ──────────────────────────────────────────────
PAIRS = [
    "EURUSD","GBPUSD","AUDUSD","NZDUSD",
    "USDJPY","USDCHF","USDCAD",
    "EURGBP","EURAUD","EURNZD","EURJPY","EURCHF","EURCAD",
    "GBPAUD","GBPNZD","GBPJPY","GBPCHF","GBPCAD",
    "AUDNZD","AUDJPY","AUDCHF","AUDCAD",
    "NZDJPY","NZDCHF","NZDCAD",
    "CHFJPY","CADJPY","CADCHF",
]

CURRENCIES = ["EUR","USD","GBP","JPY","CHF","AUD","NZD","CAD"]

FLAGS = {
    "EUR":"🇪🇺","USD":"🇺🇸","GBP":"🇬🇧","JPY":"🇯🇵",
    "CHF":"🇨🇭","AUD":"🇦🇺","NZD":"🇳🇿","CAD":"🇨🇦",
}

MAX_PAIRS = {c: 7 for c in CURRENCIES}

# ──────────────────────────────────────────────
# INDICATOR FUNCTIONS
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
    return closes[-1] > ema if ema is not None else False

def calc_atr(highs, lows, closes, length=14):
    if len(highs) < length + 1:
        return None
    tr_values = []
    for i in range(1, len(highs)):
        tr_values.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1])
        ))
    if len(tr_values) < length:
        return None
    alpha = 1.0 / length
    atr = tr_values[0]
    for tr in tr_values[1:]:
        atr = alpha * tr + (1 - alpha) * atr
    return atr

def calc_atr_series(highs, lows, closes, length=14):
    if len(highs) < length + 1:
        return []
    tr_values = []
    for i in range(1, len(highs)):
        tr_values.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1])
        ))
    if len(tr_values) < length:
        return []
    alpha = 1.0 / length
    atr = tr_values[0]
    series = [atr]
    for tr in tr_values[1:]:
        atr = alpha * tr + (1 - alpha) * atr
        series.append(atr)
    return series

def calc_std(values, length):
    if len(values) < length:
        return None
    recent = values[-length:]
    mean = sum(recent) / length
    return (sum((x - mean) ** 2 for x in recent) / length) ** 0.5

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
        bbw = ((mid + mult*std) - (mid - mult*std)) / mid * 100
        bbw_values.append(bbw)
    if not bbw_values:
        return None, False
    latest     = bbw_values[-1]
    recent_min = min(bbw_values[-length:]) if len(bbw_values) >= length else min(bbw_values)
    return round(latest, 2), latest <= recent_min * 1.05

# ──────────────────────────────────────────────
# DATA FETCH
# ──────────────────────────────────────────────
def yf_symbol(pair):
    return f"{pair}=X"

def fetch_raw_h1(pair, retries=3):
    """Fetch raw H1 DataFrame with timezone-aware index."""
    symbol = yf_symbol(pair)
    for attempt in range(retries):
        try:
            df = yf.Ticker(symbol).history(interval="1h", period="60d", auto_adjust=True)
            if df is None or df.empty:
                time.sleep(1)
                continue
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize("UTC")
            else:
                df.index = df.index.tz_convert("UTC")
            return df
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                print(f"  ⚠ {pair} H1 fetch failed: {e}")
    return None

def fetch_raw_d1(pair, retries=3):
    """Fetch raw D1 DataFrame."""
    symbol = yf_symbol(pair)
    for attempt in range(retries):
        try:
            df = yf.Ticker(symbol).history(interval="1d", period="2y", auto_adjust=True)
            if df is None or df.empty:
                time.sleep(1)
                continue
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize("UTC")
            else:
                df.index = df.index.tz_convert("UTC")
            return df
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                print(f"  ⚠ {pair} D1 fetch failed: {e}")
    return None

def get_signals_at(pair, h1_df, d1_df, cutoff_utc):
    """
    Calculate H1, H4, D1 EMA20 bull/bear signals for a pair
    using only bars that closed AT OR BEFORE cutoff_utc.
    """
    result = {"H1": False, "H4": False, "D1": False}

    # ── H1 ──
    if h1_df is not None and not h1_df.empty:
        sliced = h1_df[h1_df.index <= cutoff_utc]
        if len(sliced) >= EMA_LEN:
            result["H1"] = bull_flag_ema(sliced["Close"].tolist(), EMA_LEN)

    # ── H4 (resample H1 → H4) ──
    if h1_df is not None and not h1_df.empty:
        sliced = h1_df[h1_df.index <= cutoff_utc]
        if len(sliced) >= 4:
            h4 = sliced.resample("4h").agg({
                "Open":"first","High":"max","Low":"min","Close":"last"
            }).dropna()
            if len(h4) >= EMA_LEN:
                result["H4"] = bull_flag_ema(h4["Close"].tolist(), EMA_LEN)

    # ── D1 ──
    if d1_df is not None and not d1_df.empty:
        sliced = d1_df[d1_df.index <= cutoff_utc]
        if len(sliced) >= EMA_LEN:
            result["D1"] = bull_flag_ema(sliced["Close"].tolist(), EMA_LEN)

    return result

def fetch_all_raw():
    """Fetch H1 and D1 DataFrames for all 28 pairs."""
    h1_data = {}
    d1_data = {}
    print(f"Fetching raw data for {len(PAIRS)} pairs...")
    for i, pair in enumerate(PAIRS, 1):
        print(f"  [{i:>2}/{len(PAIRS)}] {pair}")
        h1_data[pair] = fetch_raw_h1(pair)
        d1_data[pair] = fetch_raw_d1(pair)
    return h1_data, d1_data

# ──────────────────────────────────────────────
# SCORE CALCULATION
# ──────────────────────────────────────────────
def b2s(b):
    return 1 if b else -1

def calc_scores(signals):
    scores = {}
    for tf in ("H1","H4","D1"):
        def g(p, _tf=tf):
            return b2s(signals[p][_tf])
        tf_vals = [
            (g("EURUSD")+g("EURGBP")+g("EURAUD")+g("EURNZD")+g("EURJPY")+g("EURCHF")+g("EURCAD")),
            (-g("EURUSD")-g("GBPUSD")-g("AUDUSD")-g("NZDUSD")+g("USDJPY")+g("USDCHF")+g("USDCAD")),
            (g("GBPUSD")-g("EURGBP")+g("GBPAUD")+g("GBPNZD")+g("GBPJPY")+g("GBPCHF")+g("GBPCAD")),
            (-g("USDJPY")-g("EURJPY")-g("GBPJPY")-g("AUDJPY")-g("NZDJPY")-g("CHFJPY")-g("CADJPY")),
            (-g("USDCHF")-g("EURCHF")-g("GBPCHF")+g("CHFJPY")-g("AUDCHF")-g("NZDCHF")-g("CADCHF")),
            (g("AUDUSD")-g("EURAUD")-g("GBPAUD")+g("AUDNZD")+g("AUDJPY")+g("AUDCHF")+g("AUDCAD")),
            (g("NZDUSD")-g("EURNZD")-g("GBPNZD")-g("AUDNZD")+g("NZDJPY")+g("NZDCHF")+g("NZDCAD")),
            (-g("USDCAD")-g("EURCAD")-g("GBPCAD")-g("AUDCAD")-g("NZDCAD")+g("CADJPY")+g("CADCHF")),
        ]
        for ccy, val in zip(CURRENCIES, tf_vals):
            scores.setdefault(ccy, {})[tf] = val

    for ccy in CURRENCIES:
        s = scores[ccy]
        s["total"] = s["H1"]*1 + s["H4"]*2 + s["D1"]*3
        mx = MAX_PAIRS[ccy] * 6
        s["norm"] = round(s["total"] / mx * 10) if mx else 0

    return scores

# ──────────────────────────────────────────────
# DISPLAY HELPERS
# ──────────────────────────────────────────────
def rank_currencies(scores):
    return sorted(CURRENCIES, key=lambda c: scores[c]["norm"], reverse=True)

def alignment_label(ccy, scores):
    s  = scores[ccy]
    d  = 1 if s["norm"] >= 0 else -1
    ag = sum(1 for tf in ("H1","H4","D1") if s[tf] * d > 0)
    return ["✗ 0/3","⚠ 1/3","⚡ 2/3","✅ 3/3"][ag]

def bias_label(norm):
    if norm > 0: return "STRONG ▲"
    if norm < 0: return "WEAK ▼"
    return "NEUTRAL"

def bar(norm):
    n = min(10, max(0, abs(norm)))
    return "█" * n + "░" * (10 - n)

def delta_str(curr, prev):
    if prev is None:
        return "  —  "
    d = curr - prev
    if d > 0: return f"▲+{d}"
    if d < 0: return f"▼{d}"
    return "→ 0"

# ──────────────────────────────────────────────
# SCOREBOARD FORMATTER (identical layout for both messages)
# ──────────────────────────────────────────────
def fmt_scoreboard(scores, ranked, title, prev_scores=None):
    """
    Formats a scoreboard block identical to Message 1.
    If prev_scores provided, adds a DELTA column.
    """
    m = []
    m.append(title)
    m.append("```")
    if prev_scores:
        m.append(f"{'#':<3} {'CCY':<6} {'H1':>4} {'H4':>4} {'D1':>4} {'SCR':>5}  {'STRENGTH':<10} {'ALN':<6} {'DELTA':<7} BIAS")
        m.append("─" * 75)
    else:
        m.append(f"{'#':<3} {'CCY':<6} {'H1':>4} {'H4':>4} {'D1':>4} {'SCR':>5}  {'STRENGTH':<10} {'ALN':<6} BIAS")
        m.append("─" * 66)

    for rank, ccy in enumerate(ranked, 1):
        s      = scores[ccy]
        prefix = "▶" if rank == 1 else ("◀" if rank == 8 else " ")
        prev   = prev_scores[ccy]["norm"] if prev_scores else None
        delt   = delta_str(s["norm"], prev) if prev_scores else ""

        if prev_scores:
            m.append(
                f"{prefix}{rank:<2} {FLAGS[ccy]}{ccy:<4} "
                f"{s['H1']:>+4} {s['H4']:>+4} {s['D1']:>+4} "
                f"{s['norm']:>+5}  {bar(s['norm']):<10} "
                f"{alignment_label(ccy, scores):<6} {delt:<7} {bias_label(s['norm'])}"
            )
        else:
            m.append(
                f"{prefix}{rank:<2} {FLAGS[ccy]}{ccy:<4} "
                f"{s['H1']:>+4} {s['H4']:>+4} {s['D1']:>+4} "
                f"{s['norm']:>+5}  {bar(s['norm']):<10} "
                f"{alignment_label(ccy, scores):<6} {bias_label(s['norm'])}"
            )
    m.append("```")
    return "\n".join(m)

# ──────────────────────────────────────────────
# MESSAGE BUILDERS
# ──────────────────────────────────────────────
def build_message_1(scores, ranked):
    pht     = pytz.timezone("Asia/Manila")
    now_pht = datetime.now(pht).strftime("%Y-%m-%d %H:%M PHT")
    m = []
    m += [
        f"⚡ *CURRENCY STRENGTH METER v10*",
        f"EMA{EMA_LEN} · ATR{ATR_LEN} · BB{BB_LEN}",
        f"`{now_pht}`", "",
    ]
    m.append(fmt_scoreboard(scores, ranked, "📊 *CURRENT SCOREBOARD*"))
    return "\n".join(m)

def build_message_2(snapshots):
    pht     = pytz.timezone("Asia/Manila")
    now_pht = datetime.now(pht).strftime("%Y-%m-%d %H:%M PHT")
    m = []
    m += [
        f"📊 *CSM SCORE HISTORY*",
        f"`{now_pht}`",
        "_Showing last 4 hourly snapshots with score delta_",
        "",
    ]

    prev_scores = None
    for label, scores in snapshots:
        ranked = rank_currencies(scores)
        m.append(fmt_scoreboard(scores, ranked, label, prev_scores))
        m.append("")
        prev_scores = scores

    m.append("_▲ Rising · ▼ Falling · →0 Unchanged_")
    return "\n".join(m)

# ──────────────────────────────────────────────
# TELEGRAM SEND
# ──────────────────────────────────────────────
def send_telegram(message, token, chat_id):
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    resp    = requests.post(url, json=payload, timeout=15)
    if resp.ok:
        print("✅ Telegram sent.")
    else:
        print(f"❌ Telegram error {resp.status_code}: {resp.text}")
    return resp

# ──────────────────────────────────────────────
# CORE RUN
# ──────────────────────────────────────────────
def run_csm():
    pht     = pytz.timezone("Asia/Manila")
    now_pht = datetime.now(pht).strftime("%Y-%m-%d %H:%M PHT")
    now_utc = datetime.now(timezone.utc)

    print(f"\n{'='*60}")
    print(f"  CSM RUN: {now_pht}")
    print(f"{'='*60}\n")

    try:
        # Step 1: Fetch all raw data once
        h1_data, d1_data = fetch_all_raw()

        # Step 2: Build current signals from latest bar
        print("\nCalculating current scores...")
        current_signals = {}
        for pair in PAIRS:
            current_signals[pair] = get_signals_at(pair, h1_data[pair], d1_data[pair], now_utc)

        current_scores = calc_scores(current_signals)
        current_ranked = rank_currencies(current_scores)

        # Step 3: Build 4 historical snapshots (3h ago, 2h ago, 1h ago, now)
        print("\nCalculating historical snapshots...")
        snapshots = []
        for hours_back in [3, 2, 1, 0]:
            cutoff = now_utc - timedelta(hours=hours_back)
            label_pht = (datetime.now(pht) - timedelta(hours=hours_back)).strftime("%H:%M PHT")

            if hours_back == 0:
                label = f"🕐 *{label_pht} — NOW*"
            else:
                label = f"🕐 *{label_pht}*"

            snap_signals = {}
            for pair in PAIRS:
                snap_signals[pair] = get_signals_at(pair, h1_data[pair], d1_data[pair], cutoff)

            snap_scores = calc_scores(snap_signals)
            snapshots.append((label, snap_scores))
            print(f"  ✓ {label_pht} snapshot done")

        # Step 4: Build messages
        msg1 = build_message_1(current_scores, current_ranked)
        msg2 = build_message_2(snapshots)

        print(f"\n── Message 1 [{len(msg1)} chars] ──")
        print(msg1)
        print(f"\n── Message 2 [{len(msg2)} chars] ──")
        print(msg2)

        # Step 5: Broadcast
        for idx, chat_id in enumerate(TELEGRAM_CHANNELS, 1):
            print(f"\n📡 Sending to channel {idx}: {chat_id}")
            send_telegram(msg1, TELEGRAM_BOT_TOKEN, chat_id)
            send_telegram(msg2, TELEGRAM_BOT_TOKEN, chat_id)

        print(f"\n✅ Broadcast complete — {len(TELEGRAM_CHANNELS)} channel(s).")

    except Exception:
        print("\n" + "!"*60)
        print("  FATAL ERROR:")
        print("!"*60)
        traceback.print_exc()

# ──────────────────────────────────────────────
# SCHEDULER & MAIN
# ──────────────────────────────────────────────
def run_scheduler():
    print("🕐 Scheduler — PHT: 06:00 10:00 14:00 18:00 22:00\n")
    schedule.every().day.at("22:00").do(run_csm)  # 06:00 PHT
    schedule.every().day.at("02:00").do(run_csm)  # 10:00 PHT
    schedule.every().day.at("06:00").do(run_csm)  # 14:00 PHT
    schedule.every().day.at("10:00").do(run_csm)  # 18:00 PHT
    schedule.every().day.at("14:00").do(run_csm)  # 22:00 PHT
    while True:
        schedule.run_pending()
        time.sleep(30)

def main():
    print("  Currency Strength Meter — yFinance Edition v10")
    print("  EMA20 · ATR14 · BB20 · Historical Scoreboard")
    print("=" * 60)
    if "--once" in sys.argv:
        print("  Mode: SINGLE RUN\n")
        run_csm()
    else:
        print("  Mode: SCHEDULER\n")
        run_csm()
        run_scheduler()

if __name__ == "__main__":
    main()
