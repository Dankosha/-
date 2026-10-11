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
# Списки именно белорусских прокси (берём все)
COUNTRY_SOURCES = [
    ("", "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/countries/BY/data.txt"),
    ("", "https://cdn.jsdelivr.net/gh/proxyscrape/free-proxy-list@main/proxies/countries/by/data.txt"),
    ("http", "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&country=by&timeout=10000"),
    ("socks4", "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks4&country=by&timeout=10000"),
    ("socks5", "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks5&country=by&timeout=10000"),
    ("geonode", "https://proxylist.geonode.com/api/proxy-list?country=BY&limit=200&page=1&sort_by=lastChecked&sort_type=desc"),
]
# Общие списки: страну не фильтруем, просто проверяем, достучится ли прокси до rw.by
GENERIC_SOURCES = [
    ("", "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.txt"),
    ("http", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("socks4", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt"),
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    ("http", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
]
GENERIC_SAMPLE = 700   # сколько случайных прокси из общих списков проверять
POOL_SIZE = 3          # сколько рабочих прокси держим про запас
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


def read_source(proto, url):
    out = []
    try:
        r = requests.get(url, timeout=25)
        name = url.split("//")[1].split("/")[0] + " " + url.split("/")[-2][:12]
        if not r.ok:
            print("список", name, "->", r.status_code)
            return out
        if proto == "geonode":
            for it in r.json().get("data", []):
                for p in it.get("protocols", ["http"]):
                    out.append(f"{p}://{it['ip']}:{it['port']}")
        else:
            for line in r.text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "://" not in line:
                    line = f"{proto or 'http'}://{line}"
                out.append(line)
        print("список", name, "->", len(out))
    except Exception as e:
        print("не загрузился список:", url[:60], str(e)[:80])
    return out


def build_candidates():
    country, generic = [], []
    for proto, url in COUNTRY_SOURCES:
        country += read_source(proto, url)
    for proto, url in GENERIC_SOURCES:
        generic += read_source(proto, url)
    country = list(dict.fromkeys(country))
    generic = [p for p in dict.fromkeys(generic) if p not in country]
    random.shuffle(generic)
    cands = country + generic[:GENERIC_SAMPLE]
    print("белорусских:", len(country), "общих в проверку:", min(len(generic), GENERIC_SAMPLE))
    return cands


def search_proxies(test_day, need=POOL_SIZE):
    cands = build_candidates()
    if not cands:
        return []
    ex = ThreadPoolExecutor(max_workers=100)
    futs = {ex.submit(fetch, test_day, p, (5, 10)): p for p in cands}
    found = []
    try:
        for f in as_completed(futs):
            if f.exception() is None:
                print("Рабочий прокси:", futs[f])
                found.append(futs[f])
                if len(found) >= need:
                    break
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return found


def main():
    old = json.loads(STATE.read_text()) if STATE.exists() else {}
    new = {}
    pool = [FIXED_PROXY] if FIXED_PROXY else list(old.get("_proxies", []))
    searched = False
    ok_any = False

    for day in upcoming_dates():
        key = day.isoformat()
        html, err = None, None
        for _ in range(6):
            if not pool and not searched and not FIXED_PROXY:
                pool = search_proxies(day)
                searched = True
            if not pool:
                err = "нет рабочего прокси"
                break
            try:
                html = fetch(day, pool[0], (8, 20))
                err = None
                break
            except Exception as e:
                err = str(e)[:150]
                print("Ошибка:", key, pool[0], err[:100])
                if FIXED_PROXY:
                    break
                pool.pop(0)  # этот прокси умер, пробуем следующий

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
    new["_proxies"] = [] if FIXED_PROXY else pool[:POOL_SIZE]
    STATE.write_text(json.dumps(new, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
