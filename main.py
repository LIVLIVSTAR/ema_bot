import os, time, math, requests
from datetime import datetime, timezone
import pandas as pd

# ===== ENV =====
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID   = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
PAIRS_ENV          = os.getenv("PAIRS", "BTCUSDT,ETHUSDT").strip()
INTERVAL_MINUTES   = int(os.getenv("INTERVAL_MINUTES", "60"))
SEND_RELAX         = os.getenv("SEND_RELAX_IF_NO_TOUCHES", "1").lower() in ("1","true","yes")
CHUNK_SIZE         = int(os.getenv("CHUNK_SIZE", "28"))  # ~28 lines per message to keep under 4096 chars

# Fixed monitors per your request:
# (interval, ema_span, label)
MONITORS = [
    ("1d", 200, "ema200"),
    ("1w",  50, "ema50"),
    ("1w", 200, "ema200"),
]

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
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram config missing")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=20
        )
        if r.status_code != 200:
            print(f"Telegram send failed [{r.status_code}]: {r.text}")
            return False
        return True
    except Exception as e:
        print("Telegram exception:", repr(e))
        return False

def get_klines(symbol: str, interval: str, limit: int):
    url = "https://api.binance.com/api/v3/klines"
    try:
        r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=20)
        data = r.json()
        if not isinstance(data, list):
            print(f"Binance error for {symbol} [{interval}]: {data}")
            return None
        df = pd.DataFrame(data, columns=[
            'time','o','h','l','c','v','close_time','q','n','taker_base','taker_quote','ignore'
        ])
        df['c'] = df['c'].astype(float)
        return df
    except Exception as e:
        print(f"Klines exception for {symbol} [{interval}]:", repr(e))
        return None

def ema_touch_for(symbol: str, interval: str, span: int, rel_tol=0.0005):
    """Return ('touched', price) if last close touches/crosses EMA(span) on the given interval."""
    # enough history: 210 for ema200 is safe; weekly needs fewer requests but keep generous
    limit = 260 if interval in ("1w", "1d") else 250
    df = get_klines(symbol, interval, limit=limit)
    if df is None or len(df) < span + 5:
        return None

    closes = df['c']
    ema    = closes.ewm(span=span, adjust=False).mean().iloc[-1]
    price  = closes.iloc[-1]
    prev   = closes.iloc[-2]

    touched = (
        math.isclose(price, ema, rel_tol=rel_tol) or
        (price >= ema and prev < ema) or
        (price <= ema and prev > ema)
    )
    if touched:
        return price
    return None

def one_report():
    lines = []
    for sym in PAIRS:
        for (interval, span, label) in MONITORS:
            try:
                pr = ema_touch_for(sym, interval, span)
                if pr is not None:
                    lines.append(f"{sym}, price {pr:.6f}, {label} [{interval}]")
            except Exception as e:
                print(f"touch check error {sym} {interval} {span}:", repr(e))

    if lines:
        # split into chunks to avoid Telegram 4096-char limit
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        for i in range(0, len(lines), CHUNK_SIZE):
            part = "\n".join(lines[i:i+CHUNK_SIZE])
            msg = f"EMA touches report ({ts})\n{part}\nSponsored by www.livlivstar.com"
            tg_send(msg)
    else:
        if SEND_RELAX:
            tg_send("Just Relax / Sponsored by www.livlivstar.com")

def main():
    print(f"Bot started… pairs={len(PAIRS)} interval={INTERVAL_MINUTES}m monitors={MONITORS} relax={SEND_RELAX}")
    tg_send("ema_bot is online ✅ (startup ping)")
    period = max(60, INTERVAL_MINUTES * 60)  # at least 60s
    t0 = time.monotonic()

    # first report immediately
    one_report()

    while True:
        now = time.monotonic()
        remaining = period - ((now - t0) % period)
        time.sleep(min(max(remaining, 1), period))
        one_report()

if __name__ == "__main__":
    main()
