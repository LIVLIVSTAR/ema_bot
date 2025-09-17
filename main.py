import os
import asyncio
import time
import requests
import json
import math
from datetime import datetime

import websockets
import pandas as pd

# === Параметры из Railway .env ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# пары из переменной окружения PAIRS через запятую (например: BTCUSDT,BNBUSDT)
PAIRS_ENV = os.getenv("PAIRS", "")
PAIRS = [p.strip().lower() for p in PAIRS_ENV.split(",") if p.strip()]

TOUCH_EMA50 = {}
TOUCH_EMA200 = {}

last_sent_hour = None

# === Функция отправки сообщения в Telegram ===
def send_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print("Ошибка отправки Telegram:", e)

# === Получение данных по свечам Binance ===
def get_klines(symbol: str, interval='1m', limit=250):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol.upper()}&interval={interval}&limit={limit}"
    data = requests.get(url, timeout=10).json()
    df = pd.DataFrame(data, columns=[
        'time','o','h','l','c','v','close_time','q','n','taker_base','taker_quote','ignore'
    ])
    df['c'] = df['c'].astype(float)
    return df

# === Проверка касания EMA ===
def check_touch(symbol: str):
    df = get_klines(symbol, interval='1m', limit=250)
    closes = df['c']
    ema50 = closes.ewm(span=50).mean().iloc[-1]
    ema200 = closes.ewm(span=200).mean().iloc[-1]
    price = closes.iloc[-1]

    touched_50 = math.isclose(price, ema50, rel_tol=0.0005) or (price >= ema50 and closes.iloc[-2] < ema50) or (price <= ema50 and closes.iloc[-2] > ema50)
    touched_200 = math.isclose(price, ema200, rel_tol=0.0005) or (price >= ema200 and closes.iloc[-2] < ema200) or (price <= ema200 and closes.iloc[-2] > ema200)

    return price, ema50, ema200, touched_50, touched_200

# === Главная асинхронная петля ===
async def main_loop():
    global last_sent_hour
    while True:
        now = datetime.utcnow()
        current_hour = now.strftime("%Y-%m-%d %H")  # например 2025-09-16 17
        messages = []

        for symbol in PAIRS:
            try:
                price, ema50, ema200, touched_50, touched_200 = check_touch(symbol)
                text_part = ""
                if touched_50:
                    text_part += f"{symbol.upper()} touched EMA50 at {price:.4f}\n"
                if touched_200:
                    text_part += f"{symbol.upper()} touched EMA200 at {price:.4f}\n"
                if text_part:
                    messages.append(text_part)
            except Exception as e:
                print("Ошибка по паре", symbol, e)

        if messages:
            text = "\n".join(messages) + "\nSponsored by www.livlivstar.com"
        else:
            text = "/Just Relax/ Sponsored by www.livlivstar.com"

        send_message(text)

        # ждём до следующего часа
        # вычисляем сколько секунд осталось до начала следующего часа
        now = datetime.utcnow()
        wait_sec = 3600 - (now.minute*60 + now.second)
        await asyncio.sleep(wait_sec)

# === Запуск ===
if __name__ == "__main__":
    print("Bot started...")
    asyncio.run(main_loop())
