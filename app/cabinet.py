# -*- coding: utf-8 -*-
"""API партнёрского кабинета.

Этап 1: вход и доступы (login, me, invites, staff).
Этап 2: онбординг заведения — партнёр сам заводит точку, грузит фото,
настраивает тихие часы и отправляет на модерацию. Ничего не публикуется
без ручного одобрения (см. handlers/partner.py) — витрина остаётся
доверенной, как обсуждали с основателем.
"""
from __future__ import annotations

import base64
import contextvars
import datetime as dt
import hashlib
import html
import os
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select

from . import booking_flow, notify
from .tz import fmt_dt, fmt_slot, msk_today, utc_now
from .auth import AuthError, verify_init_data
from .config import ADMIN_IDS, BOT_TOKEN
from .db import (Booking, BookingEvent, Notification, Session, Slot, User,
                 Venue, WebSession, add_points)
from .models_partner import (AuditLog, CabLogin, CabSession, ManualReview,
                             Partner, PartnerUser, QuietWindow, StaffInvite,
                             VenuePhoto, VenueProfile)

router = APIRouter(prefix="/api/cab", tags=["cabinet"])

INVITE_TTL_HOURS = 72
MAX_PHOTOS = 5
MAX_PHOTO_BYTES = 8_000_000  # ~8 МБ, фото с телефона
CATS = {"food", "coffee", "beauty", "spa", "fun", "auto"}
CAT_LABEL = {"food": "Ресторан / кафе", "coffee": "Кофейня", "beauty": "Красота",
            "spa": "СПА и здоровье", "fun": "Досуг", "auto": "Авто"}


# ---------------------------------------------------------------------
# Вход в кабинет: два равноправных способа
#   1) X-Init-Data — подпись Telegram, работает внутри мини-аппа
#   2) кука tc_cab — обычный браузер, куда партнёр пришёл по ссылке из бота
# ---------------------------------------------------------------------
COOKIE_NAME = "tc_cab"
SESSION_DAYS = 30
LOGIN_TTL_MINUTES = 15
CABINET_URL = os.getenv("CABINET_URL",
                        "https://tihiy-chas.ru/tihiy-chas-cabinet.html")

# Кука читается из запроса один раз в ASGI-слое и кладётся в контекст задачи.
# Так вход по куке появляется у всех маршрутов кабинета сразу, без правки
# сигнатуры каждого обработчика — меньше правок, меньше шансов промахнуться.
_cab_cookie: contextvars.ContextVar[str] = contextvars.ContextVar("tc_cab", default="")
_web_cookie: contextvars.ContextVar[str] = contextvars.ContextVar("tc_web", default="")


class CabCookieMiddleware:
    """Чистое ASGI-middleware: работает в той же задаче, контекст не теряется."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        raw = ""
        for k, v in scope.get("headers", []):
            if k == b"cookie":
                raw = v.decode("latin-1")
                break
        token, web_token = "", ""
        for part in raw.split(";"):
            name, _, val = part.strip().partition("=")
            if name == COOKIE_NAME:
                token = val
            elif name == "tc_web":
                web_token = val
        ctx = _cab_cookie.set(token)
        ctx2 = _web_cookie.set(web_token)
        try:
            await self.app(scope, receive, send)
        finally:
            _cab_cookie.reset(ctx)
            _web_cookie.reset(ctx2)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def issue_login_code(tg_id: int) -> str:
    """Выдаёт одноразовый код входа. Вызывает бот, когда партнёр просит кабинет."""
    code = secrets.token_urlsafe(24)
    now = dt.datetime.utcnow()
    async with Session() as s:
        s.add(CabLogin(code_hash=_digest(code), tg_id=tg_id,
                       expires_at=now + dt.timedelta(minutes=LOGIN_TTL_MINUTES)))
        await s.commit()
    return code


async def _user_by_tg(s, tg_id: int) -> PartnerUser:
    user = await s.scalar(select(PartnerUser).where(PartnerUser.tg_id == tg_id))
    if not user or not user.active:
        raise HTTPException(status_code=403, detail="Нет доступа в кабинет")
    return user


async def current_user(x_init_data: str = Header(default="", alias="X-Init-Data")):
    """Партнёр из подписи Telegram или из сессионной куки."""
    now = dt.datetime.utcnow()
    tc_cab = _cab_cookie.get()

    if x_init_data:
        try:
            tg = verify_init_data(x_init_data)
        except AuthError as e:
            raise HTTPException(status_code=401, detail=str(e))
        async with Session() as s:
            user = await _user_by_tg(s, tg["id"])
            partner = await s.get(Partner, user.partner_id)
            user.last_login = now
            await s.commit()
            return {"user": user, "partner": partner, "tg": tg}

    if tc_cab:
        async with Session() as s:
            sess = await s.scalar(select(CabSession).where(
                CabSession.token_hash == _digest(tc_cab)))
            if not sess:
                raise HTTPException(status_code=401,
                                    detail="Сессия не найдена, войдите заново")
            if sess.expires_at < now:
                raise HTTPException(status_code=401,
                                    detail="Сессия истекла, войдите заново")
            user = await s.get(PartnerUser, sess.partner_user_id)
            if not user or not user.active:
                raise HTTPException(status_code=403, detail="Нет доступа в кабинет")
            partner = await s.get(Partner, user.partner_id)
            sess.last_seen = now
            user.last_login = now
            await s.commit()
            return {"user": user, "partner": partner, "tg": None}

    web_token = _web_cookie.get()
    if web_token:
        async with Session() as s:
            wsess = await s.scalar(select(WebSession).where(
                WebSession.token_hash == _digest(web_token)))
            if wsess and wsess.expires_at >= now:
                web_user = await s.get(User, wsess.user_id)
                if web_user and web_user.role == "partner":
                    user = await s.scalar(select(PartnerUser).where(
                        PartnerUser.tg_id == web_user.id))
                    if user and user.active:
                        partner = await s.get(Partner, user.partner_id)
                        user.last_login = now
                        await s.commit()
                        return {"user": user, "partner": partner, "tg": None}

    raise HTTPException(status_code=401, detail="Нужен вход в кабинет")


class SessionIn(BaseModel):
    code: str = Field(min_length=10, max_length=200)


@router.post("/session")
async def open_session(body: SessionIn, response: Response):
    """Меняет одноразовый код из бота на сессию в браузере."""
    now = dt.datetime.utcnow()
    async with Session() as s:
        row = await s.scalar(select(CabLogin).where(
            CabLogin.code_hash == _digest(body.code)))
        if not row:
            raise HTTPException(status_code=401,
                                detail="Ссылка недействительна — запросите новую в боте")
        if row.used_at:
            raise HTTPException(status_code=409,
                                detail="Ссылка уже использована — запросите новую в боте")
        if row.expires_at < now:
            raise HTTPException(status_code=410,
                                detail="Срок ссылки истёк — запросите новую в боте")

        user = await _user_by_tg(s, row.tg_id)
        row.used_at = now
        token = secrets.token_urlsafe(32)
        s.add(CabSession(token_hash=_digest(token), partner_user_id=user.id,
                         expires_at=now + dt.timedelta(days=SESSION_DAYS)))
        user.last_login = now
        partner = await s.get(Partner, user.partner_id)
        s.add(AuditLog(partner_user_id=user.id, partner_id=user.partner_id,
                       action="cab_login", entity="partner_user", entity_id=user.id))
        await s.commit()
        out = {"ok": True,
               "user": {"id": user.id, "name": user.name, "role": user.role},
               "partner": {"id": partner.id, "title": partner.title,
                           "status": partner.status, "tariff": partner.tariff}}

    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_DAYS * 86400,
                        httponly=True, secure=True, samesite="lax", path="/")
    return out


@router.post("/logout")
async def logout(response: Response):
    tc_cab = _cab_cookie.get()
    if tc_cab:
        async with Session() as s:
            sess = await s.scalar(select(CabSession).where(
                CabSession.token_hash == _digest(tc_cab)))
            if sess:
                await s.delete(sess)
                await s.commit()
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


class JoinIn(BaseModel):
    init_data: str = Field(min_length=10)
    invite: str = Field(default="", max_length=32)


@router.post("/login")
async def login(body: JoinIn):
    """Вход владельца или сотрудника.

    Если у Telegram-аккаунта ещё нет доступа, но передан код приглашения —
    создаём сотрудника и привязываем к организации.
    """
    try:
        tg = verify_init_data(body.init_data)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    full_name = " ".join(x for x in (tg["first_name"], tg["last_name"]) if x)

    async with Session() as s:
        user = await s.scalar(select(PartnerUser).where(PartnerUser.tg_id == tg["id"]))

        if not user and body.invite:
            inv = await s.scalar(select(StaffInvite).where(StaffInvite.code == body.invite))
            if not inv:
                raise HTTPException(status_code=404, detail="Приглашение не найдено")
            if inv.used_by:
                raise HTTPException(status_code=409, detail="Приглашение уже использовано")
            if inv.expires_at < dt.datetime.utcnow():
                raise HTTPException(status_code=410, detail="Срок приглашения истёк")

            user = PartnerUser(tg_id=tg["id"], partner_id=inv.partner_id,
                               name=full_name, username=tg["username"], role=inv.role)
            s.add(user)
            inv.used_by = tg["id"]
            await s.flush()
            s.add(AuditLog(partner_user_id=user.id, partner_id=inv.partner_id,
                           action="staff_joined", entity="partner_user",
                           entity_id=user.id))
            await s.commit()

        if not user:
            raise HTTPException(status_code=403,
                                detail="Заведение ещё не подключено. Оставьте заявку на сайте.")

        partner = await s.get(Partner, user.partner_id)
        user.last_login = dt.datetime.utcnow()
        if full_name and user.name != full_name:
            user.name = full_name
        await s.commit()

        return {
            "ok": True,
            "user": {"id": user.id, "name": user.name, "role": user.role},
            "partner": {"id": partner.id, "title": partner.title,
                        "status": partner.status, "tariff": partner.tariff},
        }


async def _linked_web_user(s, pu: PartnerUser):
    """Учётка сайта, привязанная к партнёру. У партнёров, заведённых через
    сайт, PartnerUser.tg_id хранит id из users — оттуда берётся почта.
    У пришедших из Telegram такой записи нет, и это нормально."""
    web = await s.get(User, pu.tg_id)
    return web if (web and web.role == "partner") else None


@router.get("/me")
async def me(x_init_data: str = Header(default="", alias="X-Init-Data")):
    ctx = await current_user(x_init_data)
    u, p = ctx["user"], ctx["partner"]
    async with Session() as s:
        web = await _linked_web_user(s, u)
    return {
        # phone и email нужны экрану профиля: без них поля стояли пустыми,
        # хотя телефон лежит в базе.
        "user": {"id": u.id, "name": u.name, "role": u.role, "username": u.username,
                 "phone": u.phone or "", "email": (web.email if web else "") or ""},
        "partner": {"id": p.id, "title": p.title, "status": p.status,
                    "tariff": p.tariff, "inn": p.inn},
    }


class CabProfileIn(BaseModel):
    name: str = Field(default="", max_length=128)
    phone: str = Field(default="", max_length=32)


@router.patch("/profile")
async def update_cab_profile(body: CabProfileIn,
                             x_init_data: str = Header(default="", alias="X-Init-Data")):
    """Правка контактов владельца из кабинета.

    Отдельно от /api/auth/profile: тот работает только по куке сайта, а в
    кабинет пускают ещё и по подписи Telegram, и по куке бота. Партнёр из
    Telegram получал бы там 401 и вылетал на страницу входа.
    """
    ctx = await current_user(x_init_data)
    async with Session() as s:
        u = await s.get(PartnerUser, ctx["user"].id)
        name, phone = body.name.strip(), body.phone.strip()
        if name:
            u.name = name[:128]
        u.phone = phone[:32]
        web = await _linked_web_user(s, u)
        if web:                      # держим обе учётки в согласии
            if name:
                web.name = name[:128]
            web.phone = u.phone
        await s.commit()
        return {"ok": True, "user": {"name": u.name, "phone": u.phone,
                                     "email": (web.email if web else "") or ""}}


class InviteIn(BaseModel):
    role: str = Field(default="staff", pattern="^(staff|owner)$")


@router.post("/invites")
async def create_invite(body: InviteIn,
                        x_init_data: str = Header(default="", alias="X-Init-Data")):
    """Владелец создаёт ссылку-приглашение для хостес или официанта."""
    ctx = await current_user(x_init_data)
    if ctx["user"].role != "owner":
        raise HTTPException(status_code=403, detail="Приглашать может только владелец")

    code = secrets.token_urlsafe(12)
    async with Session() as s:
        s.add(StaffInvite(
            partner_id=ctx["partner"].id, code=code, role=body.role,
            expires_at=dt.datetime.utcnow() + dt.timedelta(hours=INVITE_TTL_HOURS)))
        s.add(AuditLog(partner_user_id=ctx["user"].id, partner_id=ctx["partner"].id,
                       action="invite_created", entity="staff_invite", payload=body.role))
        await s.commit()

    return {"ok": True, "code": code, "expires_hours": INVITE_TTL_HOURS}


@router.get("/staff")
async def staff(x_init_data: str = Header(default="", alias="X-Init-Data")):
    ctx = await current_user(x_init_data)
    async with Session() as s:
        rows = (await s.scalars(select(PartnerUser).where(
            PartnerUser.partner_id == ctx["partner"].id))).all()
    return {"staff": [{"id": r.id, "name": r.name, "role": r.role,
                       "active": r.active,
                       "last_login": r.last_login.isoformat() if r.last_login else None}
                      for r in rows]}


# =====================================================================
# ЭТАП 2 — онбординг заведения
# =====================================================================

async def _owned_venue(s, ctx, venue_id: int) -> tuple[Venue, VenueProfile]:
    """Достаёт заведение и проверяет, что оно принадлежит текущему партнёру."""
    profile = await s.scalar(select(VenueProfile).where(VenueProfile.venue_id == venue_id))
    if not profile or profile.partner_id != ctx["partner"].id:
        raise HTTPException(status_code=404, detail="Заведение не найдено")
    venue = await s.get(Venue, venue_id)
    return venue, profile


def _venue_out(v: Venue, p: VenueProfile, photos: list, windows: list[dict]) -> dict:
    """photos — список {"id": ..., "url": ...}. Раньше отдавались голые строки,
    и кабинет не мог удалить снимок: у него не было id для DELETE."""
    return {
        "id": v.id, "name": v.name, "cat": v.cat, "cat_label": CAT_LABEL.get(v.cat, v.cat),
        "district": p.district, "address": p.address, "phone": p.phone, "url": p.url,
        "about": p.about, "cover_url": p.cover_url, "photos": photos,
        "status": p.status, "quiet_windows": windows,
    }


# ---------- Шаг 1: карточка заведения ----------
class VenueStep1(BaseModel):
    name: str = Field(min_length=2, max_length=64)
    cat: str
    district: str = Field(min_length=1, max_length=32)
    address: str = Field(min_length=4, max_length=200)
    phone: str = Field(default="", max_length=32)
    url: str = Field(default="", max_length=200)

    @field_validator("cat")
    @classmethod
    def cat_known(cls, v: str) -> str:
        if v not in CATS:
            raise ValueError(f"неизвестная категория: {v}")
        return v


@router.get("/venues")
async def list_venues(x_init_data: str = Header(default="", alias="X-Init-Data")):
    ctx = await current_user(x_init_data)
    async with Session() as s:
        rows = (await s.execute(
            select(Venue, VenueProfile)
            .join(VenueProfile, VenueProfile.venue_id == Venue.id)
            .where(VenueProfile.partner_id == ctx["partner"].id))).all()
        return {"venues": [{"id": v.id, "name": v.name, "status": p.status,
                            "cat_label": CAT_LABEL.get(v.cat, v.cat),
                            "district": p.district} for v, p in rows]}


@router.get("/venues/{venue_id}")
async def get_venue(venue_id: int, x_init_data: str = Header(default="", alias="X-Init-Data")):
    ctx = await current_user(x_init_data)
    async with Session() as s:
        v, p = await _owned_venue(s, ctx, venue_id)
        rows = (await s.execute(select(VenuePhoto.id, VenuePhoto.url).where(
            VenuePhoto.venue_id == venue_id).order_by(VenuePhoto.sort_order, VenuePhoto.id))).all()
        photos = [{"id": pid, "url": purl} for pid, purl in rows]
        wins = (await s.scalars(select(QuietWindow).where(
            QuietWindow.venue_id == venue_id, QuietWindow.active.is_(True)))).all()
        windows = [{"weekday": w.weekday, "hour": w.hour, "discount": w.discount,
                   "capacity": w.capacity} for w in wins]
        return _venue_out(v, p, photos, windows)


@router.post("/venues")
async def create_venue(body: VenueStep1,
                       x_init_data: str = Header(default="", alias="X-Init-Data")):
    """Шаг 1. Создаёт черновик заведения — сразу с минимумом данных,
    остальное партнёр донастраивает на следующих шагах."""
    ctx = await current_user(x_init_data)
    async with Session() as s:
        venue = Venue(name=body.name, cat=body.cat, district=body.district,
                      place=f"{CAT_LABEL.get(body.cat, body.cat)} · {body.address}",
                      check_note="", left_note="", weekdays="", active=False)
        s.add(venue)
        await s.flush()
        profile = VenueProfile(venue_id=venue.id, partner_id=ctx["partner"].id,
                               district=body.district, address=body.address,
                               phone=body.phone, url=body.url, status="draft")
        s.add(profile)
        s.add(AuditLog(partner_user_id=ctx["user"].id, partner_id=ctx["partner"].id,
                       action="venue_created", entity="venue", entity_id=venue.id))
        await s.commit()
        return {"ok": True, "venue_id": venue.id}


@router.patch("/venues/{venue_id}")
async def update_venue(venue_id: int, body: VenueStep1,
                       x_init_data: str = Header(default="", alias="X-Init-Data")):
    ctx = await current_user(x_init_data)
    async with Session() as s:
        v, p = await _owned_venue(s, ctx, venue_id)
        if p.status == "moderation":
            raise HTTPException(status_code=409,
                                detail="Заведение уже на модерации — дождитесь решения")
        v.name, v.cat = body.name, body.cat
        v.place = f"{CAT_LABEL.get(body.cat, body.cat)} · {body.address}"
        p.district, p.address, p.phone, p.url = body.district, body.address, body.phone, body.url
        p.updated_at = dt.datetime.utcnow()
        s.add(AuditLog(partner_user_id=ctx["user"].id, partner_id=ctx["partner"].id,
                       action="venue_updated", entity="venue", entity_id=venue_id))
        await s.commit()
        return {"ok": True}


# ---------- Шаг 2: фотографии ----------
class PhotoIn(BaseModel):
    data_url: str = Field(min_length=20)  # "data:image/jpeg;base64,...."

    @field_validator("data_url")
    @classmethod
    def looks_like_image(cls, v: str) -> str:
        if not v.startswith("data:image/"):
            raise ValueError("ожидается data:image/... base64")
        return v


@router.post("/venues/{venue_id}/photos")
async def add_photo(venue_id: int, body: PhotoIn,
                    x_init_data: str = Header(default="", alias="X-Init-Data")):
    ctx = await current_user(x_init_data)
    try:
        b64 = body.data_url.split(",", 1)[1]
        raw_len = len(base64.b64decode(b64, validate=False))
    except Exception:
        raise HTTPException(status_code=400, detail="Не удалось прочитать изображение")
    if raw_len > MAX_PHOTO_BYTES:
        # Предел и текст берутся из одного места: раньше лимит подняли до 8 МБ,
        # а сообщение осталось про 1.5 МБ — партнёр не понимал, что не так.
        raise HTTPException(
            status_code=413,
            detail=f"Фото слишком большое, до {MAX_PHOTO_BYTES // 1_000_000} МБ")

    async with Session() as s:
        v, p = await _owned_venue(s, ctx, venue_id)
        n = await s.scalar(select(VenuePhoto).where(VenuePhoto.venue_id == venue_id))
        count = len((await s.scalars(select(VenuePhoto.id).where(
            VenuePhoto.venue_id == venue_id))).all())
        if count >= MAX_PHOTOS:
            raise HTTPException(status_code=409, detail=f"Максимум {MAX_PHOTOS} фото")
        photo = VenuePhoto(venue_id=venue_id, url=body.data_url, sort_order=count)
        s.add(photo)
        if count == 0:
            p.cover_url = body.data_url
        p.updated_at = dt.datetime.utcnow()   # метка версии обложки для витрины
        await s.commit()
        pid = photo.id
    from .guest_api import invalidate_photo_cache
    invalidate_photo_cache(venue_id)
    return {"ok": True, "photo_id": pid, "is_cover": count == 0}


@router.delete("/venues/{venue_id}/photos/{photo_id}")
async def delete_photo(venue_id: int, photo_id: int,
                       x_init_data: str = Header(default="", alias="X-Init-Data")):
    ctx = await current_user(x_init_data)
    async with Session() as s:
        v, p = await _owned_venue(s, ctx, venue_id)
        photo = await s.get(VenuePhoto, photo_id)
        if not photo or photo.venue_id != venue_id:
            raise HTTPException(status_code=404, detail="Фото не найдено")
        await s.delete(photo)
        await s.flush()
        # Обложка профиля — копия первого снимка. Если её не пересобрать,
        # витрина продолжит показывать удалённое фото через запасной путь.
        rest = (await s.scalars(select(VenuePhoto.url).where(
            VenuePhoto.venue_id == venue_id).order_by(
            VenuePhoto.sort_order, VenuePhoto.id))).all()
        p.cover_url = rest[0] if rest else ""
        p.updated_at = dt.datetime.utcnow()   # метка версии обложки для витрины
        await s.commit()
    from .guest_api import invalidate_photo_cache
    invalidate_photo_cache(venue_id)
    return {"ok": True, "photos_left": len(rest)}


# ---------- Шаг 3: тихие часы ----------
class QuietSlotIn(BaseModel):
    hour: int = Field(ge=10, le=21)
    discount: int = Field(ge=20, le=50)
    capacity: int = Field(default=4, ge=1, le=30)


class QuietHoursIn(BaseModel):
    weekdays: list[int] = Field(min_length=1, max_length=7)
    slots: list[QuietSlotIn] = Field(min_length=1, max_length=12)

    @field_validator("weekdays")
    @classmethod
    def weekdays_valid(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("день недели должен быть 0..6")
        return sorted(set(v))


@router.put("/venues/{venue_id}/quiet-hours")
async def set_quiet_hours(venue_id: int, body: QuietHoursIn,
                          x_init_data: str = Header(default="", alias="X-Init-Data")):
    """Шаг 3. Партнёр задаёт дни недели и часы со скидкой — те же слоты
    действуют во все выбранные дни (кнопка «скопировать на все будни»
    на фронте). Полностью пересобираем сетку при каждом сохранении."""
    ctx = await current_user(x_init_data)
    async with Session() as s:
        v, p = await _owned_venue(s, ctx, venue_id)
        if p.status == "moderation":
            raise HTTPException(status_code=409, detail="Заведение уже на модерации")
        await s.execute(delete(QuietWindow).where(QuietWindow.venue_id == venue_id))
        for wd in body.weekdays:
            for sl in body.slots:
                s.add(QuietWindow(venue_id=venue_id, weekday=wd, hour=sl.hour,
                                  discount=sl.discount, capacity=sl.capacity))
        p.updated_at = dt.datetime.utcnow()
        s.add(AuditLog(partner_user_id=ctx["user"].id, partner_id=ctx["partner"].id,
                       action="quiet_hours_set", entity="venue", entity_id=venue_id,
                       payload=f"{len(body.weekdays)}d × {len(body.slots)}h"))
        await s.commit()
        return {"ok": True, "windows": len(body.weekdays) * len(body.slots)}


# ---------- Шаг 4: отправка на модерацию ----------
@router.post("/venues/{venue_id}/submit")
async def submit_venue(venue_id: int,
                       x_init_data: str = Header(default="", alias="X-Init-Data")):
    ctx = await current_user(x_init_data)
    async with Session() as s:
        v, p = await _owned_venue(s, ctx, venue_id)
        if p.status == "moderation":
            raise HTTPException(status_code=409, detail="Уже отправлено на модерацию")
        if p.status == "active":
            raise HTTPException(status_code=409, detail="Заведение уже опубликовано")

        missing = []
        if not p.address:
            missing.append("адрес")
        if not p.phone:
            missing.append("телефон")
        photos_n = len((await s.scalars(select(VenuePhoto.id).where(
            VenuePhoto.venue_id == venue_id))).all())
        if photos_n == 0:
            missing.append("хотя бы одно фото")
        windows_n = len((await s.scalars(select(QuietWindow.id).where(
            QuietWindow.venue_id == venue_id, QuietWindow.active.is_(True)))).all())
        if windows_n == 0:
            missing.append("тихие часы")
        if missing:
            raise HTTPException(status_code=422,
                                detail="Заполните перед отправкой: " + ", ".join(missing))

        p.status = "moderation"
        if ctx["partner"].status == "new":
            ctx["partner"].status = "moderation"
        s.add(AuditLog(partner_user_id=ctx["user"].id, partner_id=ctx["partner"].id,
                       action="venue_submitted", entity="venue", entity_id=venue_id))
        # В той же транзакции: заявка и уведомление о ней либо есть оба,
        # либо нет обоих.
        await _queue_admin_submission(s, venue_id, v.name, p.district,
                                      ctx["partner"].id)
        await s.commit()

    return {"ok": True, "status": "moderation"}


async def _queue_admin_submission(s, venue_id: int, name: str, district: str,
                                  partner_id: int) -> None:
    """Поставить админам уведомление о заявке на модерацию.

    В очередь, а не напрямую: раньше отправка держала ответ партнёру,
    а сбой Telegram глушился и заявка молча оставалась незамеченной.

    Кнопки «Одобрить/Отклонить» из очереди не уходят — она отправляет
    только текст. Решение принимается командой /venue_<id>, а очередь
    команд не теряет: заявку видно в /pending в любом случае.
    """
    if not ADMIN_IDS:
        return
    from .keyboards import DIST_NAME
    text = (f"🆕 <b>Новое заведение на модерации</b>\n\n"
            f"<b>{html.escape(name)}</b>\n"
            f"Район: {DIST_NAME.get(district, district)}\n"
            f"Партнёр #{partner_id} · Заведение #{venue_id}\n\n"
            f"Проверьте адрес, фото и часы перед публикацией — "
            f"/venue_{venue_id} покажет карточку целиком.")
    await notify.enqueue_many(s, "admin_venue_submitted", ADMIN_IDS, text)

# ---------------------------------------------------------------------
# PARTNER_BOOKINGS: брони по заведениям партнёра.
# Раньше владелец узнавал о госте только из Telegram — у веб-партнёра
# телеграма нет, поэтому список живёт в кабинете.
# ---------------------------------------------------------------------

@router.get("/bookings")
async def partner_bookings(ctx=Depends(current_user)):
    """Все брони заведений партнёра: сначала ближайшие."""
    partner = ctx["partner"]
    # Московское время, а не серверное: после полуночи по Москве сервер в UTC
    # считал «сегодня» вчерашним днём, и у гостя с заведением расходились
    # и дата брони, и признак «предстоит».
    today = msk_today()
    async with Session() as s:
        venue_ids = list(await s.scalars(
            select(VenueProfile.venue_id).where(VenueProfile.partner_id == partner.id)))
        if not venue_ids:
            return {"bookings": [], "new_count": 0}
        rows = (await s.execute(
            select(Booking, Venue, Slot, User)
            .join(Venue, Venue.id == Booking.venue_id)
            .join(Slot, Slot.id == Booking.slot_id)
            .join(User, User.id == Booking.user_id)
            .where(Booking.venue_id.in_(venue_ids))
            .order_by(Booking.visit_date.desc(), Slot.hour.desc())
            .limit(200)
        )).all()

    items = [{
        "id": b.id, "code": b.code, "status": b.status,
        "date": b.visit_date.isoformat(), "hour": sl.hour, "discount": sl.discount,
        "venue_id": v.id, "venue_name": v.name,
        "guest_name": u.name, "guest_phone": u.phone or "",
        "upcoming": b.status == "active" and b.visit_date >= today,
    } for b, v, sl, u in rows]
    new_count = sum(1 for i in items if i["upcoming"])
    return {"bookings": items, "new_count": new_count}


def _actor(request: Request) -> dict:
    """Откуда пришла отметка — для журнала брони."""
    return {
        "ip": (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
               or (request.client.host if request.client else "")),
        "device": request.headers.get("user-agent", ""),
    }


@router.get("/bookings/today")
async def bookings_today(ctx=Depends(current_user)):
    """Экран смены: кто придёт сегодня, с кодами и временем.

    Это источник правды для персонала — не уведомление в Telegram.
    Сообщение может не дойти, партнёр может заблокировать бота, телефон
    может быть не тот. Этот экран не зависит ни от чего из перечисленного.

    Отдельно от /bookings: тот отдаёт двести записей за все даты, а хостес
    у входа нужны только сегодняшние и в порядке времени.
    """
    partner = ctx["partner"]
    day = msk_today()
    async with Session() as s:
        venue_ids = list(await s.scalars(
            select(VenueProfile.venue_id).where(VenueProfile.partner_id == partner.id)))
        if not venue_ids:
            return {"date": day.isoformat(), "bookings": [], "waiting": 0,
                    "arrived": 0, "notifications_blocked": False,
                    "manual_open": 0}
        rows = (await s.execute(
            select(Booking, Venue, Slot, User)
            .join(Venue, Venue.id == Booking.venue_id)
            .join(Slot, Slot.id == Booking.slot_id)
            .join(User, User.id == Booking.user_id)
            .where(Booking.venue_id.in_(venue_ids), Booking.visit_date == day)
            .order_by(Slot.hour, Booking.id))).all()

        # Заблокировал ли кто-то из сотрудников бота: иначе непонятно,
        # почему уведомления «не приходят», и люди винят сервис.
        # Открытые случаи «код не прошёл, гостя впустили»: если их не
        # показать здесь, они осядут в базе и никто не разберёт.
        manual_open = len(list(await s.scalars(
            select(ManualReview.id).where(ManualReview.partner_id == partner.id,
                                          ManualReview.status == "open"))))
        staff_ids = list(await s.scalars(
            select(PartnerUser.tg_id).where(PartnerUser.partner_id == partner.id)))
        blocked = False
        if staff_ids:
            blocked = bool(await s.scalar(
                select(Notification.id).where(Notification.chat_id.in_(staff_ids),
                                              Notification.status == "blocked").limit(1)))

    items = [{
        "id": b.id, "code": b.code, "status": b.status,
        "hour": sl.hour, "when": fmt_slot(b.visit_date, sl.hour),
        "discount": sl.discount,
        "venue_id": v.id, "venue_name": v.name,
        "guest_name": u.name, "guest_phone": u.phone or "",
        "redeemed_at": fmt_dt(b.redeemed_at) if b.redeemed_at else None,
    } for b, v, sl, u in rows]
    return {
        "date": day.isoformat(),
        "bookings": items,
        "waiting": sum(1 for i in items if i["status"] == "active"),
        "arrived": sum(1 for i in items if i["status"] == "visited"),
        "notifications_blocked": blocked,
        "manual_open": manual_open,
    }


class VisitIn(BaseModel):
    booking_id: int


class RedeemIn(BaseModel):
    code: str = Field(min_length=2, max_length=32)


@router.post("/bookings/visit")
async def confirm_visit(body: VisitIn, request: Request, ctx=Depends(current_user)):
    """Кнопка «Пришёл» напротив брони в списке.

    Окно действия кода тут не проверяем: сотрудник смотрит на конкретную
    бронь своего заведения и уже вошёл в кабинет. Смену часто закрывают
    в конце, и отказывать в отметке из-за получаса — значит заставить
    людей вести учёт мимо системы.
    """
    out = await booking_flow.redeem("", partner_id=ctx["partner"].id,
                                    partner_user_id=ctx["user"].id,
                                    booking_id=body.booking_id,
                                    enforce_window=False, **_actor(request))
    if not out.ok:
        raise HTTPException(status_code=out.http, detail=out.message)
    return {"ok": True, "booking": out.booking, "message": out.message}


@router.post("/bookings/redeem")
async def redeem_code(body: RedeemIn, request: Request, ctx=Depends(current_user)):
    """Погашение по коду: гость называет его вслух, сотрудник вводит.

    Сюда же приходит результат сканирования QR — камера лишь подставляет
    код в то же поле, отдельного пути для QR нет.
    """
    out = await booking_flow.redeem(body.code, partner_id=ctx["partner"].id,
                                    partner_user_id=ctx["user"].id, **_actor(request))
    if not out.ok:
        raise HTTPException(status_code=out.http, detail=out.message)
    return {"ok": True, "booking": out.booking, "message": out.message}


# ---------------------------------------------------------------------
# ДЕГРАДИРОВАННЫЙ РЕЖИМ: код не проходит, а гость уже стоит на входе
# ---------------------------------------------------------------------

class ManualIn(BaseModel):
    code: str = Field(default="", max_length=64)
    guest_hint: str = Field(default="", max_length=200)
    reason: str = Field(default="code_failed", max_length=64)


@router.post("/bookings/manual")
async def manual_admit(body: ManualIn, request: Request, ctx=Depends(current_user)):
    """«Код не проходит» — впустить гостя и разобраться потом.

    Отказ кода не должен решать, впускать ли человека. Он уже пришёл,
    а не сработать может что угодно: сеть в подвале, опечатка, просроченное
    окно, наша собственная ошибка. Сотрудник жмёт кнопку, сажает гостя
    и работает дальше — случай остаётся в разборе.

    Ручка намеренно не проверяет ничего, кроме входа в кабинет: любая
    дополнительная проверка здесь означала бы, что гостя снова можно
    не впустить из-за нашей ошибки.
    """
    partner, user = ctx["partner"], ctx["user"]
    meta = _actor(request)
    booking_id = None
    async with Session() as s:
        if body.code:
            venue_ids = list(await s.scalars(
                select(VenueProfile.venue_id).where(
                    VenueProfile.partner_id == partner.id)))
            if venue_ids:
                bk = await s.scalar(select(Booking).where(
                    Booking.code.in_(booking_flow.code_candidates(body.code)),
                    Booking.venue_id.in_(venue_ids)))
                booking_id = bk.id if bk else None
        review = ManualReview(
            partner_id=partner.id, partner_user_id=user.id,
            actor_name=user.name or "", raw_code=(body.code or "")[:64],
            booking_id=booking_id, guest_hint=body.guest_hint[:200],
            reason=body.reason[:64], status="open",
            ip=meta["ip"][:64], device=meta["device"][:200],
            created_at=utc_now())
        s.add(review)
        await s.flush()
        review_id = review.id
        # Если бронь всё же нашлась, оставляем след и в её журнале:
        # иначе при разборе непонятно, к чему относился случай.
        if booking_id:
            s.add(BookingEvent(
                booking_id=booking_id, action="manual_admit",
                status_from="", status_to="",
                actor_kind="staff", actor_id=user.id,
                actor_name=user.name or "", ip=meta["ip"][:64],
                device=meta["device"][:200],
                note=f"ручной разбор #{review_id}", created_at=utc_now()))
        await s.commit()
    return {"ok": True, "id": review_id, "booking_id": booking_id,
            "message": "Гостя можно впустить. Случай записан — разберём позже."}


@router.get("/manual-reviews")
async def manual_reviews(ctx=Depends(current_user)):
    """Открытые случаи ручного разбора — владельцу после смены."""
    partner = ctx["partner"]
    async with Session() as s:
        rows = (await s.scalars(
            select(ManualReview)
            .where(ManualReview.partner_id == partner.id,
                   ManualReview.status == "open")
            .order_by(ManualReview.id.desc()).limit(50))).all()
    return {"reviews": [{
        "id": r.id, "code": r.raw_code, "booking_id": r.booking_id,
        "guest_hint": r.guest_hint, "actor_name": r.actor_name,
        "created_at": fmt_dt(r.created_at),
    } for r in rows], "open_count": len(rows)}


class ResolveIn(BaseModel):
    review_id: int
    note: str = Field(default="", max_length=300)


@router.post("/manual-reviews/resolve")
async def resolve_manual(body: ResolveIn, ctx=Depends(current_user)):
    """Закрыть случай. Разбирает владелец, не хостес."""
    if ctx["user"].role != "owner":
        raise HTTPException(status_code=403, detail="Разбирать может только владелец")
    async with Session() as s:
        r = await s.get(ManualReview, body.review_id)
        if not r or r.partner_id != ctx["partner"].id:
            raise HTTPException(status_code=404, detail="Случай не найден")
        if r.status == "resolved":
            raise HTTPException(status_code=409, detail="Случай уже разобран")
        r.status = "resolved"
        r.note = body.note[:300]
        r.resolved_at = utc_now()
        r.resolved_by = ctx["user"].id
        await s.commit()
    return {"ok": True}
