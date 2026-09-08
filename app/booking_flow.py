# -*- coding: utf-8 -*-
"""Переходы состояния брони — одно место на весь продукт.

Раньше отметка визита жила в двух местах: кнопка в кабинете
(`cabinet.py: confirm_visit`) и команда `/visit КОД` у администратора
(`handlers/partner.py`). Обе читали бронь, проверяли статус в Python
и потом писали — то есть между проверкой и записью оставалась щель.
Две одновременные отметки проходили обе, гость получал баллы дважды,
а спорить «мы гасили — нет, не гасили» было нечем.

Здесь переход делается одним UPDATE с условием на текущий статус.
Дальше смотрим число затронутых строк: ноль — это не ошибка сервера,
а осмысленный ответ «код уже погашен, вот когда и кем».

Веб, бот и админка зовут отсюда. Своей копии этой логики быть не должно.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import time
from collections import defaultdict

from sqlalchemy import select, update

from .db import Booking, Session, Slot, User, Venue, add_points
from .models_partner import AuditLog, PartnerUser, VenueProfile
from .tz import fmt_dt, fmt_slot, msk_now, slot_start_msk, utc_now

# Код действует в окне слота плюс-минус полчаса: гость может прийти
# чуть раньше и чуть задержаться, но код из вчерашней брони на входе
# сегодня не сработает.
GRACE = dt.timedelta(minutes=30)
SLOT_LENGTH = dt.timedelta(hours=1)

# Подбор кода перебором должен упираться в стену: не больше 10 неудачных
# попыток в час на аккаунт партнёра. Успешные погашения не считаем —
# в занятую смену их может быть много, и блокировать за работу нельзя.
ATTEMPT_LIMIT = 10
ATTEMPT_WINDOW = 3600
_attempts: dict[int, list[float]] = defaultdict(list)


@dataclasses.dataclass
class Outcome:
    """Что случилось с попыткой погашения.

    ok — статус сменился именно этим вызовом. Всё остальное — отказ
    с причиной, которую можно показать сотруднику словами.
    """
    ok: bool
    reason: str = ""           # машинный код: not_found, already, expired, …
    message: str = ""          # текст для человека
    booking: dict | None = None
    http: int = 200


def _fail(reason: str, message: str, http: int = 409, booking=None) -> Outcome:
    return Outcome(ok=False, reason=reason, message=message, http=http, booking=booking)


def normalize_code(raw: str) -> str:
    """Приводит введённый код к каноническому виду.

    Сотрудник вводит код с телефона в спешке: с пробелами, в нижнем
    регистре, иногда с латинской раскладки вместо кириллицы (или наоборот).
    Отказывать из-за раскладки нельзя — гость назвал код правильно.
    """
    s = (raw or "").strip().upper().replace(" ", "").replace("—", "-").replace("–", "-")
    # Кириллица ↔ латиница для букв, которые выглядят одинаково.
    same = {"A": "А", "B": "В", "E": "Е", "K": "К", "M": "М", "H": "Н",
            "O": "О", "P": "Р", "C": "С", "T": "Т", "X": "Х", "Y": "У"}
    return "".join(same.get(ch, ch) for ch in s)


def attempts_left(partner_user_id: int) -> int:
    now = time.time()
    hits = [t for t in _attempts[partner_user_id] if now - t < ATTEMPT_WINDOW]
    _attempts[partner_user_id] = hits
    return max(0, ATTEMPT_LIMIT - len(hits))


def _register_miss(partner_user_id: int) -> None:
    _attempts[partner_user_id].append(time.time())


def reset_attempts(partner_user_id: int) -> None:
    """Нужно тестам и ручному разбору: снять счётчик неудач."""
    _attempts.pop(partner_user_id, None)


def _booking_json(bk: Booking, venue: Venue | None, slot: Slot | None) -> dict:
    return {
        "id": bk.id,
        "code": bk.code,
        "status": bk.status,
        "date": bk.visit_date.isoformat(),
        "hour": slot.hour if slot else None,
        "discount": slot.discount if slot else None,
        "when": fmt_slot(bk.visit_date, slot.hour) if slot else "",
        "venue_id": bk.venue_id,
        "venue_name": venue.name if venue else "",
    }


async def _who_redeemed(s, booking_id: int) -> tuple[str, str]:
    """Кто и когда отметил визит — из журнала действий кабинета.

    В самой брони этого не записать: колонок redeemed_at/redeemed_by в
    таблице нет, а менять схему на живой базе без миграций нельзя.
    Журнал уже пишется на каждое погашение, и его достаточно, чтобы
    ответить сотруднику «отметили в 15:12, Марина» вместо «ошибка».
    """
    row = await s.scalar(
        select(AuditLog).where(AuditLog.action == "visit_confirm",
                               AuditLog.entity == "booking",
                               AuditLog.entity_id == booking_id)
        .order_by(AuditLog.id.desc()).limit(1))
    if not row:
        return "", ""
    who = ""
    if row.partner_user_id:
        pu = await s.get(PartnerUser, row.partner_user_id)
        who = pu.name if pu and pu.name else ""
    return fmt_dt(row.created_at), who


async def redeem(code: str, *, partner_id: int | None = None,
                 partner_user_id: int | None = None,
                 booking_id: int | None = None,
                 enforce_window: bool = True,
                 admin: bool = False) -> Outcome:
    """Погасить код брони. Единственная точка смены статуса на «visited».

    partner_id ограничивает поиск заведениями этого партнёра: код чужой
    площадки не должен гаситься даже случайно.
    booking_id — вход для кнопки в списке броней, где код и так известен.
    admin=True снимает ограничение по площадке (команда /visit в боте).
    """
    if partner_user_id is not None and not admin:
        if attempts_left(partner_user_id) <= 0:
            return _fail("locked",
                         "Слишком много неверных кодов. Ввод заблокирован на час — "
                         "отметьте гостя в списке броней или позовите управляющего.",
                         http=429)

    wanted = normalize_code(code) if code else ""

    async with Session() as s:
        # ---- находим бронь ----
        if booking_id is not None:
            bk = await s.get(Booking, booking_id)
        else:
            bk = await s.scalar(select(Booking).where(Booking.code == wanted))

        venue_ids: list[int] = []
        if partner_id is not None:
            venue_ids = list(await s.scalars(
                select(VenueProfile.venue_id).where(VenueProfile.partner_id == partner_id)))

        if not bk or (partner_id is not None and bk.venue_id not in venue_ids):
            # Чужая площадка отвечает так же, как несуществующий код:
            # иначе по разнице ответов можно перебирать чужие брони.
            if partner_user_id is not None and not admin:
                _register_miss(partner_user_id)
                left = attempts_left(partner_user_id)
                tail = (f" Осталось попыток: {left}." if left <= 3 else "")
            else:
                tail = ""
            return _fail("not_found",
                         "Код не найден. Проверьте, что гость назвал его целиком." + tail,
                         http=404)

        venue = await s.get(Venue, bk.venue_id)
        slot = await s.get(Slot, bk.slot_id)
        info = _booking_json(bk, venue, slot)

        # Дальше работаем простыми значениями. UPDATE и rollback помечают
        # атрибуты ORM-объекта протухшими, и любое обращение к bk.id после
        # них уходит в ленивую загрузку — а она в асинхронной сессии падает
        # (MissingGreenlet). Ловится только под одновременными запросами,
        # то есть ровно там, где этот код и должен работать.
        bk_id, bk_code = bk.id, bk.code
        bk_status, bk_user, bk_date = bk.status, bk.user_id, bk.visit_date

        # ---- состояние брони ----
        if bk_status == "cancelled":
            return _fail("cancelled", "Бронь отменена — гостя по ней впустить нельзя.",
                         booking=info)
        if bk_status == "visited":
            when, who = await _who_redeemed(s, bk_id)
            msg = "Код уже погашен"
            if when:
                msg += f" {when}"
            if who:
                msg += f", отметил(а) {who}"
            return _fail("already", msg + ".", booking=info)

        # ---- окно действия кода ----
        if enforce_window and slot:
            start = slot_start_msk(bk_date, slot.hour)
            now = msk_now()
            if now < start - GRACE:
                return _fail("too_early",
                             f"Рано: бронь на {fmt_slot(bk_date, slot.hour)}. "
                             f"Код заработает за полчаса до начала окна.",
                             booking=info)
            if now > start + SLOT_LENGTH + GRACE:
                return _fail("expired",
                             f"Код просрочен: бронь была на "
                             f"{fmt_slot(bk_date, slot.hour)}.",
                             booking=info)

        # ---- сам переход: одним UPDATE с условием на текущий статус ----
        res = await s.execute(
            update(Booking)
            .where(Booking.id == bk_id, Booking.status == "active")
            .values(status="visited"))

        if res.rowcount == 0:
            # Между чтением и записью бронь успели тронуть из другой сессии.
            await s.rollback()
            async with Session() as s2:
                fresh = await s2.get(Booking, bk_id)
                if fresh and fresh.status == "visited":
                    when, who = await _who_redeemed(s2, bk_id)
                    msg = "Код уже погашен"
                    if when:
                        msg += f" {when}"
                    if who:
                        msg += f", отметил(а) {who}"
                    return _fail("already", msg + ".", booking=info)
            return _fail("conflict", "Бронь только что изменилась — обновите список.",
                         booking=info)

        # ---- побочные эффекты только после успешного перехода ----
        guest = await s.get(User, bk_user)
        if guest:
            guest.visits += 1
            await add_points(s, guest, 30, f"Визит {bk_code}")

        s.add(AuditLog(partner_user_id=partner_user_id, partner_id=partner_id,
                       action="visit_confirm", entity="booking", entity_id=bk_id,
                       payload=bk_code))
        await s.commit()

        info["status"] = "visited"
        return Outcome(ok=True, booking=info,
                       message=f"Визит отмечен: {info['venue_name']}, {info['when']}.")
