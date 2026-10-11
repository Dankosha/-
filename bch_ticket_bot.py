"""
Один проход: проверяет все ближайшие воскресенья на pass.rw.by
и пишет в Telegram, если места ПОЯВИЛИСЬ (было нет -> стало есть).
Запускается по расписанию из GitHub Actions. Токен берётся из секретов.
"""
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
FROM_ST = "Ганцевичи"
TO_ST = "Минск"
DEPARTURE = "20:19"
WEEKDAY = 6          # 6 = воскресенье
WEEKS_AHEAD = 7
PAUSE = 3            # секунд между запросами

URL = "https://pass.rw.by/ru/route/"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
NO_SEATS = ("нет мест", "мест нет", "продажа закрыта", "билетов нет")
STATE = Path("state.json")


def notify(text):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text},
        timeout=15,
    )


def upcoming_dates():
    now = datetime.now(ZoneInfo("Europe/Minsk"))
    d = now.date()
    while d.weekday() != WEEKDAY:
        d += timedelta(days=1)
    if d == now.date() and now.strftime("%H:%M") >= DEPARTURE:
        d += timedelta(days=7)
    return [d + timedelta(days=7 * i) for i in range(WEEKS_AHEAD)]


def check(day):
    r = requests.get(
        URL,
        params={"from": FROM_ST, "to": TO_ST, "date": day.isoformat()},
        headers=HEADERS,
        timeout=20,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for row in soup.select("tr, li, div.sch-table__row"):
        text = " ".join(row.get_text(" ", strip=True).split())
        if DEPARTURE in text and len(text) < 600:
            return (not any(p in text.lower() for p in NO_SEATS)), text
    return None, ""


def main():
    old = json.loads(STATE.read_text()) if STATE.exists() else {}
    new = {}
    for day in upcoming_dates():
        key = day.isoformat()
        try:
            status, text = check(day)
            print(key, status, text[:150])
            new[key] = bool(status)
            if status and not old.get(key):
                notify(
                    f"🚆 ПОЯВИЛИСЬ МЕСТА!\n{FROM_ST} → {TO_ST}, "
                    f"{day.strftime('%d.%m.%Y')} в {DEPARTURE}\n"
                    f"{text[:300]}\n\nhttps://pass.rw.by/ru/"
                )
        except Exception as e:
            print("Ошибка:", key, e)
            new[key] = old.get(key, False)  # при сбое не меняем состояние
        time.sleep(PAUSE)
    STATE.write_text(json.dumps(new, indent=1))


if __name__ == "__main__":
    main()
