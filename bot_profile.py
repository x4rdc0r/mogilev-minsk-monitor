#!/usr/bin/env python3
"""Оформление профиля бота: имя, описания, команды, кнопка меню.

Запускается один раз локально:
    python bot_profile.py
Токен берётся из переменной окружения или .env.local.
"""

from __future__ import annotations

import json
import sys

import monitor

NAME = "Могилёв ⇄ Минск: места"
SHORT = "Следит за свободными местами на маршрутках Могилёв ⇄ Минск"
DESCRIPTION = (
    "Помогает поймать свободное место на маршрутке Могилёв ⇄ Минск.\n\n"
    "Проверяет mogilevminsk.by, avto-slava.by и atlasbus.by каждые ~5 минут. "
    "В меню настраиваются даты (до 3), направление и время выезда (до двух окон). "
    "Когда место освобождается — бот присылает уведомление.\n\n"
    "Бронь: на сайте перевозчика или по телефонам 788-55-33, 788-66-33."
)

COMMANDS = [
    {"command": "menu", "description": "Настройки мониторинга"},
    {"command": "status", "description": "Что свободно по последней проверке"},
    {"command": "check", "description": "Проверить сейчас"},
    {"command": "help", "description": "Справка"},
]


def main() -> int:
    steps = [
        ("setMyName", {"name": NAME}),
        ("setMyShortDescription", {"short_description": SHORT}),
        ("setMyDescription", {"description": DESCRIPTION}),
        ("setMyCommands", {"commands": json.dumps(COMMANDS, ensure_ascii=False)}),
        ("setChatMenuButton", {"menu_button": json.dumps({"type": "commands"})}),
    ]

    for method, payload in steps:
        try:
            monitor.tg_call(method, payload)
            print(f"{method}: ok")
        except Exception as exc:  # noqa: BLE001
            print(f"{method}: ОШИБКА — {exc}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
