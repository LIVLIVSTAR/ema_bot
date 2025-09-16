import os, asyncio, json, math, time, datetime as dt
from collections import deque, defaultdict

import pandas as pd
import requests
import websockets
from dotenv import load_dotenv

load_dotenv()

TG_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHATID = os.getenv("TELEGRAM_CHAT_ID")
TOUCH_EPS = float(os.getenv("TOUCH_EPS", "0.001"))   # 0.1%
RESET_EPS = float(os.getenv("RESET_EPS", "0.003"))   # 0.3%

BINANCE_API = "https://api.binance.com"
WS_MINITICK = "wss://stream.binance.com:9443/ws/!miniTicker@arr"  # все спотовые минитикеры

ALPHA200 = 2/(200+1)

# ---------- Telegram ----------
def tg_send(text: str):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHATID, "text": text, "disable_web_page_preview": True})
    except Exception as e:
        print("Telegram error:", e)

# ---------- Binance REST ----------
def get_exchange_info():
    r = requests.get(BINANCE_API + "/api/v3/exchangeInfo", timeout=20)
    r.raise_for_status()
    return r.json()

def fetch_1d_klines(symbol: str, limit=210):
    params = {"symbol": symbol, "interval": "1d", "limit": limit}
    r = requests.get(BINANCE_API + "/api/v3/klines", params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def calc_ema200_prev(closes: pd.Series) -> float:
    """
    Возвращает EMA200 на ЗАКРЫТОЙ вчерашней свече.
    Для intraday будем проектировать текущую EMA как: ema_today = ema_prev*(1-α) + price*α
    """
    ema = closes.ewm(span=200, adjust=False).mean()
    # ema.iloc[-2] — вчерашняя EMA200 (последняя закрытая свеча)
    return float(ema.iloc[-2])

# ---------- Вселенная спотовых символов ----------
def build_spot_universe():
    info = get_exchange_info()
    spot = set()
    for s in info["symbols"]:
        if s.get("status") != "TRADING":
            continue
        perms = s.get("permissions", [])
        if "SPOT" not in perms:
            continue
        sym = s["symbol"]
        # Можно исключить токены типа UP/DOWN, если не нужны:
        if sym.endswith("UPUSDT") or sym.endswith("DOWNUSDT"):
            pass  # оставляем, ты сказал "все пары" — не фильтрую. Хочешь — раскомментируй ниже.
            # continue
        spot.add(sym)
    return spot

# ---------- Состояние по символам ----------
class SymState:
    __slots__ = ("ema_prev200", "last_side", "in_touch", "last_reset_anchor", "last_refresh_date", "yesterday_close")
    def __init__(self, ema_prev200: float, yesterday_close: float, last_refresh_date: dt.date):
        self.ema_prev200 = ema_prev200        # вчерашняя EMA200
        self.yesterday_close = yesterday_close
        self.last_side = None                 # 'above' | 'below' | 'touch'
        self.in_touch = False                 # находимся в зоне touch
        self.last_reset_anchor = None         # цена, от которой вышли из зоны touch
        self.last_refresh_date = last_refresh_date  # когда последний раз пересчитывали ema_prev

def fmt_pair(binance_symbol: str) -> str:
    # Разделяем BASE/QUOTE по известным котировочным валютам
    quotes = ["USDT","USDC","FDUSD","BUSD","BTC","ETH","BNB","TRY","EUR","TUSD","PYUSD","SOL"]
    for q in quotes:
        if binance_symbol.endswith(q):
            base = binance_symbol[:-len(q)]
            return f"{base}/{q}"
    # fallback
    return binance_symbol

def classify_side(price: float, ema_now: float, eps: float):
    d = (price - ema_now) / ema_now
    if abs(d) <= eps:
        return "touch"
    return "above" if d > eps else "below"

async def prepare_symbol_state(symbol: str) -> SymState | None:
    try:
        kl = fetch_1d_klines(symbol, limit=210)
        if len(kl) < 201:
            return None
        closes = pd.Series([float(x[4]) for x in kl], dtype=float)
        ema_prev = calc_ema200_prev(closes)
        yclose = float(kl[-2][4])  # вчерашний close
        last_day = dt.datetime.utcfromtimestamp(kl[-1][0]/1000).date()  # дата текущей (незакрытой) свечи
        return SymState(ema_prev200=ema_prev, yesterday_close=yclose, last_refresh_date=last_day)
    except Exception as e:
        print("prepare_symbol_state error", symbol, e)
        return None

async def refresh_symbol_state(symbol: str, state: SymState) -> None:
    """ Ежедневное обновление: пересчитать вчерашнюю EMA200 после закрытия дня """
    try:
        kl = fetch_1d_klines(symbol, limit=210)
        if len(kl) < 201:
            return
        closes = pd.Series([float(x[4]) for x in kl], dtype=float)
        state.ema_prev200 = calc_ema200_prev(closes)
        state.yesterday_close = float(kl[-2][4])
        state.last_refresh_date = dt.datetime.utcfromtimestamp(kl[-1][0]/1000).date()
    except Exception as e:
        print("refresh_symbol_state error", symbol, e)

async def daily_refresher(states: dict, throttle_per_minute=40):
    """Раз в 15 минут проходит по символам, которые не обновлялись сегодня, и освежает EMA_prev200."""
    while True:
        try:
            today = dt.datetime.utcnow().date()
            cnt = 0
            for sym, st in list(states.items()):
                if st.last_refresh_date != today and cnt < throttle_per_minute:
                    await refresh_symbol_state(sym, st)
                    cnt += 1
            # печатаем для контроля
            if cnt:
                print(f"[daily_refresher] refreshed {cnt} symbols")
        except Exception as e:
            print("daily_refresher error:", e)
        await asyncio.sleep(900)  # 15 минут

def event_text(symbol: str, price: float, action: str) -> str:
    # формат: "pair name, price, action"
    return f"{fmt_pair(symbol)}, {price}, {action}"

def will_reset_touch(prev_anchor: float | None, price: float, ema_now: float, reset_eps: float) -> bool:
    """Вернулись ли мы достаточно далеко от EMA, чтобы новый 'touch' считался новым событием?"""
    d = abs((price - ema_now)/ema_now)
    return d >= reset_eps

async def run():
    spot_symbols = build_spot_universe()
    print(f"Spot symbols: {len(spot_symbols)}")

    # состояния загружаем лениво при первом тике символа
    states: dict[str, SymState] = {}
    # запускаем ежедневный освежатель
    asyncio.create_task(daily_refresher(states))

    # подключаемся к минитикерам
    while True:
        try:
            async with websockets.connect(WS_MINITICK, ping_interval=20, ping_timeout=20) as ws:
                print("WS connected")
                async for raw in ws:
                    try:
                        arr = json.loads(raw)
                        # формат: список минитикеров
                        now = dt.datetime.utcnow()
                        today = now.date()

                        for item in arr:
                            sym = item.get("s")
                            if sym not in spot_symbols:
                                continue
                            price = float(item.get("c"))  # last price

                            st = states.get(sym)
                            if st is None:
                                # Ленивая инициализация (бережём лимиты)
                                st = await prepare_symbol_state(sym)
                                if st is None:
                                    continue
                                states[sym] = st

                            # если день сменился — пусть фоновой таск освежит; здесь просто не мешаем
                            # проецируем текущую EMA200 на основе сегодняшней цены
                            ema_now = st.ema_prev200*(1-ALPHA200) + price*ALPHA200

                            # определяем сторону
                            side = classify_side(price, ema_now, TOUCH_EPS)

                            # TOUCH логика
                            if side == "touch":
                                if st.last_side == "above":
                                    if not st.in_touch:  # первый вход в зону
                                        tg_send(event_text(sym, price, "touch from above"))
                                        st.in_touch = True
                                        st.last_reset_anchor = price
                                elif st.last_side == "below":
                                    if not st.in_touch:
                                        tg_send(event_text(sym, price, "touch from below"))
                                        st.in_touch = True
                                        st.last_reset_anchor = price
                                else:
                                    # были touch → повторим только если вышли из зоны достаточно далеко и снова вернулись
                                    pass

                            # CROSS логика
                            if st.last_side == "above" and side == "below":
                                tg_send(event_text(sym, price, "cross to down"))
                                st.in_touch = False
                                st.last_reset_anchor = price

                            if st.last_side == "below" and side == "above":
                                tg_send(event_text(sym, price, "cross to up"))
                                st.in_touch = False
                                st.last_reset_anchor = price

                            # Обновляем last_side и touch-reset
                            # если вышли далеко от EMA — разрешаем следующий touch
                            if side != "touch":
                                if st.in_touch and will_reset_touch(st.last_reset_anchor, price, ema_now, RESET_EPS):
                                    st.in_touch = False
                                    st.last_reset_anchor = price

                            st.last_side = side

                    except Exception as e:
                        print("loop item error:", e)
        except Exception as e:
            print("WS reconnect in 5s, reason:", e)
            await asyncio.sleep(5)

if __name__ == "__main__":
    if not TG_TOKEN or not TG_CHATID:
        print("Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        raise SystemExit(1)
    asyncio.run(run())
