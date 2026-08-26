# -*- coding: utf-8 -*-
"""Конфигурация «Тихий Час» бота. Все секреты — в .env."""
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
# ID администраторов через запятую: "12345678,87654321"
ADMIN_IDS: set[int] = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
# URL размещённого Mini App (наш tihiy-chas-miniapp-v2.html на https-хостинге).
# Пусто — кнопка приложения просто не показывается.
MINIAPP_URL: str = os.getenv("MINIAPP_URL", "")
DB_URL: str = os.getenv("DB_URL", "sqlite+aiosqlite:///tihiy.db")

DEPOSIT = 99           # единый депозит, ₽ — синхронизировано с сайтом (было 149)
COMMISSION = 99        # единая комиссия за визит, ₽ — раньше 99/149 по категориям
PTS_VISIT = 10         # баллов за подтверждённый визит
PTS_NEW_MULT = 2       # ×2 за первый визит в новое заведение
PTS_REF = 200          # рефералка: обоим
CANCEL_FREE_HOURS = 3  # бесплатная отмена за N часов
BOOKING_DAYS_AHEAD = 14  # на сколько дней вперёд можно бронировать — как на сайте
