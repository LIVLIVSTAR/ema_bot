import os, time, math, json, requests
from datetime import datetime, timezone
import pandas as pd

# ===== ENV =====
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID   = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
PAIRS_ENV          = os.getenv("PAIRS", "BTCUSDT,ETHUSDT").strip()
TIMEFRAME          = os.getenv("TIMEFRAME", "1m").strip()  # 1m / 5m / 15m ...
INTERVAL_MINUTES   = int(os.getenv("INTERVAL_MINUTES", "60"))
SEND_RELAX         = (os.getenv("SEND_RELAX_IF_NO_TOUCHES", "1").lower() in ("1","true","yes"))

# ===== Helpers =====
def parse_pairs(raw: str):
    # split by comma/semicolon/space and dedupe
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
        print("ERROR: Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=20)
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
            # пример: {"code":-1121,"msg":"Invalid symbol."}
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
    df = get_klines(symbol, TIMEFRAME, limit=250)
    if df is None or len(df) < 210:
        return None  # недостаточно данных или ошибка

    closes = df['c']
    ema50  = closes.ewm(span=50, adjust=False).mean().iloc[-1]
    ema200 = closes.ewm(span=200, adjust=False).mean().iloc[-1]
    price  = closes.iloc[-1]
    prev   = closes.iloc[-2]

    # касание/пересечение
    touched_50  = math.isclose(price, ema50, rel_tol=0.0005) or (price >= ema50 and prev < ema50) or (price <= ema50 and prev > ema50)
    touched_200 = math.isclose(price, ema200, rel_tol=0.0005) or (price >= ema200 and prev < ema200) or (price <= ema200 and prev > ema200)

    res = []
    if touched_50:
        res.append((symbol, price, "ema50"))
    if touched_200:
        res.append((symbol, price, "ema200"))
    return res

def one_report():
    messages = []
    for sym in PAIRS:
        try:
            hits = check_touch(sym)
            if not hits:
                continue
            for (symbol, price, which) in hits:
                messages.append(f"{symbol}, price {price:.6f}, {which}")
        except Exception as e:
            print(f"check_touch exception for {sym}:", repr(e))

    if messages:
        text = "EMA touches report (" + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") + ")\n" + "\n".join(messages) + "\nSponsored by www.livlivstar.com"
        tg_send(text)
    else:
        if SEND_RELAX:
            tg_send("Just Relax / Sponsored by www.livlivstar.com")

def main():
    print(f"Bot started… Pairs={PAIRS} interval={INTERVAL_MINUTES}m timeframe={TIMEFRAME} send_relax={SEND_RELAX}")
    # startup ping
    if tg_send("ema_bot is online ✅ (startup ping)"):
        print("Startup ping sent")
    else:
        print("Startup ping FAILED — check CHAT_ID (для каналов должен начинаться с -100...) и права бота")

    period = INTERVAL_MINUTES * 60
    t0 = time.monotonic()
    # сразу первый отчёт при старте (чтобы убедиться, что цикл живой)
    try:
        one_report()
    except Exception as e:
        print("Initial report exception:", repr(e))

    while True:
        now = time.monotonic()
        # ждём до конца периода
        remaining = period - ((now - t0) % period)
        time.sleep(min(max(remaining, 1), period))
        try:
            one_report()
        except Exception as e:
            print("Report exception:", repr(e))

if __name__ == "__main__":
    main()
