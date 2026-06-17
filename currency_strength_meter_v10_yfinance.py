"""
Currency Strength Meter — yFinance Edition v11
===============================================
PAIR-LEVEL SCORECARD (replaces v10's currency scoreboard)

For each of the 28 pairs:
  - Strength: 0-10 gauge derived from (base_currency.norm - quote_currency.norm)
  - Signal: BUY / SELL / NEUTRAL
  - Score History: last 8 hourly strength readings
  - Transition: Aligned (consistent direction across history) / Neutral (flip-flopping)

Underlying currency scoring math (28 pairs, 8 currencies, H1/H4/D1,
EMA20 bull/bear, weighted H1x1+H4x2+D1x3, normalized -10..+10) is
UNCHANGED from v10. Only the presentation layer changed.
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
HISTORY_HOURS = 8   # how many hourly snapshots to keep per pair

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

MAX_PAIRS = {c: 7 for c in CURRENCIES}

# Pair -> (base, quote) lookup, derived directly from the symbol
def base_quote(pair):
    return pair[:3], pair[3:]

# ──────────────────────────────────────────────
# INDICATOR FUNCTIONS  (unchanged from v10)
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

# ──────────────────────────────────────────────
# DATA FETCH  (unchanged from v10)
# ──────────────────────────────────────────────
def yf_symbol(pair):
    return f"{pair}=X"

def fetch_raw_h1(pair, retries=3):
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
    result = {"H1": False, "H4": False, "D1": False}

    if h1_df is not None and not h1_df.empty:
        sliced = h1_df[h1_df.index <= cutoff_utc]
        if len(sliced) >= EMA_LEN:
            result["H1"] = bull_flag_ema(sliced["Close"].tolist(), EMA_LEN)

    if h1_df is not None and not h1_df.empty:
        sliced = h1_df[h1_df.index <= cutoff_utc]
        if len(sliced) >= 4:
            h4 = sliced.resample("4h").agg({
                "Open":"first","High":"max","Low":"min","Close":"last"
            }).dropna()
            if len(h4) >= EMA_LEN:
                result["H4"] = bull_flag_ema(h4["Close"].tolist(), EMA_LEN)

    if d1_df is not None and not d1_df.empty:
        sliced = d1_df[d1_df.index <= cutoff_utc]
        if len(sliced) >= EMA_LEN:
            result["D1"] = bull_flag_ema(sliced["Close"].tolist(), EMA_LEN)

    return result

def fetch_all_raw():
    h1_data = {}
    d1_data = {}
    print(f"Fetching raw data for {len(PAIRS)} pairs...")
    for i, pair in enumerate(PAIRS, 1):
        print(f"  [{i:>2}/{len(PAIRS)}] {pair}")
        h1_data[pair] = fetch_raw_h1(pair)
        d1_data[pair] = fetch_raw_d1(pair)
    return h1_data, d1_data

# ──────────────────────────────────────────────
# CURRENCY SCORE CALCULATION  (unchanged from v10)
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
# PAIR-LEVEL DERIVATION  (new in v11)
# ──────────────────────────────────────────────
def pair_strength(ccy_scores, pair):
    """
    Derive a pair's 0-10 strength + BUY/SELL signal from the two
    underlying currency norm scores (-10..+10 each).

    pair_diff range: -20 .. +20
    strength range:   0 .. 10   (10 = max conviction either direction)
    """
    base, quote = base_quote(pair)
    diff = ccy_scores[base]["norm"] - ccy_scores[quote]["norm"]
    strength = round((abs(diff) / 20) * 10)
    strength = max(0, min(10, strength))

    if diff > 0:
        signal = "BUY"
    elif diff < 0:
        signal = "SELL"
    else:
        signal = "NEUTRAL"

    return strength, signal, diff

def transition_label(signal_history):
    """
    signal_history: list of 'BUY'/'SELL'/'NEUTRAL' strings, oldest -> newest.
    Aligned  = no direction flips across the whole window (NEUTRAL doesn't break alignment)
    Neutral  = at least one BUY<->SELL flip somewhere in the window
    """
    directional = [s for s in signal_history if s != "NEUTRAL"]
    if len(directional) < 2:
        return "Neutral"
    first = directional[0]
    if all(s == first for s in directional):
        return "Aligned"
    return "Neutral"

# ──────────────────────────────────────────────
# DISPLAY HELPERS
# ──────────────────────────────────────────────
def strength_bar(strength):
    n = max(0, min(10, strength))
    return "█" * n + "░" * (10 - n)

SIGNAL_DOT = {"BUY": "🟢", "SELL": "🔴", "NEUTRAL": "⚪"}

# ──────────────────────────────────────────────
# MESSAGE BUILDER  (new card-per-pair format)
# ──────────────────────────────────────────────
def build_scorecard_message(pair_results, run_label):
    """
    pair_results: dict pair -> {
        "strength": int, "signal": str,
        "history": [int, ...] (8 values, oldest->newest),
        "transition": str
    }
    """
    m = []
    m.append("📊 *HOURLY SCORE UPDATE* 📊")
    m.append(f"`{run_label}`")
    m.append("")

    for pair in PAIRS:
        r = pair_results[pair]
        hist_str = " ".join(str(v) for v in r["history"])
        m.append(f"{SIGNAL_DOT[r['signal']]} *{pair}*")
        m.append(f"Signal: {r['signal']}")
        m.append(f"Strength: {r['strength']} `{strength_bar(r['strength'])}`")
        m.append(f"Score History: `{hist_str}`")
        m.append(f"Transition: {r['transition']}")
        m.append("")

    return "\n".join(m).strip()

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

def send_telegram_chunked(message, token, chat_id, max_len=3800):
    """
    Telegram caps messages at 4096 chars. 28 pair cards can exceed that,
    so split on blank-line boundaries (between pair cards) if needed.
    """
    if len(message) <= max_len:
        send_telegram(message, token, chat_id)
        return

    blocks = message.split("\n\n")
    chunk = ""
    for block in blocks:
        candidate = (chunk + "\n\n" + block) if chunk else block
        if len(candidate) > max_len:
            send_telegram(chunk, token, chat_id)
            chunk = block
        else:
            chunk = candidate
    if chunk:
        send_telegram(chunk, token, chat_id)

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

        # Step 2: Compute currency scores at each of the last HISTORY_HOURS
        # hourly cutoffs (oldest -> newest), so we can derive pair strength
        # history without re-fetching data.
        print("\nCalculating currency scores across history window...")
        hours_back_list = list(range(HISTORY_HOURS - 1, -1, -1))  # e.g. [7,6,...,1,0]
        ccy_scores_by_cutoff = []  # list of (cutoff_utc, ccy_scores), oldest->newest
        for hb in hours_back_list:
            cutoff = now_utc - timedelta(hours=hb)
            signals = {p: get_signals_at(p, h1_data[p], d1_data[p], cutoff) for p in PAIRS}
            ccy_scores_by_cutoff.append((cutoff, calc_scores(signals)))
            print(f"  ✓ snapshot t-{hb}h done")

        # Step 3: Derive per-pair strength/signal at every snapshot,
        # build 8-value history, current strength/signal, and transition.
        print("\nDeriving pair-level scorecard...")
        pair_results = {}
        for pair in PAIRS:
            history = []
            signal_history = []
            for cutoff, ccy_scores in ccy_scores_by_cutoff:
                strength, signal, _ = pair_strength(ccy_scores, pair)
                history.append(strength)
                signal_history.append(signal)

            current_strength = history[-1]
            current_signal   = signal_history[-1]
            trans = transition_label(signal_history)

            pair_results[pair] = {
                "strength": current_strength,
                "signal": current_signal,
                "history": history,
                "transition": trans,
            }

        # Step 4: Build message
        msg = build_scorecard_message(pair_results, now_pht)

        print(f"\n── Scorecard Message [{len(msg)} chars] ──")
        print(msg)

        # Step 5: Broadcast (chunked, since 28 pair cards can exceed Telegram's 4096 char cap)
        for idx, chat_id in enumerate(TELEGRAM_CHANNELS, 1):
            print(f"\n📡 Sending to channel {idx}: {chat_id}")
            send_telegram_chunked(msg, TELEGRAM_BOT_TOKEN, chat_id)

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
    print("  Currency Strength Meter — yFinance Edition v11")
    print("  EMA20 · Pair-Level Scorecard (28 pairs, 8h history)")
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
