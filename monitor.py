#!/usr/bin/env python3
"""Мониторинг свободных мест Могилёв ⇄ Минск по настройкам из config.json.

Источники:
  * mogilevminsk.by — API платформы Transporter (free_seats, показ с потолком 2);
  * avto-slava.by   — та же платформа, тот же API;
  * atlasbus.by     — Next.js: рейсы лежат в __NEXT_DATA__ страницы маршрута ?date=.

Настройки (config.json) задаёт Telegram-бот, но файл можно править и руками.

Запуск:
    python monitor.py            # проверка + уведомление в Telegram
    python monitor.py --show     # показать доступность без уведомлений
    python monitor.py --dry-run  # проверка без реальной отправки
    python monitor.py --test     # тестовое сообщение в Telegram
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
MAX_DATES = 3
MAX_WINDOWS = 2

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

DIRECTIONS: dict[str, dict] = {
    "mogilev-minsk": {
        "title": "Могилёв → Минск",
        "from": "Могилёв",
        "to": "Минск",
        "from_city": "2",
        "dest_city": "1",
        "phones": "788-55-33, 788-66-33",
    },
    "minsk-mogilev": {
        "title": "Минск → Могилёв",
        "from": "Минск",
        "to": "Могилёв",
        "from_city": "1",
        "dest_city": "2",
        "phones": "788-55-33, 788-66-33",
    },
}

SOURCES: list[dict] = [
    {
        "id": "mogilevminsk",
        "title": "mogilevminsk.by",
        "kind": "transporter",
        "base": "https://mogilevminsk.by",
        "cap": 2,
        "url": "https://mogilevminsk.by/timetable",
    },
    {
        "id": "avtoslava",
        "title": "avto-slava.by",
        "kind": "transporter",
        "base": "https://avto-slava.by",
        "cap": 4,
        "url": "https://avto-slava.by/timetable",
    },
    {
        "id": "atlasbus",
        "title": "atlasbus.by",
        "kind": "atlasbus",
        "base": "https://atlasbus.by",
        "cap": None,
        "url": "",
    },
]


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

CONFIG_FILE = Path(os.getenv("CONFIG_FILE", "config.json"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

DEFAULTS: dict = {
    "enabled": True,
    "dates": [os.getenv("TARGET_DATE", "2026-11-08")],
    "direction": os.getenv("DIRECTION", "mogilev-minsk"),
    "windows": [
        {
            "start": os.getenv("WINDOW_START", "16:30"),
            "end": os.getenv("WINDOW_END", "20:00"),
        }
    ],
    "min_free_seats": int(os.getenv("MIN_FREE_SEATS", "1")),
}


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------- конфиг


def valid_date(value: str) -> bool:
    if not DATE_RE.match(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def normalize_window(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    start = str(raw.get("start", "")).strip()
    end = str(raw.get("end", "")).strip()
    if not TIME_RE.match(start) or not TIME_RE.match(end) or start >= end:
        return None
    return {"start": start, "end": end}


def load_config() -> dict:
    raw: dict = {}
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, ValueError):
        pass

    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if v is not None}}

    if isinstance(raw.get("dates"), list):
        cfg["dates"] = sorted({d for d in raw["dates"] if isinstance(d, str) and valid_date(d)})[
            :MAX_DATES
        ]
    else:
        cfg["dates"] = [d for d in DEFAULTS["dates"] if valid_date(d)]

    if cfg.get("direction") not in DIRECTIONS:
        cfg["direction"] = DEFAULTS["direction"]

    if isinstance(raw.get("windows"), list):
        windows = [w for w in (normalize_window(item) for item in raw["windows"]) if w]
        cfg["windows"] = windows[:MAX_WINDOWS]
    else:
        cfg["windows"] = [normalize_window(w) for w in DEFAULTS["windows"] if normalize_window(w)]

    try:
        cfg["min_free_seats"] = max(1, int(cfg.get("min_free_seats") or 1))
    except (TypeError, ValueError):
        cfg["min_free_seats"] = 1

    cfg["enabled"] = bool(cfg.get("enabled", True))
    return cfg


def windows_label(windows: list[dict]) -> str:
    if not windows:
        return "любое время"
    return ", ".join(f"{w['start']}–{w['end']}" for w in windows)


def in_window(departure: str, windows: list[dict]) -> bool:
    if not windows:
        return True
    return any(w["start"] <= departure <= w["end"] for w in windows)


# ---------------------------------------------------------------- HTTP


def _request(
    url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 40
) -> str:
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


def to_dmy(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")


def fetch_transporter(source: dict, date_str: str, direction: dict) -> dict[str, dict]:
    """Сайты на платформе Transporter: /timetable/trips/ отдаёт free_seats."""
    body, headers = _multipart(
        {
            "date": date_str,
            "from_city": direction["from_city"],
            "dest_city": direction["dest_city"],
        }
    )

    def call() -> list[dict]:
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
        try:
            free = int(trip.get("free_seats") or 0)
        except (TypeError, ValueError):
            free = 0
        previous = result.get(departure)
        if previous is None or free > previous["free"]:
            result[departure] = {"free": free, "carrier": None}
    return result


def fetch_atlasbus(source: dict, date_str: str, direction: dict) -> dict[str, dict]:
    """Atlasbus (Next.js): рейсы на дату лежат в __NEXT_DATA__ страницы маршрута."""
    path = "/".join(urllib.parse.quote(part) for part in ("Маршруты", direction["from"], direction["to"]))
    url = f"{source['base']}/{path}?date={date_str}"

    def call() -> list[dict]:
        html = _request(url, timeout=60)
        match = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
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


# ---------------------------------------------------------------- сбор данных


def collect(config: dict) -> tuple[dict, list[str]]:
    direction = DIRECTIONS[config["direction"]]
    results: dict[str, dict] = {}
    errors: list[str] = []

    for date_str in config["dates"]:
        per_source: dict[str, dict] = {}
        for source in SOURCES:
            try:
                rides = FETCHERS[source["kind"]](source, date_str, direction)
            except Exception as exc:  # noqa: BLE001
                log(f"  {date_str} {source['title']}: ОШИБКА — {exc}")
                errors.append(f"{date_str} {source['title']}")
                per_source[source["id"]] = {"failed": True, "rides": {}, "available": {}}
                continue

            rides = {
                departure: entry
                for departure, entry in rides.items()
                if in_window(departure, config["windows"])
            }
            available = {
                departure: entry
                for departure, entry in rides.items()
                if entry["free"] >= config["min_free_seats"]
            }
            per_source[source["id"]] = {"failed": False, "rides": rides, "available": available}
            log(
                f"  {date_str} {source['title']}: рейсов {len(rides)}, свободных {len(available)}"
            )
        results[date_str] = per_source

    return results, errors


# ---------------------------------------------------------------- состояние


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
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
    )
    log(f"уведомление отправлено в чат {chat_id}")


# ---------------------------------------------------------------- сообщение


def plural_seats(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "место"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "места"
    return "мест"


def format_free(count: int, cap: int | None) -> str:
    """Сайты Transporter отдают места с потолком (2 и 4) — показываем «≥N»."""
    if cap and count >= cap:
        return f"≥{cap} {plural_seats(cap)}"
    return f"{count} {plural_seats(count)}"


def source_url(source: dict, config: dict, date_str: str) -> str:
    if source["kind"] == "atlasbus":
        direction = DIRECTIONS[config["direction"]]
        return (
            f"https://atlasbus.by/Маршруты/{direction['from']}/{direction['to']}"
            f"?date={date_str}"
        )
    return source["url"]


def build_message(
    config: dict, results: dict, newly: list[tuple[str, str, str]]
) -> str:
    direction = DIRECTIONS[config["direction"]]
    by_id = {source["id"]: source for source in SOURCES}

    lines = [
        f"🎫 Свободные места: {direction['title']}",
        f"Окно: {windows_label(config['windows'])}",
    ]

    for date_str in config["dates"]:
        date_label = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
        weekday = WEEKDAYS[datetime.strptime(date_str, "%Y-%m-%d").weekday()]
        lines.append("")
        lines.append(f"📅 {date_label} ({weekday})")

        date_new = [item for item in newly if item[0] == date_str]
        if date_new:
            lines.append("Новое:")
            for _, source_id, departure in date_new:
                entry = results[date_str][source_id]["available"][departure]
                carrier = f" — {entry['carrier']}" if entry.get("carrier") else ""
                lines.append(
                    f"• {by_id[source_id]['title']} — {departure} "
                    f"({format_free(entry['free'], by_id[source_id]['cap'])}){carrier}"
                )
            lines.append("")

        lines.append("Сейчас свободно:")
        for source in SOURCES:
            result = results[date_str].get(source["id"]) or {}
            if result.get("failed"):
                lines.append(f"• {source['title']} — ⚠️ ошибка проверки")
                continue
            available = result.get("available") or {}
            if not available:
                lines.append(f"• {source['title']} — мест нет")
                continue
            parts = [
                f"{departure} ({format_free(entry['free'], source['cap'])})"
                for departure, entry in sorted(available.items())
            ]
            lines.append(f"• {source['title']}: " + ", ".join(parts))

        links = []
        for source in SOURCES:
            result = results[date_str].get(source["id"]) or {}
            if result.get("available"):
                links.append(f"• {source['title']}: {source_url(source, config, date_str)}")
        if links:
            lines.append("")
            lines.append("Бронировать:")
            lines.extend(links)

    lines.append("")
    lines.append(f"Тел.: {direction['phones']}")
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

    config = load_config()

    if test_send:
        direction = DIRECTIONS[config["direction"]]
        try:
            send_telegram(
                f"✅ Тест монитора мест: {direction['title']}.\n"
                f"Даты: {', '.join(config['dates']) or '—'}\n"
                f"Окно: {windows_label(config['windows'])}\n"
                "Отслеживаются mogilevminsk.by, avto-slava.by, atlasbus.by.",
                dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"ОШИБКА отправки: {exc}")
            return 1
        return 0

    direction = DIRECTIONS[config["direction"]]
    log(
        f"проверка {direction['title']}, даты: {', '.join(config['dates']) or '—'}, "
        f"окно: {windows_label(config['windows'])}"
    )

    if not config["enabled"]:
        log("мониторинг выключен в настройках — выхожу")
        return 0
    if not config["dates"]:
        log("даты не выбраны — выхожу")
        return 0

    results, errors = collect(config)

    previous = load_state()
    previous_items = previous.get("items") or {}
    same_direction = previous.get("direction") == config["direction"]

    # Упавший источник не должен терять своё состояние: переносим прошлые данные.
    for date_str, per_source in results.items():
        for source_id, result in per_source.items():
            if not result["failed"]:
                continue
            carried = (previous_items.get(date_str) or {}).get(source_id) or []
            result["available"] = {
                departure: {"free": 0, "carrier": None, "carried": True}
                for departure in carried
            }

    if show_only:
        for date_str, per_source in results.items():
            log(f"\n{date_str}:")
            for source in SOURCES:
                result = per_source[source["id"]]
                if result["failed"]:
                    log(f"  {source['title']}: ошибка проверки")
                    continue
                log(f"  {source['title']}:")
                if not result["rides"]:
                    log("    рейсов в окне нет")
                for departure, entry in sorted(result["rides"].items()):
                    mark = "ЕСТЬ" if entry["free"] >= config["min_free_seats"] else "нет"
                    carrier = f"  {entry['carrier']}" if entry.get("carrier") else ""
                    log(f"    {departure}  свободно={entry['free']:<3} {mark}{carrier}")
        return 0

    if errors and len(errors) == sum(len(config["dates"]) for _ in SOURCES):
        log("ОШИБКА: все источники недоступны")
        return 1

    newly: list[tuple[str, str, str]] = []
    for date_str, per_source in results.items():
        for source_id, result in per_source.items():
            if result["failed"]:
                continue
            before = set()
            if same_direction:
                before = set((previous_items.get(date_str) or {}).get(source_id) or [])
            for departure in result["available"]:
                if departure not in before:
                    newly.append((date_str, source_id, departure))

    if newly:
        message = build_message(config, results, newly)
        try:
            send_telegram(message, dry_run)
        except Exception as exc:  # noqa: BLE001
            log(f"ОШИБКА отправки уведомления: {exc}")
            return 1

    new_state = {
        "updated": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "direction": config["direction"],
        "items": {
            date_str: {
                source_id: sorted(result["available"])
                for source_id, result in per_source.items()
            }
            for date_str, per_source in results.items()
        },
        "observed": {
            date_str: {
                source_id: {
                    departure: entry["free"]
                    for departure, entry in sorted(result["rides"].items())
                }
                for source_id, result in per_source.items()
            }
            for date_str, per_source in results.items()
        },
    }

    # updated — время последней проверки, а не изменения наличия мест.
    # Сохраняем и свежие наблюдения, даже если список свободных мест прежний.
    save_state(new_state)

    if not newly:
        log("новых свободных мест нет")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
