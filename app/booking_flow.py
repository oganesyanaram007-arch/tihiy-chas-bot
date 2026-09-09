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
import secrets
import time
from collections import defaultdict

from sqlalchemy import select, update

from .db import Booking, BookingEvent, Session, Slot, User, Venue, add_points
from .product import CODE_ALPHABET, CODE_LEGACY_PREFIX, CODE_LENGTH
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


# Буквы, которые в кириллице и латинице выглядят одинаково. Сотрудник
# набирает код на телефоне и раскладку переключить забывает — отказывать
# из-за этого нельзя, гость назвал код правильно.
_LAT_TO_CYR = {"A": "А", "B": "В", "E": "Е", "K": "К", "M": "М", "H": "Н",
               "O": "О", "P": "Р", "C": "С", "T": "Т", "X": "Х", "Y": "У"}
_CYR_TO_LAT = {v: k for k, v in _LAT_TO_CYR.items()}


def _clean(raw: str) -> str:
    """Убирает всё, чем код обрастает при наборе: пробелы, тире, регистр."""
    s = (raw or "").strip().upper()
    for junk in (" ", "\t", "\u00a0", "—", "–", "−"):
        s = s.replace(junk, "-" if junk in ("—", "–", "−") else "")
    return s


def code_candidates(raw: str) -> list[str]:
    """Варианты, которыми мог быть набран один и тот же код.

    Форматов два и оба живые: новый — шесть латинских знаков, старый —
    ТЧ- и четыре цифры с кириллическим префиксом. Одно правило замены их
    не покрывает: перевод латиницы в кириллицу чинит старый код, набранный
    не в той раскладке, и ломает новый. Поэтому пробуем оба направления,
    а решает уже поиск в базе.
    """
    s = _clean(raw)
    if not s:
        return []
    out = [s]
    for mapping in (_CYR_TO_LAT, _LAT_TO_CYR):
        variant = "".join(mapping.get(ch, ch) for ch in s)
        if variant not in out:
            out.append(variant)
    # Гость иногда диктует код без префикса, а сотрудник так и вводит.
    if CODE_LEGACY_PREFIX and s.isdigit():
        for pref in (CODE_LEGACY_PREFIX, "".join(_CYR_TO_LAT.get(c, c)
                                                 for c in CODE_LEGACY_PREFIX)):
            if pref + s not in out:
                out.append(pref + s)
    return out


def normalize_code(raw: str) -> str:
    """Канонический вид кода — первый из вариантов. Нужен для показа в ответах."""
    got = code_candidates(raw)
    return got[0] if got else ""


async def new_code(s) -> str:
    """Свежий код брони.

    secrets, а не random: коды не должны идти подряд и не должны угадываться
    по своему. Алфавит без похожих знаков (0/O, 1/I/L, U) лежит в
    content/product.json — гость называет код вслух, а сотрудник набирает
    его на телефоне, и пара «ноль или буква О» стоит отказа на входе.
    """
    for _ in range(12):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        taken = await s.scalar(select(Booking.id).where(Booking.code == code))
        if not taken:
            return code
    # 30^6 вариантов: сюда можно попасть только при сломанном генераторе.
    raise RuntimeError("не удалось выдать уникальный код брони")


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
    """Кто и когда отметил визит: «отметили 09.09 в 15:12, Марина».

    Читаем журнал переходов — он помнит и время, и имя того, кто нажал.
    Если брони погашены до появления журнала, берём то, что записано
    в самой броне; там имени нет, но время есть.
    """
    ev = await s.scalar(
        select(BookingEvent).where(BookingEvent.booking_id == booking_id,
                                   BookingEvent.action == "redeem")
        .order_by(BookingEvent.id.desc()).limit(1))
    if ev:
        return fmt_dt(ev.created_at), ev.actor_name or ""

    bk = await s.get(Booking, booking_id)
    if bk and bk.redeemed_at:
        who = ""
        if bk.redeemed_by:
            pu = await s.get(PartnerUser, bk.redeemed_by)
            who = pu.name if pu and pu.name else ""
        return fmt_dt(bk.redeemed_at), who
    return "", ""


async def redeem(code: str, *, partner_id: int | None = None,
                 partner_user_id: int | None = None,
                 booking_id: int | None = None,
                 enforce_window: bool = True,
                 admin: bool = False,
                 ip: str = "", device: str = "") -> Outcome:
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

    wanted = code_candidates(code) if code else []

    async with Session() as s:
        # ---- находим бронь ----
        if booking_id is not None:
            bk = await s.get(Booking, booking_id)
        elif wanted:
            bk = await s.scalar(select(Booking).where(Booking.code.in_(wanted)))
        else:
            bk = None

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
            .values(status="visited", redeemed_at=utc_now(),
                    redeemed_by=partner_user_id))

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

        actor_name = ""
        if partner_user_id:
            pu = await s.get(PartnerUser, partner_user_id)
            actor_name = (pu.name if pu else "") or ""
        s.add(BookingEvent(
            booking_id=bk_id, action="redeem",
            status_from="active", status_to="visited",
            actor_kind="admin" if admin else "staff",
            actor_id=partner_user_id, actor_name=actor_name,
            ip=ip[:64], device=device[:200], note=bk_code,
            created_at=utc_now()))
        # audit_log остаётся: он про действия партнёра в кабинете, и на него
        # смотрят существующие экраны. Журнал брони живёт рядом, не вместо.
        s.add(AuditLog(partner_user_id=partner_user_id, partner_id=partner_id,
                       action="visit_confirm", entity="booking", entity_id=bk_id,
                       payload=bk_code))
        await s.commit()

        info["status"] = "visited"
        return Outcome(ok=True, booking=info,
                       message=f"Визит отмечен: {info['venue_name']}, {info['when']}.")


async def cancel(booking_id: int, *, guest_id: int | None = None,
                 actor_kind: str = "guest", actor_name: str = "",
                 ip: str = "", device: str = "") -> Outcome:
    """Отменить бронь. Вторая точка смены статуса, устроена так же.

    guest_id ограничивает отмену владельцем брони: чужую отменить нельзя.
    Переход — одним UPDATE с условием, ноль строк означает, что бронь уже
    закрыта: либо отменена раньше, либо гость успел прийти.
    """
    async with Session() as s:
        bk = await s.get(Booking, booking_id)
        if not bk or (guest_id is not None and bk.user_id != guest_id):
            return _fail("not_found", "Бронь не найдена.", http=404)

        venue = await s.get(Venue, bk.venue_id)
        slot = await s.get(Slot, bk.slot_id)
        info = _booking_json(bk, venue, slot)
        bk_id, bk_code, bk_status = bk.id, bk.code, bk.status

        if bk_status == "visited":
            return _fail("already_visited",
                         "Визит уже отмечен — отменить бронь нельзя.", booking=info)
        if bk_status == "cancelled":
            return _fail("already", "Бронь уже отменена.", booking=info)

        res = await s.execute(
            update(Booking)
            .where(Booking.id == bk_id, Booking.status == "active")
            .values(status="cancelled", cancelled_at=utc_now()))
        if res.rowcount == 0:
            await s.rollback()
            return _fail("conflict", "Бронь только что изменилась — обновите экран.",
                         booking=info)

        s.add(BookingEvent(
            booking_id=bk_id, action="cancel",
            status_from="active", status_to="cancelled",
            actor_kind=actor_kind, actor_id=guest_id, actor_name=actor_name[:128],
            ip=ip[:64], device=device[:200], note=bk_code, created_at=utc_now()))
        await s.commit()

    info["status"] = "cancelled"
    return Outcome(ok=True, booking=info, message="Бронь отменена.")
