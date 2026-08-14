#!/usr/bin/env python3
"""
Агрегатор квиз-игр по Караганде.

Собирает расписание с 4 сайтов:
- Квиз, плиз!  (krg.quizplease.com)     — Nuxt SPA
- Шейкер Квиз  (karaganda.shakerquiz.ru) — Next.js SPA
- Мохито Квиз  (krg.mohito-quiz.com)     — Tilda
- Смузи Квиз   (krg.smuzi-quiz.com)      — неизвестная платформа

Стратегия для каждого сайта:
  1) Быстрый путь: скачать голый HTML и поискать встроенный JSON-стейт
     (Nuxt кладёт данные в <script id="__NUXT_DATA__">, Next.js — в
     <script id="__NEXT_DATA__">). Если нашли — парсим оттуда, браузер
     не нужен.
  2) Медленный путь (fallback): открыть страницу в headless-браузере
     через Playwright, дождаться отрисовки JS, сохранить видимый текст
     страницы в debug/<site>.txt — чтобы можно было руками (или вместе
     со мной) написать точный парсер под конкретную вёрстку.

ВАЖНО: сайты часто меняют вёрстку/API, поэтому здесь не готовый
"вечный" парсер, а прочный каркас + отладочные дампы. После первого
запуска пришлите мне debug/*.txt (или *.json) — я доточу извлечение
дат/времени/адресов под реальную структуру данных.

Установка:
    pip install playwright requests beautifulsoup4
    playwright install chromium

Запуск:
    python scrape.py
"""

import json
import re
import datetime as dt
from pathlib import Path

import requests
from bs4 import BeautifulSoup

DEBUG_DIR = Path.cwd() / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

SITES = [
    {
        "key": "quizplease",
        "name": "Квиз, плиз!",
        "url": "https://krg.quizplease.com/schedule",
        "engine": "nuxt",
    },
    {
        "key": "shakerquiz",
        "name": "Шейкер Квиз",
        "url": "https://karaganda.shakerquiz.ru/",
        "engine": "next",
    },
    {
        "key": "mohito",
        "name": "Мохито Квиз",
        "url": "https://krg.mohito-quiz.com/games",
        "engine": "tilda",
    },
    {
        "key": "smuzi",
        "name": "Смузи Квиз",
        "url": "https://krg.smuzi-quiz.com/games",
        "engine": "unknown",
    },
    {
        "key": "wowquiz",
        "name": "Вау Квиз",
        "url": "https://krg.wowquiz.ru/schedule",
        "engine": "nuxt",
    },
    {
        "key": "chillquiz",
        "name": "Chill Quiz",
        "url": "https://chillquiz.kz/quizzes",
        "engine": "next",
        # по умолчанию сайт показывает другой город — нужно кликнуть
        # "Город" -> "Караганда" перед тем, как снимать расписание
        "needs_city_click": "Караганда",
    },
]


def try_fetch_embedded_json(url: str) -> dict | None:
    """Пытаемся скачать страницу без браузера и вытащить встроенный
    JSON-стейт (Nuxt / Next.js). Возвращает dict с сырыми данными или None."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"  [!] requests.get failed: {e}")
        return None

    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    # Next.js: <script id="__NEXT_DATA__" type="application/json">{...}</script>
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag and tag.string:
        try:
            return {"engine": "next", "data": json.loads(tag.string)}
        except json.JSONDecodeError:
            pass

    # Nuxt 3: <script id="__NUXT_DATA__" type="application/json">[...]</script>
    tag = soup.find("script", id="__NUXT_DATA__")
    if tag and tag.string:
        try:
            return {"engine": "nuxt3", "data": json.loads(tag.string)}
        except json.JSONDecodeError:
            pass

    # Nuxt 2: window.__NUXT__=(function(...){return {...}}(...))
    m = re.search(r"window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>", html, re.S)
    if m:
        # это JS-выражение, не всегда валидный JSON — сохраняем как текст
        return {"engine": "nuxt2_raw", "data": m.group(1)}

    return None


async def render_with_playwright(url: str, wait_selector: str | None = None, city_click: str | None = None):
    """Открываем страницу настоящим headless-браузером, ждём отрисовки
    и возвращаем видимый текст body. Требует `playwright install chromium`.

    Используем ИМЕННО async API: в Colab/Jupyter уже крутится свой
    event loop, а синхронный Playwright API с этим несовместим и
    падает с ошибкой "Sync API inside the asyncio loop".

    city_click: если задано (например, "Караганда") — перед снятием
    текста пытаемся кликнуть по элементу "Город", затем по элементу с
    этим названием города (для сайтов, где список игр зависит от
    выбранного в интерфейсе города, а не от URL)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(user_agent=HEADERS["User-Agent"])
        await page.goto(url, wait_until="networkidle", timeout=45000)
        try:
            await page.wait_for_timeout(2000)  # доп. пауза на подгрузку списков
            if wait_selector:
                await page.wait_for_selector(wait_selector, timeout=10000)
        except Exception:
            pass

        if city_click:
            try:
                await page.click("text=Город", timeout=5000)
                await page.wait_for_timeout(500)
                await page.click(f"text={city_click}", timeout=5000)
                await page.wait_for_timeout(2000)
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception as e:
                print(f"      [!] Не удалось выбрать город «{city_click}»: {e}")

        # прокрутим страницу вниз — вдруг список игр подгружается лениво
        for _ in range(5):
            await page.mouse.wheel(0, 2000)
            await page.wait_for_timeout(300)
        text = await page.inner_text("body")
        html = await page.content()
        await browser.close()
        return text, html


MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def parse_shakerquiz(next_data: dict) -> list[dict]:
    """Парсер для Шейкер Квиз (Next.js). Данные лежат в
    props.pageProps.store — это список пар [запрос, результат],
    имитирующий Map. Нужные нам ключи:
      'GET/games/search'              -> список игр (дата, цена, статус)
      'GET/games/venue/:venue/search' -> список мест проведения по game_id
    Время в event_time помечено суффиксом "Z", но по факту это уже
    локальное время Караганды (а не настоящий UTC) — конвертацию не делаем.
    """
    store = dict(next_data["props"]["pageProps"]["store"])
    games = store.get("GET/games/search", [])
    venues = store.get("GET/games/venue/:venue/search", [])
    venue_by_game = {v["game_id"]: v for v in venues}

    events = []
    for g in games:
        if g.get("status") not in ("Publish", "Invite"):
            continue  # пропускаем прошедшие/черновые игры
        when = dt.datetime.fromisoformat(g["event_time"].replace("Z", ""))
        venue = venue_by_game.get(g["id"])
        place = (
            f"{venue['name'].strip()}, {venue['street'].strip()} {venue['house_number']}"
            if venue else None
        )
        events.append({
            "source": "Шейкер Квиз",
            "when": when,
            "title": g["name"],
            "price": g.get("price"),
            "place": place,
        })
    return events


# Парсеры для конкретных площадок: engine -> функция(next_data) -> [event, ...]
SITE_PARSERS = {
    "shakerquiz": parse_shakerquiz,
}


def _resolve_year(month: int, day: int, today: dt.date) -> int:
    """Сайты не пишут год в расписании. Берём текущий год; если дата уже
    в прошлом (например, декабрь при просмотре в январе) — значит, речь
    о следующем годе."""
    year = today.year
    candidate = dt.date(year, month, day)
    if candidate < today - dt.timedelta(days=2):
        year += 1
    return year


def parse_mohito_text(text: str, today: dt.date | None = None) -> list[dict]:
    """Парсер для Мохито Квиз. Сайт рендерит расписание сразу целиком
    (без пагинации/календаря), поэтому достаточно видимого текста
    страницы. Формат одной игры:
        15 августа СБ 16:30 Сваты №2
        3 000KZT/ЧЕЛ
        <описание, может быть многострочным>
        Место проведения:
        Бар E33 (ул. Ермекова, 33)
        РЕГИСТРАЦИЯ
        ПОДРОБНЕЕ
    """
    today = today or dt.date.today()
    header_re = re.compile(
        r'^(\d{1,2}) ([а-яё]+) (ПН|ВТ|СР|ЧТ|ПТ|СБ|ВС) (\d{1,2}:\d{2}) (.+)$'
    )
    lines = [l.strip() for l in text.splitlines()]
    events = []
    i, n = 0, len(lines)
    while i < n:
        m = header_re.match(lines[i])
        if not m:
            i += 1
            continue
        day, month_name, _wd, time_str, title = m.groups()
        month = MONTHS_RU.get(month_name.lower())
        if not month:
            i += 1
            continue
        hh, mm = map(int, time_str.split(":"))
        year = _resolve_year(month, int(day), today)
        when = dt.datetime(year, month, int(day), hh, mm)

        j = i + 1
        price = None
        if j < n and "KZT" in lines[j].upper():
            pm = re.search(r'([\d\s]+)', lines[j])
            if pm:
                price = int(pm.group(1).replace(" ", ""))
            j += 1
        place = None
        while j < n and not header_re.match(lines[j]):
            if lines[j] == "Место проведения:":
                if j + 1 < n:
                    place = lines[j + 1]
                j += 2
                break
            j += 1
        events.append({
            "source": "Мохито Квиз", "when": when, "title": title.strip(),
            "price": price, "place": place,
        })
        i = j
    return events


def parse_smuzi_text(text: str, today: dt.date | None = None) -> list[dict]:
    """Парсер для Смузи Квиз. Тоже без пагинации — весь список сразу в
    видимом тексте. Формат:
        13 августа (ЧТ) 19:30 (Fragment) Караоке-квиз: ... №1
        3 000тңг/чел
        <описание>
        Место проведения:
        ПАБ «FRAGMENT» (Университетская улица, ст-е 28/11)
        ЗАПИСАТЬСЯ НА ИГРУ
        ПОДРОБНЕЕ
    """
    today = today or dt.date.today()
    header_re = re.compile(
        r'^(\d{1,2}) ([а-яё]+) \((ПН|ВТ|СР|ЧТ|ПТ|СБ|ВС)\) (\d{1,2}:\d{2}) (.+)$'
    )
    lines = [l.strip() for l in text.splitlines()]
    events = []
    i, n = 0, len(lines)
    while i < n:
        m = header_re.match(lines[i])
        if not m:
            i += 1
            continue
        day, month_name, _wd, time_str, title = m.groups()
        month = MONTHS_RU.get(month_name.lower())
        if not month:
            i += 1
            continue
        hh, mm = map(int, time_str.split(":"))
        year = _resolve_year(month, int(day), today)
        when = dt.datetime(year, month, int(day), hh, mm)
        # заголовок вида "(Fragment) Название" — убираем префикс площадки
        title = re.sub(r'^\([^)]*\)\s*', '', title.strip())

        j = i + 1
        price = None
        if j < n and ("тңг" in lines[j] or "тенге" in lines[j].lower()):
            pm = re.search(r'([\d\s]+)', lines[j])
            if pm:
                price = int(pm.group(1).replace(" ", ""))
            j += 1
        place = None
        while j < n and not header_re.match(lines[j]):
            if lines[j] == "Место проведения:":
                if j + 1 < n:
                    place = lines[j + 1]
                j += 2
                break
            j += 1
        events.append({
            "source": "Смузи Квиз", "when": when, "title": title,
            "price": price, "place": place,
        })
        i = j
    return events


WEEKDAYS_FULL = {"Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"}


def parse_quizplease_text(text: str, today: dt.date | None = None) -> list[dict]:
    """Парсер для Квиз, плиз!. Страница расписания сразу показывает ВСЕ
    игры, на которые открыта регистрация (их может быть одна, а может
    быть и несколько — зависит от того, сколько уже опубликовано
    организаторами). Календарь/доп. клики не нужны. Формат одной карточки:
        16 августа, Воскресенье
        [кино и музыка] изи KRG
        #11                              (необязательно — номер игры)
        <описание, может быть многострочным>
        Три толстяка Информация о площадке
        Назарбаева 74/1 Где это?
        в 19:30
        3000 ₸
        с человека оплата наличными
        ЗАПИСАТЬСЯ НА ИГРУ
        ПОДРОБНЕЕ
    """
    today = today or dt.date.today()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    date_re = re.compile(r'^(\d{1,2}) ([а-яё]+), (.+)$')
    venue_re = re.compile(r'^(.+) Информация о площадке$')
    address_re = re.compile(r'^(.+) Где это\?$')
    time_re = re.compile(r'^в (\d{1,2}:\d{2})$')
    price_re = re.compile(r'^([\d\s]+)\s*₸$')

    events = []
    i, n = 0, len(lines)
    while i < n:
        m = date_re.match(lines[i])
        if not (m and m.group(3) in WEEKDAYS_FULL):
            i += 1
            continue
        day, month_name, _wd = m.groups()
        month = MONTHS_RU.get(month_name.lower())
        if not month:
            i += 1
            continue

        j = i + 1
        title = lines[j] if j < n else None
        j += 1
        if j < n and re.match(r'^#\d+$', lines[j]):
            j += 1  # номер игры, не нужен

        venue_name = None
        while j < n:
            vm = venue_re.match(lines[j])
            if vm:
                venue_name = vm.group(1)
                j += 1
                break
            j += 1
        else:
            i = j
            continue

        address = None
        if j < n:
            am = address_re.match(lines[j])
            if am:
                address = am.group(1)
                j += 1

        hh_mm = None
        if j < n:
            tm = time_re.match(lines[j])
            if tm:
                hh_mm = tm.group(1)
                j += 1

        price = None
        if j < n:
            pm = price_re.match(lines[j])
            if pm:
                price = int(pm.group(1).replace(" ", ""))
                j += 1

        if hh_mm:
            hh, mm = map(int, hh_mm.split(":"))
            year = _resolve_year(month, int(day), today)
            when = dt.datetime(year, month, int(day), hh, mm)
            place = f"{venue_name}, {address}" if venue_name and address else (venue_name or address)
            events.append({
                "source": "Квиз, плиз!", "when": when, "title": title,
                "price": price, "place": place,
            })
        i = j
    return events


# Парсеры, применяемые к тексту, полученному через рендер в браузере
RENDER_TEXT_PARSERS = {
    "mohito": parse_mohito_text,
    "smuzi": parse_smuzi_text,
    "quizplease": parse_quizplease_text,
}


async def process_site(site: dict) -> list[dict]:
    print(f"\n=== {site['name']} ({site['url']}) ===")

    # Сайты, где список игр зависит от выбранного в интерфейсе города
    # (а не от URL) — встроенный JSON пропускаем, он покажет не тот
    # город, сразу идём в рендер с кликом по городу.
    city_click = site.get("needs_city_click")
    if not city_click:
        embedded = try_fetch_embedded_json(site["url"])
        if embedded:
            out_path = DEBUG_DIR / f"{site['key']}_embedded.json"
            with open(out_path, "w", encoding="utf-8") as f:
                if isinstance(embedded["data"], str):
                    f.write(embedded["data"])
                else:
                    json.dump(embedded["data"], f, ensure_ascii=False, indent=2)
            print(f"  [+] Нашли встроенный JSON-стейт ({embedded['engine']}) -> {out_path}")

            parser = SITE_PARSERS.get(site["key"])
            if parser and isinstance(embedded["data"], dict):
                try:
                    events = parser(embedded["data"])
                    print(f"      Распарсили {len(events)} игр(ы).")
                    return events
                except Exception as e:
                    print(f"      [!] Не смогли распарсить встроенный JSON: {e}")
            else:
                print("      Пришлите этот файл мне — извлеку из него расписание точечно.")
            return []

    if city_click:
        print(f"  [i] Сайт зависит от выбора города в интерфейсе — рендерим через браузер, выбираем «{city_click}»...")
    else:
        print("  [i] Встроенного JSON не нашли, рендерим через браузер (Playwright)...")

    try:
        text, html = await render_with_playwright(site["url"], city_click=city_click)
    except ModuleNotFoundError:
        print("  [!] Playwright не установлен. Выполните:")
        print("        pip install playwright")
        print("        playwright install chromium")
        return []
    except Exception as e:
        print(f"  [!] Не получилось отрендерить страницу: {e}")
        return []

    txt_path = DEBUG_DIR / f"{site['key']}_rendered.txt"
    html_path = DEBUG_DIR / f"{site['key']}_rendered.html"
    txt_path.write_text(text, encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")
    print(f"  [+] Сохранили видимый текст -> {txt_path}")
    print(f"  [+] Сохранили полный HTML   -> {html_path}")

    text_parser = RENDER_TEXT_PARSERS.get(site["key"])
    if text_parser:
        try:
            events = text_parser(text)
            print(f"      Распарсили {len(events)} игр(ы).")
            return events
        except Exception as e:
            print(f"      [!] Не смогли распарсить текст: {e}")
            return []

    print("      Пришлите эти файлы (или хотя бы кусок с расписанием) — доточу парсер.")
    return []


def print_weekly_summary(events: list[dict]) -> None:
    """Печатает сводку по дням на ближайшую неделю."""
    if not events:
        return
    events = sorted(events, key=lambda e: e["when"])
    today = dt.date.today()
    week_end = today + dt.timedelta(days=7)

    by_day: dict[dt.date, list[dict]] = {}
    for e in events:
        d = e["when"].date()
        if today <= d <= week_end:
            by_day.setdefault(d, []).append(e)

    print("\n" + "=" * 60)
    print("СВОДКА ПО ИГРАМ НА БЛИЖАЙШУЮ НЕДЕЛЮ")
    print("=" * 60)
    if not by_day:
        print("(в собранных данных нет игр на ближайшие 7 дней)")
        return

    weekdays_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    for d in sorted(by_day):
        print(f"\n--- {weekdays_ru[d.weekday()]}, {d.strftime('%d.%m.%Y')} ---")
        for e in sorted(by_day[d], key=lambda x: x["when"]):
            time_str = e["when"].strftime("%H:%M")
            price = f", {e['price']}₸" if e.get("price") else ""
            place = f" — {e['place']}" if e.get("place") else ""
            print(f"  {time_str} [{e['source']}] {e['title']}{price}{place}")


def events_to_json(events: list[dict]) -> list[dict]:
    """Готовим события к сериализации в JSON: datetime -> ISO-строка."""
    return [
        {
            "source": e["source"],
            "when": e["when"].isoformat(),
            "title": e["title"],
            "price": e.get("price"),
            "place": e.get("place"),
        }
        for e in sorted(events, key=lambda e: e["when"])
    ]


def save_schedule_json(events: list[dict], path: str = "schedule.json") -> None:
    payload = {
        "generated_at": dt.datetime.now().isoformat(),
        "events": events_to_json(events),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n[+] Сохранили общий файл расписания -> {path} ({len(events)} игр)")


async def main():
    print("Собираю данные расписаний по 4 сайтам квизов Караганды...")
    print(f"Сегодня: {dt.date.today().isoformat()}")

    all_events: list[dict] = []
    for site in SITES:
        all_events.extend(await process_site(site))

    print_weekly_summary(all_events)
    save_schedule_json(all_events)

    print("\n" + "-" * 60)
    print("Готово. Сырые файлы (для сайтов без готового парсера) — в ./debug/")
    print("Пришлите их мне, и я допишу парсер для оставшихся площадок —")
    print("тогда они тоже появятся в сводке выше.")


if __name__ == "__main__":
    import asyncio

    try:
        # Обычный запуск: python scrape.py (своего event loop ещё нет)
        asyncio.run(main())
    except RuntimeError:
        # Colab / Jupyter: event loop уже запущен ноутбуком,
        # используем nest_asyncio, чтобы можно было запустить наш
        # asyncio-код поверх него.
        import nest_asyncio
        nest_asyncio.apply()
        asyncio.get_event_loop().run_until_complete(main())
