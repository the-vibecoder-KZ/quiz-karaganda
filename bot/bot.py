#!/usr/bin/env python3
"""
Телеграм-бот "Квизы Караганды".

Ничего сам не парсит — просто скачивает свежий schedule.json (его
регулярно обновляет отдельный GitHub Actions workflow, см. scrape.py и
.github/workflows/scrape.yml в корне проекта) и отвечает на команды:

  /today   — квизы на сегодня (или на дату из аргумента: /today 16.08)
  /weekend — квизы на ближайшие выходные (Сб-Вс)
  /days3   — квизы на 3 дня вперёд (сегодня + 2 дня)
  /week    — квизы на ближайшие 7 дней
  /start   — краткая помощь

Все даты считаются по времени Казахстана (Asia/Almaty, UTC+5), а не по
времени сервера — это важно, т.к. Render крутит контейнеры в UTC, и
без явного учёта пояса "сегодня" переключалось бы на 5 часов позже
реального местного времени.

Работает как webhook (лёгкий Flask-сервис) — подходит для бесплатного
тарифа Render/аналогов, т.к. никакого браузера/Chromium тут не нужно.

Переменные окружения:
  TELEGRAM_BOT_TOKEN   — токен бота от @BotFather (обязательно)
  SCHEDULE_JSON_URL    — прямая ссылка на raw schedule.json в GitHub
                         (например:
                         https://raw.githubusercontent.com/<user>/<repo>/main/schedule.json)
"""

import datetime as dt
import os
import time
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SCHEDULE_JSON_URL = os.environ["SCHEDULE_JSON_URL"]
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

ALMATY_TZ = ZoneInfo("Asia/Almaty")  # UTC+5, без перехода на летнее время

WEEKDAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

app = Flask(__name__)

BOT_COMMANDS = [
    {"command": "today", "description": "Квизы на сегодня (или /today 16.08 — на дату)"},
    {"command": "weekend", "description": "Квизы на ближайшие выходные (Сб-Вс)"},
    {"command": "days3", "description": "Квизы на 3 дня вперёд"},
    {"command": "week", "description": "Квизы на ближайшие 7 дней"},
    {"command": "help", "description": "Что умеет бот"},
]


def register_commands() -> None:
    """Регистрируем список команд в Telegram, чтобы они всплывали
    подсказкой при вводе "/" в чате с ботом. Достаточно вызвать один
    раз (или при каждом старте сервиса — это безопасно, Telegram просто
    перезапишет список тем же самым)."""
    try:
        requests.post(
            f"{TELEGRAM_API}/setMyCommands",
            json={"commands": BOT_COMMANDS},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[!] Не удалось зарегистрировать команды бота: {e}")


register_commands()


def today_almaty() -> dt.date:
    """"Сегодня" по времени Казахстана, а не по времени сервера."""
    return dt.datetime.now(ALMATY_TZ).date()


def fetch_schedule() -> dict:
    # raw.githubusercontent.com кэширует ответы на своей CDN (обычно на
    # несколько минут, но иногда дольше). Добавляем cache-busting
    # параметр и явно просим не отдавать кэш, чтобы бот всегда видел
    # самую свежую версию schedule.json сразу после того, как её
    # закоммитил сборщик.
    url = f"{SCHEDULE_JSON_URL}?_={int(time.time())}"
    r = requests.get(url, headers={"Cache-Control": "no-cache", "Pragma": "no-cache"}, timeout=15)
    r.raise_for_status()
    return r.json()


def parse_date_arg(arg: str | None, today: dt.date) -> dt.date | None:
    """Разбираем необязательный аргумент вида "16.08" или "16.08.2026".
    Без аргумента — сегодняшняя дата."""
    if not arg:
        return today
    arg = arg.strip()
    for fmt in ("%d.%m.%Y", "%d.%m"):
        try:
            parsed = dt.datetime.strptime(arg, fmt)
            year = parsed.year if fmt == "%d.%m.%Y" else today.year
            return dt.date(year, parsed.month, parsed.day)
        except ValueError:
            continue
    return None


SOURCE_EMOJI = {
    "Квиз, плиз!": "🍺",
    "Шейкер Квиз": "🍸",
    "Мохито Квиз": "🍹",
    "Смузи Квиз": "🥤",
    "Chill Quiz": "❄️",
    "Вау Квиз": "🎉",
    "Эйнштейн Party": "🧠",
}
DEFAULT_EMOJI = "🎯"


def format_day(events: list[dict], day: dt.date) -> str:
    day_events = [e for e in events if dt.datetime.fromisoformat(e["when"]).date() == day]
    day_events.sort(key=lambda e: e["when"])
    header = f"📅 <b>{WEEKDAYS_RU[day.weekday()].upper()}, {day.strftime('%d.%m.%Y')}</b>"
    if not day_events:
        return f"{header}\nИгр не найдено."
    entries = []
    for e in day_events:
        when = dt.datetime.fromisoformat(e["when"])
        emoji = SOURCE_EMOJI.get(e["source"], DEFAULT_EMOJI)
        price = f", {e['price']}₸" if e.get("price") else ""
        place = f" — {e['place']}" if e.get("place") else ""
        entries.append(
            f"{emoji} <b>{when.strftime('%H:%M')}</b> [{e['source']}] {e['title']}{price}{place}"
        )
    return header + "\n\n" + "\n\n".join(entries)


def format_days(events: list[dict], start: dt.date, n: int) -> str:
    blocks = [format_day(events, start + dt.timedelta(days=i)) for i in range(n)]
    return "\n\n".join(blocks)


def format_week(events: list[dict], start: dt.date) -> str:
    return format_days(events, start, 7)


def upcoming_weekend(today: dt.date) -> tuple[dt.date, dt.date]:
    """Ближайшая суббота-воскресенье. Если сегодня уже суббота — берём
    её же (текущие выходные). Если сегодня воскресенье — эти выходные
    уже фактически прошли, берём следующие."""
    days_until_saturday = (5 - today.weekday()) % 7
    saturday = today + dt.timedelta(days=days_until_saturday)
    return saturday, saturday + dt.timedelta(days=1)


def format_weekend(events: list[dict], today: dt.date) -> str:
    saturday, sunday = upcoming_weekend(today)
    return format_day(events, saturday) + "\n\n" + format_day(events, sunday)


def send_message(chat_id: int, text: str) -> None:
    # Telegram режет сообщения по 4096 символов — на всякий случай рубим на части
    max_len = 4000
    for i in range(0, len(text), max_len):
        chunk = text[i:i + max_len]
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"},
            timeout=15,
        )


def send_typing(chat_id: int) -> None:
    """Показываем в чате индикатор "печатает..." — пока бот скачивает и
    обрабатывает schedule.json (обычно 5-7 секунд), пользователь видит,
    что запрос выполняется, а не что бот завис. Индикатор в Telegram
    держится ~5 секунд сам по себе, поэтому ошибки здесь не критичны —
    если не получилось отправить, просто промолчим."""
    try:
        requests.post(
            f"{TELEGRAM_API}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
            timeout=5,
        )
    except requests.RequestException:
        pass


@app.route("/", methods=["GET"])
def health():
    return "OK", 200


def split_command(text: str) -> tuple[str, str]:
    """Разбираем текст сообщения на команду и остаток (аргумент).
    В группах Telegram дописывает к команде имя бота (например,
    "/today@karaganda_quiz_bot 16.08") — этот суффикс нужно отрезать,
    иначе он попадёт в аргумент вместо чистой даты."""
    parts = text.split(maxsplit=1)
    if not parts:
        return "", ""
    cmd = parts[0].split("@", 1)[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    return cmd, arg


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    message = update.get("message") or update.get("edited_message")
    if not message:
        return "ignored", 200

    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    today = today_almaty()
    cmd, arg = split_command(text)

    # Все команды ниже дёргают fetch_schedule() (сетевой запрос,
    # обычно 5-7 секунд) — сразу показываем "печатает...", чтобы не
    # выглядело, будто бот завис.
    if cmd in ("/today", "/weekend", "/days3", "/week", "/start", "/help"):
        send_typing(chat_id)

    try:
        if cmd in ("/start", "/help"):
            sources_line = "разных сайтов"
            try:
                data = fetch_schedule()
                sources = sorted({e["source"] for e in data["events"]})
                if sources:
                    sources_line = ", ".join(sources)
            except requests.RequestException:
                pass
            send_message(
                chat_id,
                f"Привет! Я собираю расписание квизов по Караганде "
                f"с сайтов: {sources_line}.\n\n"
                "Команды:\n"
                "/today — квизы на сегодня (можно указать дату: /today 16.08)\n"
                "/weekend — квизы на ближайшие выходные (Сб-Вс)\n"
                "/days3 — квизы на 3 дня вперёд\n"
                "/week — квизы на ближайшие 7 дней",
            )
        elif cmd == "/today":
            day = parse_date_arg(arg or None, today)
            if day is None:
                send_message(chat_id, "Не поняла дату. Формат: /today 16.08")
                return "ok", 200
            data = fetch_schedule()
            send_message(chat_id, format_day(data["events"], day))
        elif cmd == "/weekend":
            data = fetch_schedule()
            send_message(chat_id, format_weekend(data["events"], today))
        elif cmd == "/days3":
            data = fetch_schedule()
            send_message(chat_id, format_days(data["events"], today, 3))
        elif cmd == "/week":
            data = fetch_schedule()
            send_message(chat_id, format_week(data["events"], today))
        else:
            # В группах бот получает вообще все сообщения, не только
            # команды — отвечаем "не поняла" только если это была
            # попытка команды (начинается с "/"), иначе молча
            # игнорируем обычную переписку в чате.
            if cmd.startswith("/"):
                send_message(chat_id, "Не поняла команду. Есть /today, /weekend, /days3 и /week.")
    except requests.RequestException as e:
        send_message(chat_id, f"Не получилось получить расписание: {e}")

    return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
