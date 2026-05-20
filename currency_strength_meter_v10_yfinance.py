"""
Currency Strength Meter — yFinance Edition v10
===============================================
Message 1: CSM Scoreboard (current)
Message 2: Historical Scoreboard (past 3 hours + current = 4 snapshots)

Removed: EURUSD pair analysis, Best Setup, Big Money Radar, 28-pair scan
Added:   Hourly score history with delta arrows
"""

import os
import sys
import time
import traceback
import requests
import pytz
import schedule
import yfinance as yf
from datetime import datetime, timezone, timedelta

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8781053968:AAE2f3-DtO7Cl6r41lGkZ97QqKPlu6y2GjE")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "6579085899")
TELEGRAM_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2", "")

TELEGRAM_CHANNELS  = [cid for cid in [TELEGRAM_CHAT_ID, TELEGRAM_CHAT_ID_2] if cid]

EMA_LEN  = 20
ATR_LEN  = 14
BB_LEN   = 20
BB_MULT  = 2.0
ATR_AVG  = 20
ATR_LOW  = 0.80
ATR_HIGH = 1.20

BM_D1_THRESH = 4
BM_H4_THRESH = 3
BM_H1_THRESH = 2

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
# DATA FETCH — yfinance
# ──────────────────────────────────────────────
def yf_symbol(pair):
    return f"{pair}=X"

def fetch_ohlc_yf(pair, tf, retries=3):
    symbol = yf_symbol(pair)
    if tf == "H1":
        interval, period = "1h", "60d"
    elif tf == "H4":
        interval, period = "1h", "60d"
    else:
        interval, period = "1d", "2y"

    for attempt in range(retries):
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(interval=interval, period=period, auto_adjust=True)
            if df is None or df.empty:
                time.sleep(1)
                continue
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

def fetch_h1_series(pair, retries=3):
    """
    Fetch raw H1 OHLC as a pandas DataFrame with datetime index.
    Used for historical score snapshots.
    """
    symbol = yf_symbol(pair)
    for attempt in range(retries):
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(interval="1h", period="30d", auto_adjust=True)
            if df is None or df.empty:
                time.sleep(1)
                continue
            return df
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                print(f"  ⚠ {pair} H1 series fetch failed: {e}")
    return None

def fetch_all_data():
    """Fetch all 28 pairs across H1/H4/D1. Returns (signals, volatility)."""
    signals    = {}
    volatility = {}
    print(f"Fetching market data for {len(PAIRS)} pairs from Yahoo Finance...")

    for i, pair in enumerate(PAIRS, 1):
        print(f"  [{i:>2}/{len(PAIRS)}] {pair}", end="  ")
        signals[pair] = {}

        for tf in ("H1", "H4", "D1"):
            data = fetch_ohlc_yf(pair, tf)
            if data and len(data["close"]) >= EMA_LEN:
                bull = bull_flag_ema(data["close"], EMA_LEN)
            else:
                bull = False
            signals[pair][tf] = bull

        data_d1 = fetch_ohlc_yf(pair, "D1")
        if data_d1 and len(data_d1["close"]) >= max(ATR_LEN, BB_LEN * 2):
            atr_d1    = calc_atr(data_d1["high"], data_d1["low"], data_d1["close"], ATR_LEN)
            series_d1 = calc_atr_series(data_d1["high"], data_d1["low"], data_d1["close"], ATR_LEN)
            bbw_val, is_sq = calc_bbw(data_d1["close"], BB_LEN, BB_MULT)
        else:
            atr_d1, series_d1, bbw_val, is_sq = None, [], None, False

        ratio_d1, regime_d1, emoji_d1 = atr_regime(atr_d1, series_d1, ATR_AVG)

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
# SCORE CALCULATION
# ──────────────────────────────────────────────
def b2s(b):
    return 1 if b else -1

def calc_scores(signals):
    scores = {}
    for tf in ("H1", "H4", "D1"):
        def g(p, _tf=tf):
            return b2s(signals[p][_tf])

        tf_vals = [
            (g("EURUSD") + g("EURGBP") + g("EURAUD") + g("EURNZD")
             + g("EURJPY") + g("EURCHF") + g("EURCAD")),
            (-g("EURUSD") - g("GBPUSD") - g("AUDUSD") - g("NZDUSD")
             + g("USDJPY") + g("USDCHF") + g("USDCAD")),
            (g("GBPUSD") - g("EURGBP") + g("GBPAUD") + g("GBPNZD")
             + g("GBPJPY") + g("GBPCHF") + g("GBPCAD")),
            (-g("USDJPY") - g("EURJPY") - g("GBPJPY") - g("AUDJPY")
             - g("NZDJPY") - g("CHFJPY") - g("CADJPY")),
            (-g("USDCHF") - g("EURCHF") - g("GBPCHF") + g("CHFJPY")
             - g("AUDCHF") - g("NZDCHF") - g("CADCHF")),
            (g("AUDUSD") - g("EURAUD") - g("GBPAUD") + g("AUDNZD")
             + g("AUDJPY") + g("AUDCHF") + g("AUDCAD")),
            (g("NZDUSD") - g("EURNZD") - g("GBPNZD") - g("AUDNZD")
             + g("NZDJPY") + g("NZDCHF") + g("NZDCAD")),
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
# HISTORICAL SCORE ENGINE
# ──────────────────────────────────────────────
def calc_signals_at_bar(all_h1_data, bar_index):
    """
    Calculate EMA20 bull/bear signal for each pair at a specific H1 bar index.
    bar_index: negative index — e.g. -1 = latest, -2 = 1 hour ago, etc.
    Uses only D1/H4 from current fetch (stable), only H1 changes per snapshot.
    """
    signals = {}
    for pair in PAIRS:
        df = all_h1_data.get(pair)
        signals[pair] = {}

        if df is not None and len(df) >= abs(bar_index) + EMA_LEN:
            # Slice closes up to this bar
            closes = df["Close"].tolist()[:len(df) + bar_index + 1] if bar_index < -1 else df["Close"].tolist()
            bull_h1 = bull_flag_ema(closes, EMA_LEN)
        else:
            bull_h1 = False

        signals[pair]["H1"] = bull_h1
        # H4 and D1 use current signals (they don't change hour by hour significantly)
        signals[pair]["H4"] = False  # placeholder — overridden below
        signals[pair]["D1"] = False  # placeholder — overridden below

    return signals

def fetch_historical_scores(current_signals):
    """
    Fetch H1 data for all pairs and compute CSM scores for the past 4 hours.
    Returns list of (label, scores_dict) for 4 snapshots oldest → newest.
    """
    pht = pytz.timezone("Asia/Manila")
    now_pht = datetime.now(pht)

    print("\nFetching H1 history for score snapshots...")
    all_h1_data = {}
    for i, pair in enumerate(PAIRS, 1):
        df = fetch_h1_series(pair)
        if df is not None and not df.empty:
            # Convert index to PHT
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize("UTC")
            df.index = df.index.tz_convert(pht)
        all_h1_data[pair] = df

    # Build 4 snapshots: -3h, -2h, -1h, now
    snapshots = []
    for hours_back in [3, 2, 1, 0]:
        target_time = now_pht - timedelta(hours=hours_back)
        label_time  = target_time.strftime("%H:%M PHT")
        label       = f"🕐 *{label_time}*" if hours_back > 0 else f"🕐 *{label_time} (NOW)*"

        # Build signals for this snapshot
        snap_signals = {}
        for pair in PAIRS:
            df = all_h1_data.get(pair)
            snap_signals[pair] = {}

            if df is not None and not df.empty:
                # Get closes up to target_time
                df_slice = df[df.index <= target_time]
                if len(df_slice) >= EMA_LEN:
                    closes = df_slice["Close"].tolist()
                    bull_h1 = bull_flag_ema(closes, EMA_LEN)
                else:
                    bull_h1 = False
            else:
                bull_h1 = False

            snap_signals[pair]["H1"] = bull_h1
            # Use current H4/D1 signals (stable across 4 hours)
            snap_signals[pair]["H4"] = current_signals[pair]["H4"]
            snap_signals[pair]["D1"] = current_signals[pair]["D1"]

        scores = calc_scores(snap_signals)
        snapshots.append((label, scores))

    return snapshots

# ──────────────────────────────────────────────
# DISPLAY HELPERS
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

def bar(norm):
    n = min(10, max(0, abs(norm)))
    return "█" * n + "░" * (10 - n)

def delta_arrow(current, previous):
    """Show score change with arrow and value."""
    if previous is None:
        return ""
    diff = current - previous
    if diff > 0:
        return f" ▲+{diff}"
    elif diff < 0:
        return f" ▼{diff}"
    else:
        return f" →0"

# ──────────────────────────────────────────────
# MESSAGE BUILDERS
# ──────────────────────────────────────────────
def build_message_1(scores, ranked):
    """Message 1: Current CSM Scoreboard."""
    pht     = pytz.timezone("Asia/Manila")
    now_pht = datetime.now(pht).strftime("%Y-%m-%d %H:%M PHT")
    m       = []

    m += [
        f"⚡ *CURRENCY STRENGTH METER v10*",
        f"EMA{EMA_LEN} · ATR{ATR_LEN} · BB{BB_LEN}",
        f"`{now_pht}`", "",
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
    m.append("```")

    return "\n".join(m)


def build_message_2(snapshots):
    """
    Message 2: Historical Scoreboard
    Shows 4 hourly snapshots with delta arrows between each hour.
    """
    pht     = pytz.timezone("Asia/Manila")
    now_pht = datetime.now(pht).strftime("%Y-%m-%d %H:%M PHT")
    m       = []

    # Header
    start_label = snapshots[0][0].replace("🕐 *", "").replace("*", "").strip()
    end_label   = snapshots[-1][0].replace("🕐 *", "").replace("* (NOW)*", "").replace("*", "").strip()

    m += [
        f"📊 *CSM SCORE HISTORY*",
        f"_{start_label} → {end_label}_",
        f"`{now_pht}`",
        "",
    ]

    prev_scores = None

    for label, scores in snapshots:
        ranked = rank_currencies(scores)
        m.append(label)
        m.append("```")
        m.append(f"{'CCY':<6} {'SCR':>5}  {'STRENGTH':<10}  DELTA  BIAS")
        m.append("─" * 48)

        for ccy in ranked:
            s    = scores[ccy]
            norm = s["norm"]
            prev = prev_scores[ccy]["norm"] if prev_scores else None
            delt = delta_arrow(norm, prev)
            m.append(
                f"{FLAGS[ccy]}{ccy:<4} "
                f"{norm:>+5}  "
                f"{bar(norm):<10}  "
                f"{delt:<7}  "
                f"{bias_label(norm)}"
            )

        m.append("```")
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
        print("✅ Telegram message sent successfully.")
    else:
        print(f"❌ Telegram error {resp.status_code}: {resp.text}")
    return resp

# ──────────────────────────────────────────────
# CORE RUN
# ──────────────────────────────────────────────
def run_csm():
    pht     = pytz.timezone("Asia/Manila")
    now_pht = datetime.now(pht).strftime("%Y-%m-%d %H:%M PHT")
    print(f"\n{'='*60}")
    print(f"  CSM RUN: {now_pht}")
    print(f"{'='*60}\n")

    try:
        # Step 1: Fetch current data
        signals, volatility = fetch_all_data()
        scores  = calc_scores(signals)
        ranked  = rank_currencies(scores)

        # Step 2: Fetch historical snapshots
        snapshots = fetch_historical_scores(signals)

        # Step 3: Build messages
        msg1 = build_message_1(scores, ranked)
        msg2 = build_message_2(snapshots)

        print("\n── Message 1 ────────────────────────────────────────")
        print(msg1)
        print(f"\n  [Length: {len(msg1)} chars]")
        print("\n── Message 2 ────────────────────────────────────────")
        print(msg2)
        print(f"\n  [Length: {len(msg2)} chars]")
        print("─────────────────────────────────────────────────────\n")

        # Step 4: Broadcast
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
# SCHEDULER
# ──────────────────────────────────────────────
def run_scheduler():
    print("🕐 Scheduler started — PHT: 06:00 10:00 14:00 18:00 22:00\n")
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
    print("  Currency Strength Meter — yFinance Edition v10")
    print("  EMA20 · ATR14 · BB20 · Historical Scoreboard")
    print("  Data source: Yahoo Finance")
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
