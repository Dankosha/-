"""
Один проход: проверяет ближайшие воскресенья на pass.rw.by через белорусский прокси
и пишет в Telegram, если места ПОЯВИЛИСЬ (было нет -> стало есть).

Прокси: либо секрет PROXY_URL (свой/платный), либо автопоиск бесплатных
белорусских прокси из публичных списков (jsDelivr).
"""
import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
FIXED_PROXY = os.environ.get("PROXY_URL", "").strip()   # необязательно
FROM_ST = "Ганцевичи"
TO_ST = "Минск"
DEPARTURE = "20:19"
WEEKDAY = 6          # 6 = воскресенье
WEEKS_AHEAD = 7
PAUSE = 2            # секунд между запросами
FAILS_BEFORE_ALERT = 6   # после стольких неудачных запусков подряд напишем в Telegram

URL = "https://pass.rw.by/ru/route/"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
NO_SEATS = ("нет мест", "мест нет", "продажа закрыта", "билетов нет")
PROXY_SOURCES = [
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/countries/BY/data.txt",
    "https://cdn.jsdelivr.net/gh/proxyscrape/free-proxy-list@main/proxies/countries/by/data.txt",
]
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


def fetch(day, proxy, timeout=(6, 15)):
    r = requests.get(
        URL,
        params={"from": FROM_ST, "to": TO_ST, "date": day.isoformat()},
        headers=HEADERS,
        proxies={"http": proxy, "https": proxy},
        timeout=timeout,
    )
    r.raise_for_status()
    if "rw.by" not in r.text:
        raise RuntimeError("странный ответ (не rw.by)")
    return r.text


def parse(html):
    """True — места есть, False — нет, None — поезд не найден."""
    soup = BeautifulSoup(html, "html.parser")
    for row in soup.select("tr, li, div.sch-table__row"):
        text = " ".join(row.get_text(" ", strip=True).split())
        if DEPARTURE in text and len(text) < 600:
            return (not any(p in text.lower() for p in NO_SEATS)), text
    return None, ""


def load_proxies():
    out = []
    for u in PROXY_SOURCES:
        try:
            r = requests.get(u, timeout=20)
            print("список прокси", u.split("@")[0].split("/")[-1], r.status_code, len(r.text.splitlines()), "строк")
            if r.ok:
                for line in r.text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "://" not in line:
                        line = "http://" + line
                    out.append(line)
        except Exception as e:
            print("не загрузился список:", e)
    out = list(dict.fromkeys(out))
    random.shuffle(out)
    return out[:300]


def find_proxy(test_day):
    cands = load_proxies()
    print("кандидатов:", len(cands))
    if not cands:
        return None
    ex = ThreadPoolExecutor(max_workers=40)
    futs = {ex.submit(fetch, test_day, p, (5, 10)): p for p in cands}
    try:
        for f in as_completed(futs):
            if f.exception() is None:
                print("Нашёл рабочий прокси:", futs[f])
                return futs[f]
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return None


def main():
    old = json.loads(STATE.read_text()) if STATE.exists() else {}
    new = {}
    proxy = FIXED_PROXY or old.get("_proxy") or None
    search_failed = False
    ok_any = False

    for day in upcoming_dates():
        key = day.isoformat()
        html, err = None, None
        for _ in range(2):
            if not proxy and not search_failed:
                proxy = find_proxy(day)
                search_failed = proxy is None
            if not proxy:
                err = "нет рабочего прокси"
                break
            try:
                html = fetch(day, proxy)
                err = None
                break
            except Exception as e:
                err = str(e)[:150]
                print("Ошибка:", key, err)
                if FIXED_PROXY:
                    break
                proxy = None  # попробуем найти другой

        if html is None:
            print(key, "пропуск:", err)
            new[key] = old.get(key, False)
            continue

        ok_any = True
        status, text = parse(html)
        print(key, status, text[:150])
        new[key] = bool(status)
        if status and not old.get(key):
            notify(
                f"🚆 ПОЯВИЛИСЬ МЕСТА!\n{FROM_ST} → {TO_ST}, "
                f"{day.strftime('%d.%m.%Y')} в {DEPARTURE}\n"
                f"{text[:300]}\n\nhttps://pass.rw.by/ru/"
            )
        time.sleep(PAUSE)

    fails = 0 if ok_any else old.get("_fails", 0) + 1
    if fails == FAILS_BEFORE_ALERT:
        notify("⚠️ Бот уже несколько запусков не может достучаться до pass.rw.by "
               "(нет рабочего белорусского прокси). Нужен свой прокси или другой хостинг.")
    new["_fails"] = fails
    new["_proxy"] = proxy if (proxy and not FIXED_PROXY) else ""
    STATE.write_text(json.dumps(new, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
