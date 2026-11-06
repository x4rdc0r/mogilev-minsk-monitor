#!/usr/bin/env python3
"""Telegram-бот с меню настроек мониторинга (Могилёв ⇄ Минск).

Бот работает в режиме long-polling и не требует веб-хука:
    python bot.py --local              # локально, настройки в config.json
    python bot.py --max-minutes 65     # в GitHub Actions с сохранением в git

Кнопки и команды:
    /menu, /start  — настройки
    /status        — что свободно по последней проверке
    /check         — внеочередная проверка
    /help          — справка
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import monitor

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
REPO = os.getenv("GITHUB_REPOSITORY", "").strip()
GH_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
LOCAL = "--local" in sys.argv

CONFIG_PATH = Path(monitor.CONFIG_FILE)
STATE_PATH = Path(monitor.STATE_FILE)

PRESETS = [
    ("Утро 06–12", "06:00", "12:00"),
    ("День 12–16", "12:00", "16:00"),
    ("Вечер 16–20", "16:00", "20:00"),
    ("16:30–20:00", "16:30", "20:00"),
    ("Весь день", "00:00", "23:59"),
]


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------- Telegram API


def call(method: str, payload: dict | None = None) -> dict:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(payload or {}).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"User-Agent": monitor.USER_AGENT})
    with urllib.request.urlopen(request, timeout=70) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(result.get("description") or "Telegram API error")
    return result["result"]


def send(chat_id: int | str, text: str, markup: dict | None = None) -> None:
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if markup:
        payload["reply_markup"] = json.dumps(markup, ensure_ascii=False)
    call("sendMessage", payload)


def edit(chat_id: int | str, message_id: int, text: str, markup: dict | None = None) -> None:
    payload = {
        "chat_id": str(chat_id),
        "message_id": str(message_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if markup:
        payload["reply_markup"] = json.dumps(markup, ensure_ascii=False)
    try:
        call("editMessageText", payload)
    except RuntimeError as exc:
        if "not modified" not in str(exc):
            raise


def button(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def keyboard(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": rows}


# ---------------------------------------------------------------- настройки


def load_cfg() -> dict:
    return monitor.load_config()


def save_cfg(cfg: dict, chat_id: int | str) -> None:
    payload = {
        "enabled": cfg["enabled"],
        "dates": cfg["dates"],
        "direction": cfg["direction"],
        "windows": cfg["windows"],
        "min_free_seats": cfg["min_free_seats"],
        "updated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "updated_by": str(chat_id),
    }
    CONFIG_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not LOCAL:
        git_commit("config: обновление настроек из бота")


def git_commit(message: str, attempts: int = 5) -> None:
    """Коммит config.json с перебазированием — монитор тоже пушит state.json."""
    identity = [
        "-c",
        "user.name=seat-bot",
        "-c",
        "user.email=seat-bot@users.noreply.github.com",
    ]
    subprocess.run(["git", "add", str(CONFIG_PATH.name)], check=False)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return

    subprocess.run(
        ["git", *identity, "commit", "-m", message],
        check=False,
        capture_output=True,
    )

    for attempt in range(attempts):
        # Идентичность нужна и при rebase: runner её не задаёт.
        pull = subprocess.run(
            ["git", *identity, "pull", "--rebase", "--autostash"],
            capture_output=True,
            text=True,
        )
        if pull.returncode != 0:
            log(f"git pull не удался (попытка {attempt + 1}): {pull.stderr.strip()[:200]}")
            subprocess.run(["git", "rebase", "--abort"], capture_output=True)
        else:
            push = subprocess.run(["git", "push"], capture_output=True, text=True)
            if push.returncode == 0:
                return
            log(f"git push не удался (попытка {attempt + 1}): {push.stderr.strip()[:200]}")
        time.sleep(2)

    raise RuntimeError("не удалось сохранить config.json в git")


# ---------------------------------------------------------------- экраны


def date_label(date_str: str) -> str:
    day = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{day.strftime('%d.%m')} {monitor.WEEKDAYS[day.weekday()]}"


def main_screen(cfg: dict) -> tuple[str, dict]:
    direction = monitor.DIRECTIONS[cfg["direction"]]
    dates = ", ".join(date_label(d) for d in cfg["dates"]) or "не выбраны"
    status = "🟢 Мониторинг: включён" if cfg["enabled"] else "🔴 Мониторинг: выключен"
    text = (
        "🚌 Мониторинг мест\n\n"
        f"📅 Даты: {dates}\n"
        f"🧭 Направление: {direction['title']}\n"
        f"🕐 Время: {monitor.windows_label(cfg['windows'])}\n"
        f"{status}\n\n"
        "Проверки идут автоматически каждые ~2–3 минуты."
    )
    rows = [
        [button("📅 Даты", "dates"), button("🧭 Направление", "dir")],
        [
            button("🕐 Время", "time"),
            button("▶️ Включить" if not cfg["enabled"] else "⏸ Выключить", "toggle"),
        ],
        [button("📊 Свободно сейчас", "status"), button("🔄 Проверить", "check")],
    ]
    return text, keyboard(rows)


def dates_screen(cfg: dict) -> tuple[str, dict]:
    today = (datetime.now(timezone.utc) + timedelta(hours=3)).date()
    rows: list[list[dict]] = []
    row: list[dict] = []
    for offset in range(14):
        day = today + timedelta(days=offset)
        iso = day.isoformat()
        mark = "✅ " if iso in cfg["dates"] else ""
        row.append(button(f"{mark}{day.strftime('%d.%m')} {monitor.WEEKDAYS[day.weekday()]}", f"d:{iso}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([button("✅ Готово", "menu")])

    text = (
        "📅 Даты мониторинга\n\n"
        f"Выбрано: {', '.join(date_label(d) for d in cfg['dates']) or 'ничего'}\n"
        f"Можно отметить до {monitor.MAX_DATES} дат.\n\n"
        "Сайты публикуют расписание на ~2 недели вперёд."
    )
    return text, keyboard(rows)


def time_screen(cfg: dict) -> tuple[str, dict]:
    rows: list[list[dict]] = []
    for title, start, end in PRESETS:
        active = cfg["windows"] == [{"start": start, "end": end}]
        prefix = "✅ " if active else ""
        rows.append([button(f"{prefix}{title}", f"w:{start}-{end}")])
    rows.append([button("✏️ Свой вариант", "wcustom")])
    rows.append([button("✅ Готово", "menu")])

    text = (
        "🕐 Время выезда\n\n"
        f"Сейчас: {monitor.windows_label(cfg['windows'])}\n\n"
        "Можно задать до двух окон текстом, например:\n"
        "<code>16:30-18:00, 19:00-20:00</code>"
    )
    return text, keyboard(rows)


def fetch_state() -> dict:
    if REPO:
        url = f"https://raw.githubusercontent.com/{REPO}/main/state.json?t={int(time.time())}"
        try:
            return json.loads(monitor._request(url, timeout=20))
        except Exception as exc:  # noqa: BLE001
            log(f"не удалось прочитать state.json из репозитория: {exc}")
    return monitor.load_state()


def minutes_ago(updated: str | None) -> str:
    if not updated:
        return "время неизвестно"
    try:
        moment = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError:
        return "время неизвестно"
    minutes = int((datetime.now(timezone.utc) - moment).total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    return f"{hours} ч {minutes % 60} мин назад"


def status_text(cfg: dict) -> str:
    state = fetch_state()
    if not state:
        return "📊 Данных пока нет — дождитесь первой проверки (до 2–3 минут)."

    state_direction = state.get("direction")
    direction = monitor.DIRECTIONS.get(state_direction or cfg["direction"])
    items = state.get("items") or {}
    observed = state.get("observed") or {}

    lines = [f"📊 Данные проверки: {minutes_ago(state.get('updated'))}"]
    if direction:
        lines.append(f"🧭 {direction['title']}")
    if state_direction and state_direction != cfg["direction"]:
        lines.append("⚠️ В настройках уже другое направление — данные обновятся после проверки.")

    sources = {source["id"]: source for source in monitor.SOURCES}
    for date_str, per_source in items.items():
        lines.append("")
        lines.append(f"📅 {datetime.strptime(date_str, '%Y-%m-%d').strftime('%d.%m.%Y')}")
        for source_id, source in sources.items():
            times = per_source.get(source_id) or []
            if not times:
                lines.append(f"• {source['title']} — мест нет")
                continue
            counts = (observed.get(date_str) or {}).get(source_id) or {}
            parts = []
            for departure in times:
                free = counts.get(departure)
                parts.append(
                    f"{departure} ({monitor.format_free(int(free), source['cap'])})"
                    if isinstance(free, int)
                    else departure
                )
            lines.append(f"• {source['title']}: " + ", ".join(parts))

    if not items:
        lines.append("\nПока нет данных по выбранным датам.")
    return "\n".join(lines)


def status_markup() -> dict:
    return keyboard([[button("🔄 Обновить", "status"), button("⬅️ В меню", "menu")]])


def help_text() -> str:
    return (
        "ℹ️ Как пользоваться\n\n"
        "/menu — настройки\n"
        "/status — что свободно по последней проверке\n"
        "/check — внеочередная проверка\n\n"
        "📅 Даты — до 3 дат\n"
        "🧭 Направление — Могилёв → Минск или обратно\n"
        "🕐 Время — окна выезда (до двух)\n\n"
        "Бот сам проверяет mogilevminsk.by, avto-slava.by и atlasbus.by "
        "каждые ~2–3 минуты и пишет, когда освобождается место."
    )


# ---------------------------------------------------------------- действия


def trigger_check() -> str:
    if LOCAL or not REPO or not GH_TOKEN:
        return (
            "🔄 Локальный режим: запустите проверку командой\n"
            "<code>python monitor.py</code>"
        )
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/monitor.yml/dispatches"
    body = json.dumps({"ref": "main", "inputs": {"once": "true"}}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": monitor.USER_AGENT,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ Не удалось запустить проверку: {html.escape(str(exc))}"
    return "🔄 Проверка запущена. Если место есть — уведомление придёт в течение минуты."


# ---------------------------------------------------------------- сторож монитора

ACTIVE_START_UTC = 5   # 08:00 по Минску
ACTIVE_END_UTC = 22    # 01:00 по Минску
WATCHDOG_EVERY = 300   # как часто проверять цепочку, секунд


def dispatch_chain() -> bool:
    """Ставит в очередь длинную цепочку монитора."""
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/monitor.yml/dispatches"
    body = json.dumps({"ref": "main"}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": monitor.USER_AGENT,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except Exception as exc:  # noqa: BLE001
        log(f"сторож: не удалось запустить цепочку: {exc}")
        return False
    return True


def monitor_running() -> bool:
    """Есть ли активный или ожидающий прогон монитора."""
    url = (
        f"https://api.github.com/repos/{REPO}/actions/workflows/monitor.yml/runs"
        "?per_page=10"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": monitor.USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    return any(
        run.get("status") in {"in_progress", "queued", "pending", "waiting"}
        for run in data.get("workflow_runs", [])
    )


def stop_monitor() -> None:
    """Отменяет активные прогоны монитора при выключении."""
    if LOCAL or not REPO or not GH_TOKEN:
        return
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": monitor.USER_AGENT,
    }
    try:
        url = (
            f"https://api.github.com/repos/{REPO}/actions/workflows/monitor.yml/runs"
            "?status=in_progress&per_page=10"
        )
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=30
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
        for run in data.get("workflow_runs", []):
            cancel = urllib.request.Request(
                f"https://api.github.com/repos/{REPO}/actions/runs/{run['id']}/cancel",
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(cancel, timeout=30) as response:
                response.read()
    except Exception as exc:  # noqa: BLE001
        log(f"сторож: не удалось остановить монитор: {exc}")


def ensure_monitor() -> None:
    """Поднимает цепочку, если она не идёт в активные часы."""
    if LOCAL or not REPO or not GH_TOKEN:
        return
    try:
        if not monitor.load_config().get("enabled", True):
            return
    except Exception as exc:  # noqa: BLE001
        log(f"сторож: не удалось прочитать настройки: {exc}")
        return
    if not (ACTIVE_START_UTC <= datetime.now(timezone.utc).hour < ACTIVE_END_UTC):
        return
    try:
        if monitor_running():
            return
    except Exception as exc:  # noqa: BLE001
        log(f"сторож: не удалось узнать статус монитора: {exc}")
        return
    if dispatch_chain():
        log("сторож: цепочка монитора запущена")


def parse_windows(text: str) -> list[dict] | None:
    windows: list[dict] = []
    for chunk in re.split(r"[,;]", text):
        chunk = chunk.strip().replace("–", "-").replace("—", "-")
        if not chunk:
            continue
        if "-" not in chunk:
            return None
        start, _, end = chunk.partition("-")
        start, end = normalize_time(start.strip()), normalize_time(end.strip())
        if not start or not end or start >= end:
            return None
        windows.append({"start": start, "end": end})
    if not windows or len(windows) > monitor.MAX_WINDOWS:
        return None
    return windows


def normalize_time(value: str) -> str | None:
    match = re.match(r"^(\d{1,2}):(\d{2})$", value)
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return f"{hours:02d}:{minutes:02d}"


# ---------------------------------------------------------------- обработка


def allowed(chat_id: int | str) -> bool:
    return not CHAT_ID or str(chat_id) == str(CHAT_ID)


def handle_callback(callback: dict) -> None:
    data = callback.get("data") or ""
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    callback_id = callback.get("id")

    if chat_id is None or message_id is None:
        return
    if not allowed(chat_id):
        call("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Нет доступа"})
        return

    cfg = load_cfg()
    alert: str | None = None

    if data == "menu" or data == "main":
        text, markup = main_screen(cfg)
        edit(chat_id, message_id, text, markup)
    elif data == "dates":
        text, markup = dates_screen(cfg)
        edit(chat_id, message_id, text, markup)
    elif data.startswith("d:"):
        date_str = data[2:]
        if date_str in cfg["dates"]:
            if len(cfg["dates"]) > 1:
                cfg["dates"] = [d for d in cfg["dates"] if d != date_str]
                save_cfg(cfg, chat_id)
            else:
                alert = "Нужна хотя бы одна дата"
        elif len(cfg["dates"]) >= monitor.MAX_DATES:
            alert = f"Максимум {monitor.MAX_DATES} даты"
        else:
            cfg["dates"] = sorted(cfg["dates"] + [date_str])
            save_cfg(cfg, chat_id)
        text, markup = dates_screen(cfg)
        edit(chat_id, message_id, text, markup)
    elif data == "dir":
        cfg["direction"] = (
            "minsk-mogilev" if cfg["direction"] == "mogilev-minsk" else "mogilev-minsk"
        )
        save_cfg(cfg, chat_id)
        text, markup = main_screen(cfg)
        edit(chat_id, message_id, text, markup)
    elif data == "time":
        text, markup = time_screen(cfg)
        edit(chat_id, message_id, text, markup)
    elif data.startswith("w:"):
        start, _, end = data[2:].partition("-")
        if normalize_time(start) and normalize_time(end):
            cfg["windows"] = [{"start": start, "end": end}]
            save_cfg(cfg, chat_id)
        text, markup = time_screen(cfg)
        edit(chat_id, message_id, text, markup)
    elif data == "wcustom":
        alert = "Пришлите сообщением, например: 16:30-18:00, 19:00-20:00"
    elif data == "toggle":
        cfg["enabled"] = not cfg["enabled"]
        save_cfg(cfg, chat_id)
        if cfg["enabled"]:
            ensure_monitor()
        else:
            stop_monitor()
        text, markup = main_screen(cfg)
        edit(chat_id, message_id, text, markup)
    elif data == "status":
        edit(chat_id, message_id, status_text(cfg), status_markup())
    elif data == "check":
        edit(chat_id, message_id, trigger_check(), keyboard([[button("⬅️ В меню", "menu")]]))
    else:
        text, markup = main_screen(cfg)
        edit(chat_id, message_id, text, markup)

    answer = {"callback_query_id": callback_id}
    if alert:
        answer["text"] = alert
        answer["show_alert"] = "true"
    call("answerCallbackQuery", answer)


def handle_message(message: dict) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    if chat_id is None or not text:
        return
    if not allowed(chat_id):
        send(chat_id, "Нет доступа")
        return

    cfg = load_cfg()
    command = text.split("@")[0].lower()

    if command in ("/start", "/menu"):
        body, markup = main_screen(cfg)
        send(chat_id, body, markup)
        return
    if command == "/status":
        send(chat_id, status_text(cfg), status_markup())
        return
    if command == "/check":
        send(chat_id, trigger_check(), keyboard([[button("⬅️ В меню", "menu")]]))
        return
    if command == "/help":
        send(chat_id, help_text(), keyboard([[button("⬅️ В меню", "menu")]]))
        return

    windows = parse_windows(text)
    if windows:
        cfg["windows"] = windows
        save_cfg(cfg, chat_id)
        body, markup = time_screen(cfg)
        send(chat_id, "🕐 Время сохранено.\n\n" + body, markup)
        return

    send(
        chat_id,
        "Не понял. Откройте меню: /menu\n"
        "Или пришлите окно времени, например: <code>16:30-20:00</code>",
        keyboard([[button("📅 Настройки", "menu")]]),
    )


# ---------------------------------------------------------------- цикл


def handle_update(update: dict) -> None:
    if "callback_query" in update:
        handle_callback(update["callback_query"])
    elif "message" in update:
        handle_message(update["message"])


def run(max_minutes: int) -> None:
    deadline = time.time() + max_minutes * 60
    offset: int | None = None
    last_pull = time.time()
    last_watchdog = 0.0

    try:
        backlog = call("getUpdates", {"offset": "-1", "limit": "1", "timeout": "0"})
        if backlog:
            offset = backlog[-1]["update_id"] + 1
    except Exception as exc:  # noqa: BLE001
        log(f"не удалось прочитать очередь обновлений: {exc}")

    log(f"бот запущен, режим {'локальный' if LOCAL else 'GitHub Actions'}, до {max_minutes} мин")

    while time.time() < deadline:
        if time.time() - last_watchdog >= WATCHDOG_EVERY:
            ensure_monitor()
            last_watchdog = time.time()
        try:
            updates = call(
                "getUpdates",
                {
                    "offset": str(offset or 0),
                    "timeout": "50",
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                },
            )
        except Exception as exc:  # noqa: BLE001
            log(f"ошибка getUpdates: {exc}")
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            try:
                handle_update(update)
            except Exception as exc:  # noqa: BLE001
                log(f"ошибка обработки обновления {update.get('update_id')}: {exc}")


def main() -> int:
    max_minutes = 60
    if "--max-minutes" in sys.argv:
        index = sys.argv.index("--max-minutes")
        if index + 1 < len(sys.argv):
            try:
                max_minutes = int(sys.argv[index + 1])
            except ValueError:
                pass

    if not TOKEN:
        log("ОШИБКА: TELEGRAM_BOT_TOKEN не задан")
        return 1

    try:
        run(max_minutes)
    except KeyboardInterrupt:
        log("остановлено пользователем")
    return 0


if __name__ == "__main__":
    sys.exit(main())
