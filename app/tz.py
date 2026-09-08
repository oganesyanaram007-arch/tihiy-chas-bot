# -*- coding: utf-8 -*-
"""Время в одном месте.

Сервис живёт в Петербурге, а сервер может стоять где угодно и обычно
живёт в UTC. Пока каждый модуль звал `date.today()` сам, «сегодня»
у гостя и у заведения расходилось на три часа: после полуночи по Москве
гость видел бронь сегодняшней, а кабинет партнёра — завтрашней.

Правило: в базе всё в UTC (наивные datetime, как было), на экране —
Europe/Moscow. Любой код, которому нужно «сейчас» или «сегодня»
по-человечески, берёт это отсюда, а не из datetime напрямую.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
UTC = dt.timezone.utc


def msk_now() -> dt.datetime:
    """Текущий момент в московском времени (с таймзоной)."""
    return dt.datetime.now(MSK)


def msk_today() -> dt.date:
    """Какое сегодня число в Петербурге."""
    return msk_now().date()


def utc_now() -> dt.datetime:
    """Момент для записи в базу: UTC, без таймзоны — как хранят колонки."""
    return dt.datetime.now(UTC).replace(tzinfo=None)


def to_msk(value: dt.datetime) -> dt.datetime:
    """Наивный UTC из базы → московское время для показа."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(MSK)


def slot_start_msk(visit_date: dt.date, hour: int) -> dt.datetime:
    """Начало тихого окна в московском времени."""
    return dt.datetime.combine(visit_date, dt.time(hour=hour), tzinfo=MSK)


def fmt_dt(value: dt.datetime) -> str:
    """Единый формат даты и времени для гостя, бота и кабинета: 09.09 в 15:30."""
    return to_msk(value).strftime("%d.%m в %H:%M")


def fmt_slot(visit_date: dt.date, hour: int) -> str:
    """Единый формат окна: 09.09, 15:00–16:00."""
    return f"{visit_date.strftime('%d.%m')}, {hour}:00–{hour + 1}:00"
