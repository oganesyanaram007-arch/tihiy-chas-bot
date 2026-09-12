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

# Продуктовые числа живут в одном месте — content/product.json на сайте,
# откуда генерируется app/product.py. Здесь только реэкспорт, чтобы не
# править импорты по всему проекту. Руками эти значения тут не задавать.
from .product import (          # noqa: E402
    DEPOSIT,                    # ₽, комиссия платформы, платит гость
    DEPOSIT_CHARGED,            # списываются ли деньги сейчас
    CANCEL_FREE_HOURS,
    HORIZON_DAYS as BOOKING_DAYS_AHEAD,
)

# Баллы тоже из общего модуля: правил было три и все разные — гостю
# обещали одно, начислялось другое, бот считал по-третьему.
from .product import (           # noqa: E402
    POINTS_PER_VISIT as PTS_VISIT,
    POINTS_NEW_VENUE_MULT as PTS_NEW_MULT,
    POINTS_REFERRAL as PTS_REF,
)
