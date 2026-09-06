# -*- coding: utf-8 -*-
"""API гостевого мини-аппа «Тихий Час».

До этого модуля гостевая часть жила только в боте: витрину и брони
отдавали callback-хендлеры (app/handlers/guest.py). Мини-апп рисовал
захардкоженный массив заведений и никуда не писал — гость видел
«бронь оформлена», а заведение об этом не узнавало.

Здесь тот же самый функционал вынесен в HTTP, поверх тех же таблиц
(Venue / Slot / Booking / User), чтобы бот и мини-апп были одним
продуктом с одной базой, а не двумя разными.

Решения, согласованные с основателем:
  · депозит 99 ₽ везде, единая ставка
  · бронь на 14 дней вперёд, у каждого заведения свой график дней недели
  · вход только через Telegram, без паролей и SMS
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import io
import random
from asyncio import Lock
from collections import OrderedDict, defaultdict
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import APIRouter, Header, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .auth import AuthError, verify_init_data
from .config import ADMIN_IDS, BOT_TOKEN
from .db import (Booking, PointsLedger, Session, Slot, User, Venue, WebSession,
                 add_points, get_or_create_user, works_on)
from .models_partner import PartnerUser, VenueProfile, QuietWindow, VenuePhoto
from .webauth import _cookie as _web_token_var, _digest as _web_digest

router = APIRouter(prefix="/api/guest", tags=["guest"])

MSK = ZoneInfo("Europe/Moscow")


def msk_now() -> dt.datetime:
    return dt.datetime.now(MSK)


def msk_today() -> dt.date:
    return msk_now().date()


DEPOSIT = 99            # единая ставка, решение зафиксировано
HORIZON_DAYS = 14       # горизонт бронирования
DEFAULT_CAPACITY = 4    # если партнёр не задал лимит мест в тихом окне
POINTS_PER_BOOKING = 20 # начисление за визит происходит в боте, здесь — за бронь

DIST_NAME = {
    "petro": "Петроградский", "centr": "Центральный", "adm": "Адмиралтейский",
    "vasil": "Василеостровский", "mosk": "Московский", "nev": "Невский",
    "primor": "Приморский", "vyb": "Выборгский", "kalin": "Калининский",
    "kirov": "Кировский", "frunz": "Фрунзенский", "krasnog": "Красногвардейский",
    "krasnos": "Красносельский", "push": "Пушкинский",
}

# Единственный uvicorn-воркер, поэтому внутрипроцессной блокировки достаточно,
# чтобы два гостя не забрали последний стол одновременно. При переезде на
# несколько воркеров это место меняется на блокировку в БД.
_slot_locks: dict[tuple[int, int, str], Lock] = defaultdict(Lock)


async def current_guest(x_init_data: str = Header(default="", alias="X-Init-Data")):
    """Гость из Telegram initData или из веб-сессии (кука tc_web).

    Мини-апп внутри Telegram остаётся рабочим как есть; обычный сайт
    входит через ту же сессию, что уже обслуживает кабинет партнёра."""
    if x_init_data:
        try:
            tg = verify_init_data(x_init_data)
        except AuthError as e:
            raise HTTPException(status_code=401, detail=str(e))
        name = " ".join(x for x in (tg.get("first_name", ""), tg.get("last_name", "")) if x)
        async with Session() as s:
            user, _ = await get_or_create_user(s, tg["id"], name or "Гость")
            await s.commit()
            return user

    token = _web_token_var.get()
    if token:
        now = dt.datetime.utcnow()
        async with Session() as s:
            sess = await s.scalar(select(WebSession).where(
                WebSession.token_hash == _web_digest(token)))
            if sess and sess.expires_at >= now:
                user = await s.get(User, sess.user_id)
                if user:
                    return user

    raise HTTPException(status_code=401, detail="Нужен вход")


def _venue_json(v: Venue, slots: list[Slot], photo_ver: int | None = None,
                photo_count: int = 0) -> dict:
    """Формат совпадает с тем, что мини-апп раньше держал в коде,
    чтобы фронт не пришлось переписывать целиком."""
    out = {
        "id": v.id,
        "cat": v.cat,
        "d": v.district,
        "name": v.name,
        "rate": v.check_note or "",
        "place": v.place,
        "left": v.left_note or "",
        # В базе дни по-питоновски (0 = понедельник), в JS getDay() 0 = воскресенье.
        # Конвертируем здесь, чтобы фронт не занимался арифметикой дат.
        "wd": sorted({(int(x) + 1) % 7 for x in v.weekdays.split(",") if x.strip() != ""}),
        "slots": [[sl.hour, sl.discount] for sl in sorted(slots, key=lambda x: x.hour)],
    }
    # Ссылка, а не сам снимок: фото лежит в базе как data-URL на сотни
    # килобайт, и вшивать его в список заведений — раздуть витрину в разы.
    if photo_ver is not None:
        # v= — метка версии: при замене снимка адрес меняется, и ни кэш
        # браузера, ни наш собственный не отдадут прежнюю картинку.
        out["photo"] = f"/api/guest/venue-photo/{v.id}?w=500&v={photo_ver}"
        # Сколько всего снимков: гость должен видеть заведение целиком,
        # а не одну обложку. По этому числу фронт рисует листалку.
        out["photos"] = max(1, photo_count)
    return out


# Готовые обложки держим в памяти: снимок в базе меняется редко, а
# разжимать и уменьшать мегабайт на каждый заход гостя незачем.
# Предел считаем в байтах, а не в записях: на сервере 2 ГБ памяти и нет
# swap, а один оригинал весит под мегабайт — по счётчику записей кэш мог
# бы съесть сотни мегабайт и уронить процесс.
_PHOTO_CACHE: "OrderedDict[tuple[int, int, int, int], tuple[bytes, str]]" = OrderedDict()
_PHOTO_CACHE_BYTES = 0
_PHOTO_CACHE_LIMIT = 48 * 1024 * 1024      # 48 МБ на все обложки
_PHOTO_ITEM_LIMIT = 4 * 1024 * 1024        # штуку крупнее в кэш не берём


def invalidate_photo_cache(venue_id: int) -> None:
    """Кабинет зовёт это, когда снимок добавили или удалили."""
    global _PHOTO_CACHE_BYTES
    for k in [k for k in _PHOTO_CACHE if k[0] == venue_id]:
        item = _PHOTO_CACHE.pop(k, None)
        if item:
            _PHOTO_CACHE_BYTES -= len(item[0])


def _cache_put(key, data: bytes, media: str) -> None:
    """Положить обложку в кэш, вытесняя самые давние, пока не влезет."""
    global _PHOTO_CACHE_BYTES
    if len(data) > _PHOTO_ITEM_LIMIT:
        return
    old = _PHOTO_CACHE.pop(key, None)
    if old:
        _PHOTO_CACHE_BYTES -= len(old[0])
    _PHOTO_CACHE[key] = (data, media)
    _PHOTO_CACHE_BYTES += len(data)
    while _PHOTO_CACHE_BYTES > _PHOTO_CACHE_LIMIT and _PHOTO_CACHE:
        _, victim = _PHOTO_CACHE.popitem(last=False)
        _PHOTO_CACHE_BYTES -= len(victim[0])


def _shrink(raw: bytes, width: int):
    """Уменьшить снимок до ширины width. None — если Pillow недоступен
    или картинку не удалось прочитать: тогда отдаём оригинал как есть."""
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        if im.width > width:
            h = max(1, round(im.height * width / im.width))
            im = im.resize((width, h), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=74, optimize=True, progressive=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return None


@router.get("/venue-photo/{venue_id}")
async def venue_photo(venue_id: int, w: int = Query(default=0, ge=0, le=2000),
                      v: int = Query(default=0), i: int = Query(default=0, ge=0, le=20)):
    """Обложка заведения для витрины.

    Отдельным запросом, чтобы браузер кэшировал картинку, а лента
    оставалась лёгкой. Партнёр грузит снимок из кабинета — сюда он
    попадает тем же путём, каким его сохранил cabinet.py.

    ?w=500 отдаёт уменьшенную копию: в карточке ленты снимок занимает
    около 360 px, и гонять ради неё исходный мегабайт по медленному
    каналу нельзя — уменьшенная весит примерно в одиннадцать раз меньше.
    Без параметра отдаётся оригинал.
    """
    key = (venue_id, w, v, i)
    hit = _PHOTO_CACHE.get(key)
    if hit:
        return Response(content=hit[0], media_type=hit[1],
                        headers={"Cache-Control": "public, max-age=86400"})
    async with Session() as s:
        url = await s.scalar(
            select(VenuePhoto.url)
            .where(VenuePhoto.venue_id == venue_id)
            .order_by(VenuePhoto.sort_order, VenuePhoto.id)
            .offset(i).limit(1)
        )
        if not url:
            # Запасной путь: у старых карточек обложка могла осесть только здесь.
            url = await s.scalar(
                select(VenueProfile.cover_url).where(VenueProfile.venue_id == venue_id)
            )
    if not url:
        raise HTTPException(status_code=404, detail="Нет фото")
    if not url.startswith("data:"):
        return RedirectResponse(url)
    try:
        head, b64 = url.split(",", 1)
        media = head[5:].split(";", 1)[0] or "image/jpeg"
        raw = base64.b64decode(b64)
    except Exception:
        raise HTTPException(status_code=404, detail="Фото повреждено")
    if w:
        small = _shrink(raw, w)
        if small:
            raw, media = small
    _cache_put(key, raw, media)
    return Response(content=raw, media_type=media,
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/venues")
async def venues():
    """Витрина: только активные заведения, прошедшие модерацию."""
    async with Session() as s:
        rows = (await s.scalars(select(Venue).where(Venue.active.is_(True)))).all()
        ids = [v.id for v in rows]
        slots = (await s.scalars(select(Slot).where(Slot.venue_id.in_(ids)))).all() if ids else []
        cover: dict[int, int] = {}
        photo_n: dict[int, int] = {}
        if ids:
            first_photo: dict[int, int] = {}
            for vid, pid in (await s.execute(
                select(VenuePhoto.venue_id, VenuePhoto.id)
                .where(VenuePhoto.venue_id.in_(ids))
                .order_by(VenuePhoto.sort_order, VenuePhoto.id)
            )).all():
                first_photo.setdefault(vid, pid)
                photo_n[vid] = photo_n.get(vid, 0) + 1
            prof = {vid: (upd, cov) for vid, upd, cov in (await s.execute(
                select(VenueProfile.venue_id, VenueProfile.updated_at,
                       VenueProfile.cover_url).where(VenueProfile.venue_id.in_(ids))
            )).all()}
            for vid in ids:
                upd, cov = prof.get(vid, (None, ""))
                if vid not in first_photo and not cov:
                    continue
                # Версия — момент последней правки снимков, а не id строки:
                # SQLite переиспользует id после удаления, и при замене фото
                # адрес остался бы прежним, а браузер сутки отдавал бы старое.
                cover[vid] = int(upd.timestamp()) if upd else first_photo.get(vid, 0)
    by_venue: dict[int, list[Slot]] = defaultdict(list)
    for sl in slots:
        by_venue[sl.venue_id].append(sl)
    out = [_venue_json(v, by_venue.get(v.id, []), cover.get(v.id), photo_n.get(v.id, 0))
           for v in rows if by_venue.get(v.id)]
    return {"deposit": DEPOSIT, "horizon": HORIZON_DAYS, "venues": out}


async def _capacity_map(s, venue_ids: list[int], weekday: int) -> dict[tuple[int, int], int]:
    """Лимит мест в тихом окне, если партнёр его задал в кабинете."""
    if not venue_ids:
        return {}
    rows = (await s.scalars(
        select(QuietWindow).where(
            QuietWindow.venue_id.in_(venue_ids),
            QuietWindow.weekday == weekday,
            QuietWindow.active.is_(True),
        )
    )).all()
    return {(q.venue_id, q.hour): q.capacity for q in rows}


@router.get("/availability")
async def availability(date: str = Query(..., description="YYYY-MM-DD")):
    """Сколько мест осталось в каждом окне на конкретную дату.

    Мини-апп прячет слоты, где мест не осталось, — иначе гость платит
    депозит за стол, которого нет.
    """
    try:
        day = dt.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректная дата")
    today = msk_today()
    if day < today or day > today + dt.timedelta(days=HORIZON_DAYS):
        raise HTTPException(status_code=400, detail="Дата вне горизонта бронирования")

    async with Session() as s:
        venue_rows = (await s.scalars(select(Venue).where(Venue.active.is_(True)))).all()
        open_ids = [v.id for v in venue_rows if works_on(v, day)]
        slots = (await s.scalars(select(Slot).where(Slot.venue_id.in_(open_ids)))).all() if open_ids else []
        taken_rows = (await s.execute(
            select(Booking.venue_id, Booking.slot_id, func.count(Booking.id))
            .where(Booking.visit_date == day, Booking.status == "active")
            .group_by(Booking.venue_id, Booking.slot_id)
        )).all() if open_ids else []
        caps = await _capacity_map(s, open_ids, day.weekday())

    taken = {(v, sl): n for v, sl, n in taken_rows}
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for sl in slots:
        cap = caps.get((sl.venue_id, sl.hour), DEFAULT_CAPACITY)
        left = max(0, cap - taken.get((sl.venue_id, sl.id), 0))
        out[str(sl.venue_id)][str(sl.hour)] = left
    return {"date": date, "left": out}


class BookIn(BaseModel):
    venue_id: int
    hour: int = Field(ge=0, le=23)
    date: str
    pay_with_points: bool = False


def _new_code() -> str:
    return "ТЧ-" + str(random.randint(1000, 9999))


@router.post("/bookings")
async def create_booking(body: BookIn,
                         x_init_data: str = Header(default="", alias="X-Init-Data")):
    guest = await current_guest(x_init_data)
    try:
        day = dt.date.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректная дата")

    today = msk_today()
    if day < today or day > today + dt.timedelta(days=HORIZON_DAYS):
        raise HTTPException(status_code=400, detail="Бронь доступна на 14 дней вперёд")

    async with Session() as s:
        venue = await s.get(Venue, body.venue_id)
        if not venue or not venue.active:
            raise HTTPException(status_code=404, detail="Заведение не найдено")
        if not works_on(venue, day):
            raise HTTPException(status_code=409, detail="В этот день у заведения нет тихого часа")
        slot = await s.scalar(select(Slot).where(Slot.venue_id == venue.id, Slot.hour == body.hour))
        if not slot:
            raise HTTPException(status_code=404, detail="Такого окна нет")
        if day == today and body.hour <= msk_now().hour:
            raise HTTPException(status_code=409, detail="Это окно уже прошло")
        # дальше работаем простыми значениями: ORM-объекты живут только внутри сессии
        venue_id, venue_name = venue.id, venue.name
        slot_id, slot_disc = slot.id, slot.discount

    key = (body.venue_id, body.hour, body.date)
    async with _slot_locks[key]:
        async with Session() as s:
            dup = await s.scalar(
                select(func.count(Booking.id)).where(
                    Booking.user_id == guest.id, Booking.venue_id == body.venue_id,
                    Booking.slot_id == slot_id, Booking.visit_date == day,
                    Booking.status == "active")
            )
            if dup:
                raise HTTPException(status_code=409, detail="Вы уже забронировали это окно")

            caps = await _capacity_map(s, [venue_id], day.weekday())
            cap = caps.get((venue_id, body.hour), DEFAULT_CAPACITY)
            taken = await s.scalar(
                select(func.count(Booking.id)).where(
                    Booking.venue_id == venue_id, Booking.slot_id == slot_id,
                    Booking.visit_date == day, Booking.status == "active")
            ) or 0
            if taken >= cap:
                raise HTTPException(status_code=409, detail="Все места на это время разобрали")

            user = await s.get(User, guest.id)
            if body.pay_with_points:
                if user.points < DEPOSIT:
                    raise HTTPException(status_code=402, detail="Недостаточно баллов")
                await add_points(s, user, -DEPOSIT, "deposit_points")

            for _ in range(6):
                code = _new_code()
                exists = await s.scalar(select(func.count(Booking.id)).where(Booking.code == code))
                if not exists:
                    break
            else:
                raise HTTPException(status_code=500, detail="Не удалось выдать код брони")

            bk = Booking(code=code, user_id=user.id, venue_id=venue_id,
                         slot_id=slot_id, visit_date=day, status="active")
            s.add(bk)
            await add_points(s, user, POINTS_PER_BOOKING, "booking")
            await s.commit()
            bk_id, points = bk.id, user.points

    await _notify_partner(venue_id, venue_name, body.hour, slot_disc, day, code, guest.name)
    return {"ok": True, "id": bk_id, "code": code, "discount": slot_disc,
            "points": points, "deposit": DEPOSIT}


@router.get("/bookings")
async def my_bookings(x_init_data: str = Header(default="", alias="X-Init-Data")):
    guest = await current_guest(x_init_data)
    async with Session() as s:
        rows = (await s.execute(
            select(Booking, Venue, Slot)
            .join(Venue, Venue.id == Booking.venue_id)
            .join(Slot, Slot.id == Booking.slot_id)
            .where(Booking.user_id == guest.id)
            .order_by(Booking.visit_date.desc(), Booking.id.desc())
            .limit(50)
        )).all()
        user = await s.get(User, guest.id)
    today = msk_today()
    items = [{
        "id": b.id, "code": b.code, "status": b.status,
        "date": b.visit_date.isoformat(), "hour": sl.hour, "discount": sl.discount,
        "venue": {"id": v.id, "name": v.name, "cat": v.cat, "place": v.place,
                  "district": v.district, "district_name": DIST_NAME.get(v.district, v.district)},
        "upcoming": b.status == "active" and b.visit_date >= today,
    } for b, v, sl in rows]
    return {"points": user.points, "visits": user.visits,
            "name": user.name, "deposit": DEPOSIT, "bookings": items}


class CancelIn(BaseModel):
    booking_id: int


@router.post("/bookings/cancel")
async def cancel_booking(body: CancelIn,
                         x_init_data: str = Header(default="", alias="X-Init-Data")):
    """Отмена. Метод POST, а не DELETE, — CORS в api.py разрешает
    только GET/POST/OPTIONS, менять его ради одного маршрута не стоит."""
    guest = await current_guest(x_init_data)
    async with Session() as s:
        bk = await s.get(Booking, body.booking_id)
        if not bk or bk.user_id != guest.id:
            raise HTTPException(status_code=404, detail="Бронь не найдена")
        if bk.status != "active":
            raise HTTPException(status_code=409, detail="Бронь уже закрыта")
        bk.status = "cancelled"
        await s.commit()
    return {"ok": True}


async def _notify_partner(venue_id: int, venue_name: str, hour: int, discount: int,
                          day: dt.date, code: str, guest_name: str) -> None:
    """Заведение должно узнать о госте — иначе бронь бессмысленна."""
    if not BOT_TOKEN:
        return
    async with Session() as s:
        prof = await s.scalar(select(VenueProfile).where(VenueProfile.venue_id == venue_id))
        targets: list[int] = []
        if prof:
            targets = list(await s.scalars(
                select(PartnerUser.tg_id).where(
                    PartnerUser.partner_id == prof.partner_id,
                    PartnerUser.active.is_(True))
            ))
    if not targets:
        targets = list(ADMIN_IDS)
    if not targets:
        return

    text = (f"📅 <b>Новая бронь</b>\n\n"
            f"<b>{html.escape(venue_name)}</b>\n"
            f"{day.strftime('%d.%m')} · {hour}:00–{hour + 1}:00 · −{discount}%\n"
            f"Гость: {html.escape(guest_name)}\n"
            f"Код: <code>{code}</code>\n\n"
            f"Подтвердить визит: /visit {code}")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    for tg_id in targets:
        try:
            await bot.send_message(tg_id, text)
        except Exception:
            pass
    await bot.session.close()
