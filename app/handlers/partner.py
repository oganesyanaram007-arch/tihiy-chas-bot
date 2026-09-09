# -*- coding: utf-8 -*-
"""Анкета заведения (FSM) + админ-команды + модерация кабинета (этап 2).

Витрина остаётся доверенной: ничего не публикуется без ручного решения
администратора. Одобрение конвертирует черновик партнёра (VenueProfile +
QuietWindow) в публичные Venue + Slot — те же таблицы, что читают сайт,
бот и мини-апп."""
from __future__ import annotations

import datetime as dt

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup, Message
from sqlalchemy import delete, select

from ..config import ADMIN_IDS, PTS_NEW_MULT, PTS_VISIT
from ..product import CODE_LENGTH
from .. import booking_flow
from ..db import (Booking, PartnerLead, Session, Slot, User, Venue, add_points,
                  get_or_create_user, user_visited_venue)
from ..keyboards import DIST_NAME, MenuCB, kb_back
from ..models_partner import AuditLog, Partner, PartnerUser, QuietWindow, VenuePhoto, VenueProfile

router = Router()


class PartnerForm(StatesGroup):
    name = State()
    phone = State()


@router.callback_query(MenuCB.filter(F.to == "partner"))
async def partner_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(PartnerForm.name)
    await c.message.edit_text(
        "🏪 <b>Подключение заведения</b>\n\n"
        "Заполните пилотный оффер: месяц бесплатно, дальше подписка от 2 990 ₽ "
        "и фикс 99 ₽ за пришедшего гостя — единая ставка (первые два визита; "
        "постоянные — бесплатно).\n\n<b>Как называется заведение?</b>")
    await c.answer()


@router.message(PartnerForm.name)
async def partner_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip()[:120])
    await state.set_state(PartnerForm.phone)
    await m.answer("Телефон для связи? (например, +7 921 000-00-00)")


@router.message(PartnerForm.phone)
async def partner_phone(m: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    async with Session() as s:
        s.add(PartnerLead(tg_user=m.from_user.id, name=data["name"],
                          phone=m.text.strip()[:32]))
        await s.commit()
    await m.answer("Спасибо! Заявка принята — перезвоним в рабочий день "
                   "и подключим бесплатный пилот. 🤝", reply_markup=kb_back())
    for admin in ADMIN_IDS:
        try:
            await m.bot.send_message(
                admin, f"🏪 Новая заявка заведения:\n<b>{data['name']}</b> · "
                       f"{m.text.strip()} · от @{m.from_user.username or m.from_user.id}")
        except Exception:
            pass


# ---------------- админка ----------------
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


@router.message(Command("admin"))
async def admin_help(m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer("Админ-команды:\n"
                   "/leads — заявки заведений\n"
                   "/visit КОД — подтвердить визит по коду брони (начислит баллы)")


@router.message(Command("leads"))
async def leads(m: Message):
    if not is_admin(m.from_user.id):
        return
    async with Session() as s:
        rows = (await s.scalars(select(PartnerLead)
                                .order_by(PartnerLead.created_at.desc())
                                .limit(15))).all()
    if not rows:
        return await m.answer("Заявок пока нет.")
    await m.answer("\n".join(f"#{l.id} {l.name} · {l.phone} · {l.status}"
                             for l in rows))


@router.message(Command("visit"))
async def confirm_visit(m: Message):
    if not is_admin(m.from_user.id):
        return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.answer(f"Формат: /visit КОД (например, "
                              f"/visit {'X' * CODE_LENGTH})")
    # Сам переход живёт в booking_flow: своей копии проверок здесь быть
    # не должно, иначе кабинет и админка снова разойдутся в поведении.
    out = await booking_flow.redeem(parts[1], admin=True)
    if not out.ok:
        return await m.answer(f"❌ {out.message}")

    bk = out.booking
    await m.answer(f"✅ Визит {bk['code']} подтверждён: "
                   f"{bk['venue_name']}, {bk['when']}.")
    async with Session() as s:
        booking = await s.get(Booking, bk["id"])
        guest = await s.get(User, booking.user_id) if booking else None
    if not guest:
        return
    try:
        await m.bot.send_message(
            guest.id,
            f"🟢 Визит в <b>{bk['venue_name']}</b> подтверждён!\n"
            f"Скидка −{bk['discount']}% применена. Баланс: {guest.points} баллов.")
    except Exception:
        pass


# =====================================================================
# ЭТАП 2 — модерация заведений из кабинета
# =====================================================================

class ApproveCB(CallbackData, prefix="mod_ok"):
    venue_id: int


class RejectCB(CallbackData, prefix="mod_no"):
    venue_id: int


class RejectForm(StatesGroup):
    reason = State()


def kb_moderation(venue_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        B(text="✅ Одобрить", callback_data=ApproveCB(venue_id=venue_id).pack()),
        B(text="❌ Отклонить", callback_data=RejectCB(venue_id=venue_id).pack()),
    ]])


async def _venue_card_text(venue_id: int) -> str | None:
    async with Session() as s:
        v = await s.get(Venue, venue_id)
        if not v:
            return None
        p = await s.scalar(select(VenueProfile).where(VenueProfile.venue_id == venue_id))
        photos_n = len((await s.scalars(select(VenuePhoto.id).where(
            VenuePhoto.venue_id == venue_id))).all())
        wins = (await s.scalars(select(QuietWindow).where(
            QuietWindow.venue_id == venue_id, QuietWindow.active.is_(True)))).all()
    if not p:
        return None
    days = sorted({w.weekday for w in wins})
    hours = sorted({(w.hour, w.discount) for w in wins})
    days_str = ",".join("пвсчпсв"[d] for d in days) if days else "—"
    hours_str = ", ".join(f"{h}:00 −{d}%" for h, d in hours) if hours else "—"
    return (f"🏪 <b>{v.name}</b> ({v.cat})\n"
           f"Район: {DIST_NAME.get(p.district, p.district)}\n"
           f"Адрес: {p.address or '—'}\nТелефон: {p.phone or '—'}\n"
           f"Фото: {photos_n} · Статус: {p.status}\n"
           f"Дни: {days_str}\nЧасы: {hours_str}")


@router.message(F.text.regexp(r"^/venue_(\d+)$"))
async def venue_full_card(m: Message):
    if not is_admin(m.from_user.id):
        return
    venue_id = int(m.text.split("_", 1)[1])
    text = await _venue_card_text(venue_id)
    if not text:
        return await m.answer("Заведение не найдено.")
    await m.answer(text, reply_markup=kb_moderation(venue_id))


@router.message(Command("pending"))
async def pending(m: Message):
    if not is_admin(m.from_user.id):
        return
    async with Session() as s:
        rows = (await s.scalars(select(VenueProfile).where(
            VenueProfile.status == "moderation"))).all()
    if not rows:
        return await m.answer("Очередь модерации пуста ✅")
    lines = ["🕓 <b>На модерации:</b>"]
    for p in rows:
        async with Session() as s:
            v = await s.get(Venue, p.venue_id)
        lines.append(f"/venue_{p.venue_id} — {v.name} ({DIST_NAME.get(p.district, p.district)})")
    await m.answer("\n".join(lines))


@router.callback_query(ApproveCB.filter())
async def approve_venue(c: CallbackQuery, callback_data: ApproveCB):
    if not is_admin(c.from_user.id):
        return await c.answer("Только для администратора", show_alert=True)
    venue_id = callback_data.venue_id
    async with Session() as s:
        v = await s.get(Venue, venue_id)
        p = await s.scalar(select(VenueProfile).where(VenueProfile.venue_id == venue_id))
        if not v or not p:
            return await c.answer("Заведение не найдено", show_alert=True)
        if p.status == "active":
            return await c.answer("Уже опубликовано", show_alert=True)

        wins = (await s.scalars(select(QuietWindow).where(
            QuietWindow.venue_id == venue_id, QuietWindow.active.is_(True)))).all()
        if not wins:
            return await c.answer("Нет тихих часов — нечего публиковать", show_alert=True)

        # Собираем витрину из черновика: дни — объединение всех настроенных,
        # часы — берём первую заданную скидку на каждый час (в MVP слоты
        # одинаковы во все выбранные дни, конфликтов обычно не возникает).
        days = sorted({w.weekday for w in wins})
        hour_discount: dict[int, int] = {}
        for w in wins:
            hour_discount.setdefault(w.hour, w.discount)

        v.weekdays = ",".join(str(d) for d in days)
        v.active = True
        if not v.left_note:
            v.left_note = "уточняйте в заведении"
        await s.execute(delete(Slot).where(Slot.venue_id == venue_id))
        for hour, discount in sorted(hour_discount.items()):
            s.add(Slot(venue_id=venue_id, hour=hour, discount=discount))

        p.status = "active"
        partner = await s.get(Partner, p.partner_id)
        partner.status = "active"
        partner.approved_at = dt.datetime.utcnow()
        s.add(AuditLog(partner_id=p.partner_id, action="venue_approved",
                       entity="venue", entity_id=venue_id))
        await s.commit()

        owner = await s.scalar(select(PartnerUser).where(
            PartnerUser.partner_id == p.partner_id, PartnerUser.role == "owner"))

    await c.message.edit_text(c.message.text + "\n\n✅ <b>Опубликовано</b>", reply_markup=None)
    await c.answer("Заведение опубликовано")
    if owner:
        try:
            await c.bot.send_message(
                owner.tg_id,
                f"🎉 <b>{v.name}</b> опубликовано в «Тихом Часе»!\n"
                f"Заведение уже видно гостям в ленте. Управлять тихими часами — "
                f"в кабинете, в любой момент можно поправить.")
        except Exception:
            pass


@router.callback_query(RejectCB.filter())
async def reject_venue_start(c: CallbackQuery, callback_data: RejectCB, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("Только для администратора", show_alert=True)
    await state.set_state(RejectForm.reason)
    await state.update_data(venue_id=callback_data.venue_id)
    await c.message.reply("Напишите причину отклонения одним сообщением — "
                          "партнёр увидит её и сможет исправить.")
    await c.answer()


@router.message(RejectForm.reason)
async def reject_venue_finish(m: Message, state: FSMContext):
    data = await state.get_data()
    venue_id = data["venue_id"]
    reason = m.text.strip()[:300]
    await state.clear()

    async with Session() as s:
        v = await s.get(Venue, venue_id)
        p = await s.scalar(select(VenueProfile).where(VenueProfile.venue_id == venue_id))
        if not v or not p:
            return await m.answer("Заведение не найдено — возможно, уже обработано.")
        p.status = "draft"
        s.add(AuditLog(partner_id=p.partner_id, action="venue_rejected",
                       entity="venue", entity_id=venue_id, payload=reason))
        await s.commit()
        owner = await s.scalar(select(PartnerUser).where(
            PartnerUser.partner_id == p.partner_id, PartnerUser.role == "owner"))

    await m.answer(f"Отклонено: <b>{v.name}</b>. Партнёр получит причину и сможет "
                   f"исправить и отправить снова.")
    if owner:
        try:
            await m.bot.send_message(
                owner.tg_id,
                f"⚠️ Заявку по заведению <b>{v.name}</b> вернули на доработку.\n"
                f"Причина: {reason}\n\nПоправьте в кабинете и отправьте на "
                f"проверку ещё раз — это займёт пару минут.")
        except Exception:
            pass


# =====================================================================
# Вход в кабинет: бот выдаёт одноразовую ссылку
# =====================================================================
CAB_NO_ACCESS = ("Кабинет открыт заведениям, подключённым к «Тихому Часу».\n\n"
                 "Оставьте заявку кнопкой «Подключить заведение» или на "
                 "tihiy-chas.ru — откроем доступ в рабочий день.")


async def _cabinet_reply(tg_id: int, target: Message) -> None:
    async with Session() as s:
        user = await s.scalar(select(PartnerUser).where(PartnerUser.tg_id == tg_id))
        allowed = bool(user and user.active)
    if not allowed:
        return await target.answer(CAB_NO_ACCESS, reply_markup=kb_back())

    from ..cabinet import CABINET_URL, issue_login_code
    code = await issue_login_code(tg_id)
    await target.answer(
        "🔑 <b>Кабинет партнёра</b>\n\n"
        "Здесь добавляют заведения, настраивают тихие часы и следят за "
        "модерацией.\n\nСсылка одноразовая и живёт 15 минут — открывается "
        "и на телефоне, и на компьютере.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            B(text="Открыть кабинет", url=f"{CABINET_URL}?k={code}")]]))


@router.callback_query(MenuCB.filter(F.to == "cabinet"))
async def cabinet_from_menu(c: CallbackQuery):
    await _cabinet_reply(c.from_user.id, c.message)
    await c.answer()


@router.message(Command("cabinet"))
async def cabinet_command(m: Message):
    await _cabinet_reply(m.from_user.id, m)
