#!/usr/bin/env python3
"""Мониторинг свободных мест на маршрутке Могилёв -> Минск (mogilevminsk.by).

Использует тот же JSON-эндпоинт, что и сайт: /timetable/trips/.
Зависимостей нет — только стандартная библиотека Python 3.

Запуск:
    python monitor.py            # обычная проверка + уведомление в Telegram
    python monitor.py --show     # только показать текущую доступность
    python monitor.py --dry-run  # проверка без реальной отправки в Telegram
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://mogilevminsk.by"
TRIPS_URL = f"{BASE_URL}/timetable/trips/"
SITE_URL = f"{BASE_URL}/timetable"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 seat-monitor/1.0"
)

def _load_env_file(path: Path) -> None:
    """Подхватывает локальный .env.local — чтобы токен не жил в git и не вводился каждый раз."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(Path(__file__).with_name(".env.local"))

# Чтобы русский текст корректно печатался в консоли Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TARGET_DATE = os.getenv("TARGET_DATE", "2026-11-08")  # YYYY-MM-DD
FROM_CITY = os.getenv("FROM_CITY", "2")  # 2 = Могилёв
DEST_CITY = os.getenv("DEST_CITY", "1")  # 1 = Минск
WINDOW_START = os.getenv("WINDOW_START", "16:30")
WINDOW_END = os.getenv("WINDOW_END", "20:00")
MIN_FREE_SEATS = int(os.getenv("MIN_FREE_SEATS", "1"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------- HTTP helpers


def _multipart(fields: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    """Собирает тело multipart/form-data — так его отправляет сам сайт."""
    boundary = "----seatmonitor" + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        )
    chunks.append(f"--{boundary}--\r\n")
    body = "".join(chunks).encode("utf-8")
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    return body, headers


def fetch_trips(date_str: str, attempts: int = 4) -> list[dict]:
    """Возвращает список рейсов вокруг указанной даты (сайт отдаёт ±1 день)."""
    body, headers = _multipart(
        {"date": date_str, "from_city": FROM_CITY, "dest_city": DEST_CITY}
    )
    headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": SITE_URL,
            "X-Requested-With": "XMLHttpRequest",
        }
    )

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(TRIPS_URL, data=body, headers=headers)
            with urllib.request.urlopen(request, timeout=25) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("result") != "success":
                raise RuntimeError(f"API ответил: {payload.get('result')!r}")
            raw = payload["data"]["trips"]
            return list(raw.values()) if isinstance(raw, dict) else list(raw or [])
        except Exception as exc:  # noqa: BLE001 - нужен любой сбой сети/парсинга
            last_error = exc
            if attempt + 1 < attempts:
                delay = 2**attempt
                log(f"попытка {attempt + 1} не удалась ({exc}), повтор через {delay} с")
                time.sleep(delay)

    raise RuntimeError(f"не удалось получить расписание: {last_error}")


# ---------------------------------------------------------------- data helpers


def target_date_dmy(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")


def weekday_ru(date_str: str) -> str:
    return WEEKDAYS[datetime.strptime(date_str, "%Y-%m-%d").weekday()]


def select_window(trips: list[dict], date_str: str) -> dict[str, int]:
    """Рейсы нужной даты в окне времени -> {время отправления: свободных мест}."""
    dmy = target_date_dmy(date_str)
    result: dict[str, int] = {}
    for trip in trips:
        if str(trip.get("date", "")) != dmy:
            continue
        departure = str(trip.get("departure_time", ""))[:5]
        if not departure or not (WINDOW_START <= departure <= WINDOW_END):
            continue
        try:
            free = int(trip.get("free_seats") or 0)
        except (TypeError, ValueError):
            free = 0
        result[departure] = free
    return dict(sorted(result.items()))


def plural_seats(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "место"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "места"
    return "мест"


# ---------------------------------------------------------------- state helpers


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------- Telegram


def tg_call(method: str, payload: dict[str, str]) -> dict:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def resolve_chat_id() -> str:
    """Если chat_id не задан — берём его из последнего сообщения боту."""
    global TELEGRAM_CHAT_ID
    if TELEGRAM_CHAT_ID:
        return TELEGRAM_CHAT_ID

    updates = tg_call("getUpdates", {"limit": "20", "timeout": "0"})
    for update in reversed(updates.get("result", [])):
        message = update.get("message") or update.get("channel_post") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id:
            TELEGRAM_CHAT_ID = str(chat_id)
            log(f"chat_id определён автоматически: {TELEGRAM_CHAT_ID}")
            return TELEGRAM_CHAT_ID

    raise RuntimeError(
        "не удалось определить chat_id — напишите боту /start или задайте TELEGRAM_CHAT_ID"
    )


def send_telegram(text: str, dry_run: bool) -> None:
    if dry_run:
        log("[DRY-RUN] сообщение в Telegram:\n" + text)
        return
    chat_id = resolve_chat_id()
    tg_call(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        },
    )
    log(f"уведомление отправлено в чат {chat_id}")


def build_message(available: dict[str, int], newly: list[str], date_str: str) -> str:
    title = (
        f"🎫 Свободное место: Могилёв → Минск\n"
        f"{datetime.strptime(date_str, '%Y-%m-%d').strftime('%d.%m.%Y')} "
        f"({weekday_ru(date_str)})\n"
    )
    if newly:
        fresh = ", ".join(f"{dep} ({available[dep]} {plural_seats(available[dep])})" for dep in newly)
        title += f"Новое: {fresh}\n"

    lines = [f"• {dep} — {free} {plural_seats(free)}" for dep, free in available.items()]
    footer = f"\nБронь: {SITE_URL}\nТел.: 788-55-33, 788-66-33"
    return title + "\nСейчас есть места:\n" + "\n".join(lines) + "\n" + footer


# ---------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    show_only = "--show" in argv
    test_send = "--test" in argv
    dry_run = "--dry-run" in argv or os.getenv("DRY_RUN", "").lower() in {
        "1",
        "true",
        "yes",
    }

    if test_send:
        try:
            send_telegram(
                "✅ Тест монитора мест: Могилёв → Минск, 08.11.2026, 16:30–20:00.\n"
                "Уведомления работают.",
                dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"ОШИБКА отправки: {exc}")
            return 1
        return 0

    log(
        f"проверка {TARGET_DATE} ({weekday_ru(TARGET_DATE)}), "
        f"окно {WINDOW_START}–{WINDOW_END}, городов {FROM_CITY}->{DEST_CITY}"
    )

    try:
        trips = fetch_trips(TARGET_DATE)
    except Exception as exc:  # noqa: BLE001
        log(f"ОШИБКА: {exc}")
        return 1

    current = select_window(trips, TARGET_DATE)
    if not current:
        log("рейсы на эту дату пока не опубликованы — ждём")
        return 0

    if show_only:
        for dep, free in current.items():
            mark = "ЕСТЬ" if free >= MIN_FREE_SEATS else "нет"
            log(f"{dep}  свободно={free:<3} {mark}")
        return 0

    available = {dep: free for dep, free in current.items() if free >= MIN_FREE_SEATS}

    state = load_state()
    previous = state.get("available") or {} if state.get("date") == TARGET_DATE else {}
    newly = [dep for dep in available if dep not in previous]

    log(f"рейсов в окне: {len(current)}, свободных: {len(available)}")

    if newly:
        message = build_message(available, newly, TARGET_DATE)
        try:
            send_telegram(message, dry_run)
        except Exception as exc:  # noqa: BLE001
            log(f"ОШИБКА отправки уведомления: {exc}")
            return 1

    state_unchanged = state.get("date") == TARGET_DATE and previous == available
    if state_unchanged:
        log("состояние не изменилось — state.json не трогаем")
    else:
        save_state(
            {
                "date": TARGET_DATE,
                "available": available,
                "observed": current,
                "updated": datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )

    if not newly:
        log("новых свободных мест нет")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
