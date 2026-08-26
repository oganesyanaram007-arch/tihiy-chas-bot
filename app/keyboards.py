# -*- coding: utf-8 -*-
"""Инлайн-клавиатуры бота. Район и день теперь часть навигации,
как на сайте: гость сначала решает где и когда, потом — что."""
import datetime as dt

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (InlineKeyboardButton as B, InlineKeyboardMarkup,
                           WebAppInfo)
from .config import MINIAPP_URL

CATS = [("all", "Все"), ("food", "Еда"), ("coffee", "Кофе"),
        ("beauty", "Красота"), ("spa", "СПА"), ("fun", "Развлечения"),
        ("auto", "Авто")]
CAT_NAME = dict(CATS)
CAT_ICON = {"food": "🍽", "coffee": "☕️", "beauty": "💅", "spa": "🧖",
            "fun": "🗝", "auto": "🚿", "all": "✨"}

# 15 районов — 1:1 со списком на сайте.
DISTRICTS = [
    ("all", "Весь Петербург"), ("petro", "Петроградский"), ("centr", "Центральный"),
    ("adm", "Адмиралтейский"), ("vasil", "Василеостровский"), ("mosk", "Московский"),
    ("nev", "Невский"), ("primor", "Приморский"), ("vyb", "Выборгский"),
    ("kalin", "Калининский"), ("kirov", "Кировский"), ("frunz", "Фрунзенский"),
    ("krasnog", "Красногвардейский"), ("krasnos", "Красносельский"), ("push", "Пушкинский"),
]
DIST_NAME = dict(DISTRICTS)

# Показываем в ленте только ближайшую неделю — 14 дней вперёд доступны,
# но выбор в один тап логичнее ограничить, как в кнопках, а не в скролле сайта.
DAY_BUTTONS = 7
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def date_for(offset: int) -> dt.date:
    return dt.date.today() + dt.timedelta(days=offset)


def day_label(offset: int) -> str:
    if offset == 0:
        return "Сегодня"
    if offset == 1:
        return "Завтра"
    d = date_for(offset)
    return f"{d.day} {WEEKDAYS_SHORT[d.weekday()]}"


class DistCB(CallbackData, prefix="dist"):
    dist: str
    cat: str
    day: int


class CatCB(CallbackData, prefix="cat"):
    cat: str
    dist: str
    day: int


class DayCB(CallbackData, prefix="day"):
    day: int
    dist: str
    cat: str


class VenueCB(CallbackData, prefix="ven"):
    venue_id: int
    dist: str
    cat: str
    day: int


class VenueDayCB(CallbackData, prefix="vday"):
    venue_id: int
    day: int
    dist: str
    cat: str


class BookCB(CallbackData, prefix="book"):
    slot_id: int
    day: int


class CancelCB(CallbackData, prefix="cancel"):
    booking_id: int


class MenuCB(CallbackData, prefix="menu"):
    to: str  # feed / bookings / points / partner / how / home


def kb_main() -> InlineKeyboardMarkup:
    rows = [[B(text="🕑 Тихие окна рядом", callback_data=MenuCB(to="feed").pack())],
            [B(text="🎟 Мои брони", callback_data=MenuCB(to="bookings").pack()),
             B(text="✨ Мои баллы", callback_data=MenuCB(to="points").pack())],
            [B(text="🏪 Подключить заведение", callback_data=MenuCB(to="partner").pack())],
            [B(text="🔑 Кабинет партнёра", callback_data=MenuCB(to="cabinet").pack())],
            [B(text="ℹ️ Как это работает", callback_data=MenuCB(to="how").pack())]]
    if MINIAPP_URL:
        rows.insert(1, [B(text="▶️ Открыть приложение",
                          web_app=WebAppInfo(url=MINIAPP_URL))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_days(dist: str, cat: str, active: int) -> list[list[B]]:
    """Ряд из 7 дней — тот же смысл, что полоса дат на сайте."""
    row = [B(text=("· " if d == active else "") + day_label(d),
             callback_data=DayCB(day=d, dist=dist, cat=cat).pack())
           for d in range(DAY_BUTTONS)]
    return [row[:4], row[4:]]


def kb_dists(cat: str, day: int, active: str) -> list[list[B]]:
    btns = [B(text=("· " if d == active else "") + n,
              callback_data=DistCB(dist=d, cat=cat, day=day).pack())
            for d, n in DISTRICTS]
    return [btns[i:i + 2] for i in range(0, len(btns), 2)]


def kb_cats(dist: str, day: int, active: str) -> list[list[B]]:
    row1 = [B(text=("· " if c == active else "") + n,
              callback_data=CatCB(cat=c, dist=dist, day=day).pack()) for c, n in CATS[:4]]
    row2 = [B(text=("· " if c == active else "") + n,
              callback_data=CatCB(cat=c, dist=dist, day=day).pack()) for c, n in CATS[4:]]
    return [row1, row2]


def kb_feed(venues, dist: str, cat: str, day: int) -> InlineKeyboardMarkup:
    """Полная лента: дни → районы → категории → заведения."""
    rows = kb_days(dist, cat, day)
    rows += kb_dists(cat, day, dist)
    rows += kb_cats(dist, day, cat)
    rows += [[B(text=f"{CAT_ICON.get(v.cat,'•')} {v.name} · −{maxd}%",
                callback_data=VenueCB(venue_id=v.id, dist=dist, cat=cat, day=day).pack())]
             for v, maxd in venues]
    rows.append([B(text="‹ Меню", callback_data=MenuCB(to="home").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_venue(slots, venue_id: int, dist: str, cat: str, day: int,
            venue_days: list[int]) -> InlineKeyboardMarkup:
    """Карточка заведения: свои рабочие дни + слоты на выбранный день."""
    day_row = [B(text=("· " if d == day else "") + day_label(d),
                 callback_data=VenueDayCB(venue_id=venue_id, day=d, dist=dist, cat=cat).pack())
               for d in venue_days[:5]]
    slot_row = [B(text=f"{s.hour}:00 · −{s.discount}%",
                  callback_data=BookCB(slot_id=s.id, day=day).pack()) for s in slots]
    rows = [day_row] if day_row else []
    rows += [slot_row[i:i + 3] for i in range(0, len(slot_row), 3)] if slot_row else \
            [[B(text="Нет свободных часов в этот день", callback_data="noop")]]
    rows.append([B(text="‹ К списку",
                   callback_data=CatCB(cat=cat, dist=dist, day=day).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_bookings(bookings) -> InlineKeyboardMarkup:
    rows = [[B(text=f"✕ Отменить {b.code}",
               callback_data=CancelCB(booking_id=b.id).pack())]
            for b in bookings]
    rows.append([B(text="‹ Меню", callback_data=MenuCB(to="home").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [B(text="‹ Меню", callback_data=MenuCB(to="home").pack())]])
