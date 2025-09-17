import os, time, math, requests
from datetime import datetime, timezone
import pandas as pd

# ===== ENV =====
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID   = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
PAIRS_ENV          = os.getenv("PAIRS", "BTCUSDT,ETHUSDT").strip()
TIMEFRAME          = os.getenv("TIMEFRAME", "1d").strip()  # ← по умолчанию дневной TF
INTERVAL_MINUTES   = int(os.getenv("INTERVAL_MINUTES", "60"))
SEND_RELAX         = os.getenv("SEND_RELAX_IF_NO_TOUCHES", "1").lower() in ("1","true","yes")

def parse_pairs(raw: str):
    seps = [",",";"," "]
    parts = [raw]
    for s in seps:
        parts = sum([p.split(s) for p in parts], [])
    out, seen = [], set()
    for p in parts:
        p = p.strip().upper()
        if p and p not in seen:
            seen.add(p); out.append(p)
    return out

PAIRS = parse_pairs(PAIRS_ENV)

def tg_send(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=20
        )
        if r.status_code != 200:
            print(f"Telegram send failed [{r.status_code}]: {r.text}")
            return False
        return True
    except Exception as e:
        print("Telegram exception:", repr(e))
        return False

def get_klines(symbol: str, interval: str, limit: int = 250):
    url = "https://api.binance.com/api/v3/klines"
    try:
        r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=20)
        data = r.json()
        if not isinstance(data, list):
            print(f"Binance error for {symbol}: {data}")
            return None
        df = pd.DataFrame(data, columns=[
            'time','o','h','l','c','v','close_time','q','n','taker_base','taker_quote','ignore'
        ])
        df['c'] = df['c'].astype(float)
        return df
    except Exception as e:
        print(f"Klines exception for {symbol}:", repr(e))
        return None

def check_touch(symbol: str):
    df = get_klines(symbol, TIMEFRAME, limit=250)  # ← используем дневные 1d (или что в .env)
    if df is None or len(df) < 210:
        return None

    closes = df['c']
    ema50  = closes.ewm(span=50, adjust=False).mean().iloc[-1]
    ema200 = closes.ewm(span=200, adjust=False).mean().iloc[-1]
    price  = closes.iloc[-1]
    prev   = closes.iloc[-2]

    touched_50  = math.isclose(price, ema50, rel_tol=0.0005) or (price >= ema50 and prev < ema50) or (price <= ema50 and prev > ema50)
    touched_200 = math.isclose(price, ema200, rel_tol=0.0005) or (price >= ema200 and prev < ema200) or (price <= ema200 and prev > ema200)

    res = []
    if touched_50:
        res.append((symbol, price, "ema50"))
    if touched_200:
        res.append((symbol, price, "ema200"))
    return res

def one_report():
    lines = []
    for sym in PAIRS:
        try:
            hits = check_touch(sym)
            if not hits: continue
            for (symbol, price, which) in hits:
                lines.append(f"{symbol}, price {price:.6f}, {which} [{TIMEFRAME}]")
        except Exception as e:
            print(f"check_touch exception for {sym}:", repr(e))

    if lines:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        msg = f"EMA touches report ({ts})\n" + "\n".join(lines) + "\nSponsored by www.livlivstar.com"
        tg_send(msg)
    else:
        if SEND_RELAX:
            tg_send("Just Relax / Sponsored by www.livlivstar.com")

def main():
    print(f"Bot started… Pairs={PAIRS} interval={INTERVAL_MINUTES}m timeframe={TIMEFRAME} send_relax={SEND_RELAX}")
    tg_send("ema_bot is online ✅ (startup ping)")
    period = INTERVAL_MINUTES * 60
    t0 = time.monotonic()
    one_report()  # первый отчёт сразу
    while True:
        now = time.monotonic()
        remaining = period - ((now - t0) % period)
        time.sleep(min(max(remaining, 1), period))
        one_report()

if __name__ == "__main__":
    main()
