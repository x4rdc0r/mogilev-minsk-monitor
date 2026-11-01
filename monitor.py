#!/usr/bin/env python3
"""Мониторинг свободных мест Могилёв -> Минск на 08.11.2026, окно 16:30–20:00.

Источники:
  * mogilevminsk.by — API платформы Transporter (free_seats, показ с потолком 2);
  * avto-slava.by   — та же платформа, тот же API;
  * atlasbus.by     — Next.js: данные рейсов лежат в __NEXT_DATA__ страницы
                      маршрута с параметром ?date=YYYY-MM-DD.

Зависимостей нет — только стандартная библиотека Python 3.

Запуск:
    python monitor.py            # проверка + уведомление в Telegram
    python monitor.py --show     # показать доступность по всем источникам
    python monitor.py --dry-run  # без реальной отправки в Telegram
    python monitor.py --test     # только тестовое сообщение в Telegram
"""

from __future__ import annotations

import gzip
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 seat-monitor/1.0"
)

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _load_env_file(path: Path) -> None:
    """Подхватывает локальный .env.local — чтобы токен не жил в git."""
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
WINDOW_START = os.getenv("WINDOW_START", "16:30")
WINDOW_END = os.getenv("WINDOW_END", "20:00")
MIN_FREE_SEATS = int(os.getenv("MIN_FREE_SEATS", "1"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
SOURCES_FILTER = {
    part.strip() for part in os.getenv("SOURCES", "").split(",") if part.strip()
}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Города на платформе Transporter: 2 = Могилёв, 1 = Минск (одинаково на обоих сайтах)
FROM_CITY = os.getenv("FROM_CITY", "2")
DEST_CITY = os.getenv("DEST_CITY", "1")

SOURCES: list[dict] = [
    {
        "id": "mogilevminsk",
        "title": "mogilevminsk.by",
        "kind": "transporter",
        "base": "https://mogilevminsk.by",
        "capped": True,
        "url": "https://mogilevminsk.by/timetable",
    },
    {
        "id": "avtoslava",
        "title": "avto-slava.by",
        "kind": "transporter",
        "base": "https://avto-slava.by",
        "capped": True,
        "url": "https://avto-slava.by/timetable",
    },
    {
        "id": "atlasbus",
        "title": "atlasbus.by",
        "kind": "atlasbus",
        "base": "https://atlasbus.by",
        "capped": False,
        "url": "https://atlasbus.by/Маршруты/Могилёв/Минск",
    },
]


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------- HTTP


def _request(url: str, data: bytes | None = None, headers: dict | None = None,
             timeout: int = 40) -> str:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept-Encoding": "gzip",
        "Referer": url.split("?")[0],
    }
    request_headers.update(headers or {})

    request = urllib.request.Request(url, data=data, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")


def with_retries(label: str, func, attempts: int = 3):
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - нужен любой сбой сети/парсинга
            last_error = exc
            if attempt + 1 < attempts:
                delay = 2**attempt
                log(f"  {label}: попытка {attempt + 1} не удалась ({exc}), повтор через {delay} с")
                time.sleep(delay)
    raise RuntimeError(f"{label}: {last_error}")


def _multipart(fields: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    """Тело multipart/form-data — так его отправляет сам сайт."""
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
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


# ---------------------------------------------------------------- источники


def fetch_transporter(source: dict, date_str: str) -> dict[str, dict]:
    """Сайты на платформе Transporter: /timetable/trips/ отдаёт free_seats."""
    body, headers = _multipart(
        {"date": date_str, "from_city": FROM_CITY, "dest_city": DEST_CITY}
    )

    def call() -> dict:
        text = _request(
            source["base"] + "/timetable/trips/",
            data=body,
            headers={**headers, "Accept": "application/json, text/plain, */*"},
        )
        payload = json.loads(text)
        if payload.get("result") != "success":
            raise RuntimeError(f"API ответил: {payload.get('result')!r}")
        raw = payload["data"]["trips"]
        return list(raw.values()) if isinstance(raw, dict) else list(raw or [])

    trips = with_retries(source["title"], call)
    dmy = to_dmy(date_str)

    result: dict[str, dict] = {}
    for trip in trips:
        if str(trip.get("date", "")) != dmy:
            continue
        departure = str(trip.get("departure_time", ""))[:5]
        if not in_window(departure):
            continue
        try:
            free = int(trip.get("free_seats") or 0)
        except (TypeError, ValueError):
            free = 0
        # одинаковое время у разных рейсов — оставляем лучший вариант
        previous = result.get(departure)
        if previous is None or free > previous["free"]:
            result[departure] = {"free": free, "carrier": None}
    return result


def fetch_atlasbus(source: dict, date_str: str) -> dict[str, dict]:
    """Atlasbus (Next.js): рейсы на дату лежат в __NEXT_DATA__ страницы маршрута."""
    path = "/".join(
        urllib.parse.quote(part) for part in ("Маршруты", "Могилёв", "Минск")
    )
    url = f"{source['base']}/{path}?date={date_str}"

    def call() -> dict:
        html = _request(url, timeout=60)
        match = re.search(
            r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
        )
        if not match:
            raise RuntimeError("не нашёл __NEXT_DATA__ на странице")
        data = json.loads(match.group(1))
        return data["props"]["initialState"]["searchRides"]["rides"]

    rides = with_retries(source["title"], call)

    result: dict[str, dict] = {}
    for ride in rides:
        departure_full = str(ride.get("departure", ""))
        if not departure_full.startswith(date_str):
            continue
        departure = departure_full[11:16]
        if not in_window(departure):
            continue
        try:
            free = int(ride.get("freeSeats") or 0)
        except (TypeError, ValueError):
            free = 0
        carrier = (ride.get("carrier") or "").strip() or None
        previous = result.get(departure)
        if previous is None or free > previous["free"]:
            result[departure] = {"free": free, "carrier": carrier}
    return result


FETCHERS = {"transporter": fetch_transporter, "atlasbus": fetch_atlasbus}


# ---------------------------------------------------------------- утилиты


def to_dmy(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")


def weekday_ru(date_str: str) -> str:
    return WEEKDAYS[datetime.strptime(date_str, "%Y-%m-%d").weekday()]


def in_window(departure: str) -> bool:
    return bool(departure) and WINDOW_START <= departure <= WINDOW_END


def plural_seats(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "место"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "места"
    return "мест"


def format_free(count: int, capped: bool) -> str:
    if capped and count >= 2:
        return "≥2 места"
    return f"{count} {plural_seats(count)}"


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
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
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


def build_message(results: dict[str, dict], newly: list[tuple[str, str]], date_str: str) -> str:
    by_id = {source["id"]: source for source in SOURCES}
    lines = [
        "🎫 Свободные места: Могилёв → Минск",
        f"{datetime.strptime(date_str, '%Y-%m-%d').strftime('%d.%m.%Y')} "
        f"({weekday_ru(date_str)}), окно {WINDOW_START}–{WINDOW_END}",
        "",
        "Новое:",
    ]

    for source_id, departure in newly:
        source = by_id[source_id]
        entry = results[source_id]["available"][departure]
        carrier = f" — {entry['carrier']}" if entry.get("carrier") else ""
        lines.append(
            f"• {source['title']} — {departure} "
            f"({format_free(entry['free'], source['capped'])}){carrier}"
        )

    lines.append("")
    lines.append("Сейчас свободно:")

    for source in SOURCES:
        source_id = source["id"]
        result = results.get(source_id)
        if result is None:
            lines.append(f"• {source['title']} — ошибка проверки")
            continue
        available = result["available"]
        if not available:
            lines.append(f"• {source['title']} — мест нет")
            continue
        parts = [
            f"{departure} ({format_free(entry['free'], source['capped'])})"
            for departure, entry in sorted(available.items())
        ]
        lines.append(f"• {source['title']}: " + ", ".join(parts))

    lines.append("")
    lines.append("Проверить и бронировать:")
    for source in SOURCES:
        result = results.get(source["id"])
        if result and result["available"]:
            url = source["url"]
            if source["kind"] == "atlasbus":
                url = f"{url}?date={date_str}"
            lines.append(f"• {source['title']}: {url}")

    lines.append("")
    lines.append("Тел. Могилёв: 788-55-33, 788-66-33")
    return "\n".join(lines)


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
                "Отслеживаются mogilevminsk.by, avto-slava.by, atlasbus.by.",
                dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"ОШИБКА отправки: {exc}")
            return 1
        return 0

    sources = [
        source
        for source in SOURCES
        if not SOURCES_FILTER or source["id"] in SOURCES_FILTER
    ]

    log(
        f"проверка {TARGET_DATE} ({weekday_ru(TARGET_DATE)}), "
        f"окно {WINDOW_START}–{WINDOW_END}, источников: {len(sources)}"
    )

    state = load_state()
    previous_sources: dict[str, dict] = {}
    if state.get("date") == TARGET_DATE:
        previous_sources = state.get("sources") or {}

    results: dict[str, dict] = {}
    errors: list[str] = []

    for source in sources:
        try:
            rides = FETCHERS[source["kind"]](source, TARGET_DATE)
        except Exception as exc:  # noqa: BLE001
            log(f"  {source['title']}: ОШИБКА — {exc}")
            errors.append(source["title"])
            # сохраняем прошлое состояние источника, чтобы не прислать повторно
            results[source["id"]] = {
                "failed": True,
                "rides": {},
                "available": previous_sources.get(source["id"], {}),
            }
            continue

        available = {
            departure: entry
            for departure, entry in rides.items()
            if entry["free"] >= MIN_FREE_SEATS
        }
        results[source["id"]] = {
            "failed": False,
            "rides": rides,
            "available": available,
        }
        log(
            f"  {source['title']}: рейсов в окне {len(rides)}, "
            f"свободных {len(available)}"
        )

    if errors and len(errors) == len(sources):
        log("ОШИБКА: все источники недоступны")
        return 1

    if show_only:
        for source in sources:
            result = results[source["id"]]
            if result["failed"]:
                log(f"\n{source['title']}: ошибка проверки")
                continue
            log(f"\n{source['title']}:")
            if not result["rides"]:
                log("  рейсов в окне нет")
            for departure, entry in result["rides"].items():
                mark = "ЕСТЬ" if entry["free"] >= MIN_FREE_SEATS else "нет"
                log(
                    f"  {departure}  свободно={entry['free']:<3} {mark}"
                    + (f"  {entry['carrier']}" if entry.get("carrier") else "")
                )
        return 0

    newly: list[tuple[str, str]] = []
    for source in sources:
        source_id = source["id"]
        if results[source_id]["failed"]:
            continue
        previous = previous_sources.get(source_id, {})
        for departure in results[source_id]["available"]:
            if departure not in previous:
                newly.append((source_id, departure))

    if newly:
        message = build_message(results, newly, TARGET_DATE)
        try:
            send_telegram(message, dry_run)
        except Exception as exc:  # noqa: BLE001
            log(f"ОШИБКА отправки уведомления: {exc}")
            return 1

    new_state = {
        "date": TARGET_DATE,
        "sources": {
            source["id"]: sorted(results[source["id"]]["available"])
            for source in sources
        },
        "observed": {
            source["id"]: {
                departure: entry["free"]
                for departure, entry in sorted(results[source["id"]]["rides"].items())
            }
            for source in sources
        },
        "updated": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }

    source_ids = {source["id"] for source in sources}
    previous_effective = {
        key: sorted(value) if isinstance(value, list) else value
        for key, value in (state.get("sources") or {}).items()
        if key in source_ids
    }

    if state.get("date") == TARGET_DATE and previous_effective == new_state["sources"]:
        log("состояние не изменилось — state.json не трогаем")
    else:
        save_state(new_state)

    if not newly:
        log("новых свободных мест нет")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
