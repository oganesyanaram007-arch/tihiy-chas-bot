# -*- coding: utf-8 -*-
"""Модели партнёрского кабинета.

Решения, зафиксированные с основателем:
  · вход только через Telegram (без SMS и паролей)
  · один кабинет на организацию, точек внутри может быть несколько
  · визит подтверждает хостес или официант со своего телефона
  · модерирует пока один человек, позже появятся сотрудники
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (BigInteger, Boolean, DateTime, Float, ForeignKey,
                        Integer, String, Text)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Partner(Base):
    """Организация-партнёр. Один кабинет = одна запись."""
    __tablename__ = "partners"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(160))          # как называть в кабинете
    legal_name: Mapped[str] = mapped_column(String(200), default="")
    inn: Mapped[str] = mapped_column(String(16), default="")
    tariff: Mapped[str] = mapped_column(String(16), default="standard")
    # new → moderation → active → paused / blocked
    status: Mapped[str] = mapped_column(String(16), default="new")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class PartnerUser(Base):
    """Человек с доступом в кабинет. Идентифицируется Telegram-аккаунтом."""
    __tablename__ = "partner_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"))
    name: Mapped[str] = mapped_column(String(128), default="")
    username: Mapped[str] = mapped_column(String(64), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    role: Mapped[str] = mapped_column(String(12), default="owner")   # owner / staff
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    last_login: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class StaffInvite(Base):
    """Одноразовая ссылка-приглашение сотрудника в кабинет."""
    __tablename__ = "staff_invites"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"))
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(12), default="staff")
    used_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class VenueProfile(Base):
    """Данные точки, которые заводит сам партнёр.

    Отдельная таблица, чтобы не ломать уже работающую venues из бота:
    та отвечает за витрину, эта — за то, что редактирует партнёр.
    """
    __tablename__ = "venue_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id"), unique=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"))
    district: Mapped[str] = mapped_column(String(32), default="")
    address: Mapped[str] = mapped_column(String(200), default="")
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    phone: Mapped[str] = mapped_column(String(32), default="")
    url: Mapped[str] = mapped_column(String(200), default="")
    about: Mapped[str] = mapped_column(Text, default="")
    cover_url: Mapped[str] = mapped_column(String(300), default="")
    # draft → moderation → active → paused
    status: Mapped[str] = mapped_column(String(16), default="draft")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class VenuePhoto(Base):
    __tablename__ = "venue_photos"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id"))
    url: Mapped[str] = mapped_column(String(300))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class QuietWindow(Base):
    """Тихое окно: день недели + час + скидка + лимит мест.

    Отдельно от slots, потому что партнёр мыслит неделей, а не одним днём.
    """
    __tablename__ = "quiet_windows"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)        # 0 — понедельник
    hour: Mapped[int] = mapped_column(Integer)           # 10..21
    discount: Mapped[int] = mapped_column(Integer)       # 25..50
    capacity: Mapped[int] = mapped_column(Integer, default=4)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class AuditLog(Base):
    """Кто и что менял. Нужен при спорах о ценах и скидках."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    partner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    partner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(48))
    entity: Mapped[str] = mapped_column(String(32), default="")
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


# =====================================================================
# Вход в кабинет с сайта, а не только из Telegram
# =====================================================================

class CabLogin(Base):
    """Одноразовый код входа. Бот присылает ссылку с ним, обмен даёт сессию.

    Храним не сам код, а его sha256: утечка базы не даст войти в кабинеты.
    """
    __tablename__ = "cab_logins"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class CabSession(Base):
    """Сессия кабинета в обычном браузере. Живёт в httpOnly-куке."""
    __tablename__ = "cab_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    partner_user_id: Mapped[int] = mapped_column(ForeignKey("partner_users.id"))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
