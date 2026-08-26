# -*- coding: utf-8 -*-
"""Гостевой флоу: /start, лента тихих окон (день × район × категория),
бронирование с QR на выбранную дату, брони, баллы.

Синхронизировано с сайтом (tihiy-chas-v10.html): единый депозит и
комиссия 99 ₽, бронь на 14 дней вперёд, 15 районов, формулировка QR —
«за вами закрепят столик и отметят в системе»."""
from __future__ import annotations
import datetime as dt
import io
import random

import qrcode
from aiogram import F, Router
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (BufferedInputFile, CallbackQuery,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import select

from ..config import CANCEL_FREE_HOURS, DEPOSIT, PTS_REF
from ..db import (Booking, Session, Slot, Venue, add_points,
                  get_or_create_user, venue_weekdays)
from ..keyboards import (CAT_ICON, CAT_NAME, DAY_BUTTONS, DIST_NAME, BookCB,
                         CancelCB, CatCB, DayCB, DistCB, MenuCB, VenueCB,
                         VenueDayCB, date_for, day_label, kb_back,
                         kb_bookings, kb_feed, kb_main, kb_venue)

router = Router()

WELCOME = ("<b>Тихий Час</b> — лучшее в городе, в его тихие часы. 🕑\n\n"
           "Рестораны, кофейни и салоны снижают цены на 25–50%, "
           "когда у них свободно. Выбирайте район, день и окно — "
           "и приходите за скидкой на весь счёт.\n\n"
           "Пилот идёт по Санкт-Петербургу, 15 районов.")

HOW = ("<b>Как это работает</b>\n\n"
       "1️⃣ Выберите день, район и час — когда заведение даёт скидку 25–50%.\n"
       f"2️⃣ Забронируйте. Депозит {DEPOSIT} ₽ целиком зачитывается в счёт "
       "(на пилоте бронь бесплатная — платежи включим после старта).\n"
       "3️⃣ При входе покажите QR-код — за вами закрепят столик и отметят "
       "визит в системе. Скидка считается от той же цены, что действует "
       "для всех остальных гостей — отдельного завышенного прайса нет.\n\n"
       f"Отмена более чем за {CANCEL_FREE_HOURS} часа — свободная.\n"
       f"За каждый визит начисляем баллы: {DEPOSIT} баллов = бесплатная бронь.")


def qr_png(text: str) -> BufferedInputFile:
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return BufferedInputFile(buf.getvalue(), filename="booking.png")


@router.message(CommandStart(deep_link=True))
async def start_deeplink(m: Message, command: CommandObject):
    async with Session() as s:
        u, created = await get_or_create_user(s, m.from_user.id,
                                              m.from_user.first_name or "Гость")
        arg = command.args or ""
        if created and arg.startswith("ref_"):
            try:
                ref_id = int(arg[4:])
            except ValueError:
                ref_id = 0
            if ref_id and ref_id != u.id:
                ref = await s.get(type(u), ref_id)
                if ref:
                    u.ref_by = ref_id
                    await add_points(s, u, PTS_REF, "Бонус за приглашение")
                    await add_points(s, ref, PTS_REF, "Приглашён друг")
        await s.commit()
    await m.answer(WELCOME, reply_markup=kb_main())


@router.message(CommandStart())
async def start(m: Message):
    async with Session() as s:
        await get_or_create_user(s, m.from_user.id,
                                 m.from_user.first_name or "Гость")
        await s.commit()
    await m.answer(WELCOME, reply_markup=kb_main())


@router.callback_query(MenuCB.filter(F.to == "home"))
async def home(c: CallbackQuery):
    await c.message.edit_text(WELCOME, reply_markup=kb_main())
    await c.answer()


@router.callback_query(MenuCB.filter(F.to == "how"))
async def how(c: CallbackQuery):
    await c.message.edit_text(HOW, reply_markup=kb_back())
    await c.answer()


# ---------- лента: день × район × категория ----------
async def _feed(dist: str, cat: str, day: int):
    """Заведения, у которых в выбранный день есть свободные часы."""
    target_date = date_for(day)
    async with Session() as s:
        q = select(Venue).where(Venue.active.is_(True))
        if dist != "all":
            q = q.where(Venue.district == dist)
        if cat != "all":
            q = q.where(Venue.cat == cat)
        venues = (await s.scalars(q)).all()
        out = []
        for v in venues:
            if target_date.weekday() not in venue_weekdays(v):
                continue
            q_slots = select(Slot).where(Slot.venue_id == v.id)
            # сегодня прошедшие часы не предлагаем — как на сайте
            if day == 0:
                q_slots = q_slots.where(Slot.hour > dt.datetime.now().hour)
            slots = (await s.scalars(q_slots.order_by(Slot.discount.desc()))).all()
            if slots:
                out.append((v, slots[0].discount))
        return out


def _feed_title(dist: str, cat: str, day: int, count: int) -> str:
    where = "" if dist == "all" else f" · {DIST_NAME.get(dist, dist)}"
    what = "" if cat == "all" else f" · {CAT_NAME.get(cat, cat)}"
    when = day_label(day)
    if not count:
        return (f"🕑 <b>Тихие окна · {when}{where}{what}</b>\n\n"
                f"На этот день пока пусто — попробуйте другой день, район или категорию.")
    return f"🕑 <b>Тихие окна · {when}{where}{what}</b>\nНайдено мест: {count}"


@router.callback_query(MenuCB.filter(F.to == "feed"))
async def feed_home(c: CallbackQuery):
    venues = await _feed("all", "all", 0)
    await c.message.edit_text(
        _feed_title("all", "all", 0, len(venues)),
        reply_markup=kb_feed(venues, "all", "all", 0))
    await c.answer()


@router.callback_query(DayCB.filter())
async def feed_day(c: CallbackQuery, callback_data: DayCB):
    d, dist, cat = callback_data.day, callback_data.dist, callback_data.cat
    venues = await _feed(dist, cat, d)
    await c.message.edit_text(_feed_title(dist, cat, d, len(venues)),
                              reply_markup=kb_feed(venues, dist, cat, d))
    await c.answer()


@router.callback_query(DistCB.filter())
async def feed_dist(c: CallbackQuery, callback_data: DistCB):
    dist, cat, d = callback_data.dist, callback_data.cat, callback_data.day
    venues = await _feed(dist, cat, d)
    await c.message.edit_text(_feed_title(dist, cat, d, len(venues)),
                              reply_markup=kb_feed(venues, dist, cat, d))
    await c.answer()


@router.callback_query(CatCB.filter())
async def feed_cat(c: CallbackQuery, callback_data: CatCB):
    cat, dist, d = callback_data.cat, callback_data.dist, callback_data.day
    venues = await _feed(dist, cat, d)
    await c.message.edit_text(_feed_title(dist, cat, d, len(venues)),
                              reply_markup=kb_feed(venues, dist, cat, d))
    await c.answer()


# ---------- карточка заведения ----------
async def _venue_days(v: Venue) -> list[int]:
    """Ближайшие дни (смещения от сегодня), когда у заведения тихий час."""
    wd = venue_weekdays(v)
    return [off for off in range(DAY_BUTTONS) if date_for(off).weekday() in wd]


async def _venue_slots(v: Venue, day: int):
    async with Session() as s:
        q = select(Slot).where(Slot.venue_id == v.id)
        if day == 0:
            q = q.where(Slot.hour > dt.datetime.now().hour)
        return (await s.scalars(q.order_by(Slot.hour))).all()


def _venue_text(v: Venue, day: int, has_slots: bool) -> str:
    when = day_label(day)
    base = (f"{CAT_ICON.get(v.cat,'•')} <b>{v.name}</b>\n"
            f"{v.place} · {DIST_NAME.get(v.district, v.district)}\n"
            f"{v.check_note} · <i>{v.left_note}</i>\n\n")
    if not has_slots:
        return base + f"На «{when}» у заведения нет тихих часов — выберите другой день."
    return (base + f"<b>Тихие окна · {when}</b> — скидка на весь счёт.\n"
                   f"Скидка считается от текущей цены, без завышения.\n"
                   f"Депозит {DEPOSIT} ₽ зачитывается в счёт "
                   f"(на пилоте бронь бесплатная).")


@router.callback_query(VenueCB.filter())
async def venue_card(c: CallbackQuery, callback_data: VenueCB):
    async with Session() as s:
        v = await s.get(Venue, callback_data.venue_id)
    day = callback_data.day
    v_days = await _venue_days(v)
    if day not in v_days and v_days:
        day = v_days[0]
    slots = await _venue_slots(v, day)
    await c.message.edit_text(
        _venue_text(v, day, bool(slots)),
        reply_markup=kb_venue(slots, v.id, callback_data.dist, callback_data.cat,
                              day, v_days))
    await c.answer()


@router.callback_query(VenueDayCB.filter())
async def venue_day(c: CallbackQuery, callback_data: VenueDayCB):
    async with Session() as s:
        v = await s.get(Venue, callback_data.venue_id)
    v_days = await _venue_days(v)
    slots = await _venue_slots(v, callback_data.day)
    await c.message.edit_text(
        _venue_text(v, callback_data.day, bool(slots)),
        reply_markup=kb_venue(slots, v.id, callback_data.dist, callback_data.cat,
                              callback_data.day, v_days))
    await c.answer()


@router.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()


# ---------- бронирование ----------
@router.callback_query(BookCB.filter())
async def book(c: CallbackQuery, callback_data: BookCB):
    async with Session() as s:
        slot = await s.get(Slot, callback_data.slot_id)
        v = await s.get(Venue, slot.venue_id)
        u, _ = await get_or_create_user(s, c.from_user.id,
                                        c.from_user.first_name or "Гость")
        code = f"ТЧ-{random.randint(1000, 9999)}"
        visit_date = date_for(callback_data.day)
        s.add(Booking(code=code, user_id=u.id, venue_id=v.id, slot_id=slot.id,
                      visit_date=visit_date))
        await s.commit()
    when = day_label(callback_data.day)
    date_str = visit_date.strftime("%d.%m")
    caption = (f"✅ <b>Место закреплено!</b>\n\n"
               f"{CAT_ICON.get(v.cat,'•')} <b>{v.name}</b> · {v.place}\n"
               f"🕑 {when} ({date_str}), {slot.hour}:00–{slot.hour+1}:00\n"
               f"🏷 Скидка −{slot.discount}% на весь счёт\n"
               f"Код брони: <b>{code}</b>\n\n"
               f"При входе покажите этот QR — за вами закрепят столик и "
               f"отметят визит в системе. Заказывайте и отдыхайте со скидкой, "
               f"платите как обычно, только меньше.\n\n"
               f"Отмена — в «Мои брони» (бесплатно за {CANCEL_FREE_HOURS}+ часа).")
    await c.message.answer_photo(qr_png(f"TIHIYCHAS|{code}"), caption=caption,
                                 reply_markup=kb_back())
    await c.answer("Место закреплено!")


# ---------- мои брони ----------
@router.callback_query(MenuCB.filter(F.to == "bookings"))
async def my_bookings(c: CallbackQuery):
    async with Session() as s:
        rows = (await s.execute(
            select(Booking, Venue, Slot)
            .join(Venue, Booking.venue_id == Venue.id)
            .join(Slot, Booking.slot_id == Slot.id)
            .where(Booking.user_id == c.from_user.id)
            .order_by(Booking.created_at.desc()).limit(8))).all()
    if not rows:
        await c.message.edit_text("Пока нет броней. Загляните в тихие окна 🕑",
                                  reply_markup=kb_back())
        return await c.answer()
    lines, active = ["🎟 <b>Мои брони</b>\n"], []
    st = {"active": "🟡 активна", "cancelled": "⚪️ отменена", "visited": "🟢 визит состоялся"}
    for b, v, sl in rows:
        date_str = b.visit_date.strftime("%d.%m") if b.visit_date else ""
        lines.append(f"<b>{b.code}</b> · {v.name} · {date_str} {sl.hour}:00 · "
                     f"−{sl.discount}% — {st[b.status]}")
        if b.status == "active":
            active.append(b)
    await c.message.edit_text("\n".join(lines), reply_markup=kb_bookings(active))
    await c.answer()


@router.callback_query(CancelCB.filter())
async def cancel_booking(c: CallbackQuery, callback_data: CancelCB):
    async with Session() as s:
        b = await s.get(Booking, callback_data.booking_id)
        if not b or b.user_id != c.from_user.id or b.status != "active":
            return await c.answer("Бронь не найдена", show_alert=True)
        sl = await s.get(Slot, b.slot_id)
        b.status = "cancelled"
        await s.commit()
    visit_dt = dt.datetime.combine(b.visit_date, dt.time(hour=sl.hour))
    hours_left = (visit_dt - dt.datetime.now()).total_seconds() / 3600
    note = ("Депозит вернулся бы автоматически."
            if hours_left >= CANCEL_FREE_HOURS else
            f"До визита меньше {CANCEL_FREE_HOURS} ч — на проде депозит был бы удержан 50/50.")
    await c.answer("Бронь отменена")
    await c.message.edit_text(f"Бронь <b>{b.code}</b> отменена. {note}",
                              reply_markup=kb_back())


# ---------- баллы ----------
@router.callback_query(MenuCB.filter(F.to == "points"))
async def points(c: CallbackQuery, bot_username: str):
    async with Session() as s:
        u, _ = await get_or_create_user(s, c.from_user.id,
                                        c.from_user.first_name or "Гость")
        await s.commit()
        left = max(0, DEPOSIT - u.points)
    goal = (f"ещё <b>{left}</b> до бесплатной брони"
            if left else "доступна <b>бесплатная бронь</b>!")
    link = f"https://t.me/{bot_username}?start=ref_{c.from_user.id}"
    await c.message.edit_text(
        f"✨ <b>Тихие баллы: {u.points}</b>\n{goal}\n\n"
        f"• +10 баллов за каждый визит, ×2 — за новое заведение\n"
        f"• {DEPOSIT} баллов = депозит в подарок\n"
        f"• Статус «Свой» за 6 визитов: {u.visits} / 6\n\n"
        f"Пригласите друга — по +{PTS_REF} баллов обоим:\n{link}",
        reply_markup=kb_back(), disable_web_page_preview=True)
    await c.answer()
