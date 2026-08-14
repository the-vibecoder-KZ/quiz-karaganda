#!/usr/bin/env python3
"""
Телеграм-бот "Квизы Караганды".

Ничего сам не парсит — просто скачивает свежий schedule.json (его
регулярно обновляет отдельный GitHub Actions workflow, см. scrape.py и
.github/workflows/scrape.yml в корне проекта) и отвечает на команды:

  /today  — квизы на сегодня (или на дату из аргумента: /today 16.08)
  /week   — квизы на ближайшие 7 дней
  /start  — краткая помощь

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

import requests
from flask import Flask, request

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SCHEDULE_JSON_URL = os.environ["SCHEDULE_JSON_URL"]
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

WEEKDAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

app = Flask(__name__)

BOT_COMMANDS = [
    {"command": "today", "description": "Квизы на сегодня (или /today 16.08 — на дату)"},
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


def format_day(events: list[dict], day: dt.date) -> str:
    day_events = [e for e in events if dt.datetime.fromisoformat(e["when"]).date() == day]
    day_events.sort(key=lambda e: e["when"])
    header = f"<b>{WEEKDAYS_RU[day.weekday()]}, {day.strftime('%d.%m.%Y')}</b>"
    if not day_events:
        return f"{header}\nИгр не найдено."
    lines = [header]
    for e in day_events:
        when = dt.datetime.fromisoformat(e["when"])
        price = f", {e['price']}₸" if e.get("price") else ""
        place = f" — {e['place']}" if e.get("place") else ""
        lines.append(f"{when.strftime('%H:%M')} [{e['source']}] {e['title']}{price}{place}")
    return "\n".join(lines)


def format_week(events: list[dict], start: dt.date) -> str:
    blocks = [format_day(events, start + dt.timedelta(days=i)) for i in range(7)]
    return "\n\n".join(blocks)


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


@app.route("/", methods=["GET"])
def health():
    return "OK", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    message = update.get("message") or update.get("edited_message")
    if not message:
        return "ignored", 200

    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    today = dt.date.today()

    try:
        if text.startswith("/start") or text.startswith("/help"):
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
                "/week — квизы на ближайшие 7 дней",
            )
        elif text.startswith("/today"):
            arg = text[len("/today"):].strip() or None
            day = parse_date_arg(arg, today)
            if day is None:
                send_message(chat_id, "Не поняла дату. Формат: /today 16.08")
                return "ok", 200
            data = fetch_schedule()
            send_message(chat_id, format_day(data["events"], day))
        elif text.startswith("/week"):
            data = fetch_schedule()
            send_message(chat_id, format_week(data["events"], today))
        else:
            send_message(chat_id, "Не поняла команду. Есть /today и /week.")
    except requests.RequestException as e:
        send_message(chat_id, f"Не получилось получить расписание: {e}")

    return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
