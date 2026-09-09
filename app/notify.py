# -*- coding: utf-8 -*-
"""Очередь уведомлений и воркер, который её разбирает.

Раньше сообщение в Telegram уходило прямо из обработчика запроса:
`_notify_partner` создавал бота, слал сообщения по очереди и только потом
гость получал ответ. Ошибки глушились голым `except: pass`. Из этого
следовало сразу три беды:

  · залипший Telegram держал запрос гостя;
  · недоставленное уведомление исчезало бесследно — никто не знал, что
    заведение о госте не узнало;
  · партнёр, заблокировавший бота, выглядел так же, как временный сбой,
    и мы бесконечно пытались ему написать.

Теперь запись кладётся в очередь в той же транзакции, что и бронь.
Отправляет отдельный воркер, с нарастающими паузами между попытками.
Недоставка не ломает бронь: источник правды для персонала — экран
«Брони на сегодня» в кабинете, а не сообщение в мессенджере.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import select, update

from .config import BOT_TOKEN
from .db import Notification, Session
from .tz import utc_now

log = logging.getLogger(__name__)

# Паузы между попытками: минута, две, четыре… Дальше сдаёмся.
# Растущие, а не постоянные: если Telegram лежит, долбиться раз в минуту
# бессмысленно, а первая попытка почти всегда успешна.
BACKOFF_MINUTES = [1, 2, 4, 8, 16, 32]
MAX_ATTEMPTS = len(BACKOFF_MINUTES)
POLL_SECONDS = 10
BATCH = 20


async def enqueue(s, kind: str, chat_id: int, text: str, *,
                  booking_id: int | None = None,
                  parse_mode: str = "HTML") -> None:
    """Поставить сообщение в очередь.

    Принимает сессию, а не открывает свою: запись должна попасть в базу
    в той же транзакции, что и бронь. Иначе бывает «бронь есть, а
    уведомления нет» или наоборот.
    """
    s.add(Notification(kind=kind, chat_id=chat_id, text=text,
                       parse_mode=parse_mode, booking_id=booking_id,
                       status="pending", attempts=0,
                       next_attempt_at=utc_now(), created_at=utc_now()))


async def enqueue_many(s, kind: str, chat_ids, text: str, **kw) -> int:
    n = 0
    for chat_id in dict.fromkeys(chat_ids):     # без дублей, порядок сохраняем
        if chat_id:
            await enqueue(s, kind, chat_id, text, **kw)
            n += 1
    return n


# ---------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------

def _is_blocked(err: Exception) -> bool:
    """Партнёр запретил боту писать — повторять бессмысленно.

    Проверяем и по классу aiogram, и по тексту: набор исключений между
    версиями библиотеки меняется, а причина отказа в тексте остаётся.
    """
    try:
        from aiogram.exceptions import TelegramForbiddenError
        if isinstance(err, TelegramForbiddenError):
            return True
    except Exception:
        pass
    msg = str(err).lower()
    return ("bot was blocked" in msg or "user is deactivated" in msg
            or "chat not found" in msg or "bot can't initiate conversation" in msg)


async def _claim(limit: int) -> list[int]:
    """Забрать порцию готовых к отправке.

    Помечаем строки своими одним UPDATE и только потом отправляем: бот и
    API рестартуют вместе, воркер поднимается в обоих, и без этого одно
    сообщение ушло бы дважды.
    """
    now = utc_now()
    async with Session() as s:
        ids = list(await s.scalars(
            select(Notification.id)
            .where(Notification.status == "pending",
                   Notification.next_attempt_at <= now)
            .order_by(Notification.id).limit(limit)))
        if not ids:
            return []
        res = await s.execute(
            update(Notification)
            .where(Notification.id.in_(ids), Notification.status == "pending")
            .values(status="sending"))
        await s.commit()
        if res.rowcount == 0:
            return []
        # Забрать могли не всё: часть строк успел занять другой воркер.
        mine = list(await s.scalars(
            select(Notification.id).where(Notification.id.in_(ids),
                                          Notification.status == "sending")))
        return mine


async def _finish(note_id: int, *, ok: bool, blocked: bool = False,
                  error: str = "") -> None:
    async with Session() as s:
        note = await s.get(Notification, note_id)
        if not note:
            return
        if ok:
            note.status, note.sent_at, note.last_error = "sent", utc_now(), ""
        elif blocked:
            # Отдельный статус, а не «ошибка»: по нему в кабинете видно,
            # почему человек «ничего не получал», и что дело не в нас.
            note.status, note.last_error = "blocked", error[:300]
        else:
            note.attempts += 1
            note.last_error = error[:300]
            if note.attempts >= MAX_ATTEMPTS:
                note.status = "failed"
            else:
                note.status = "pending"
                note.next_attempt_at = utc_now() + dt.timedelta(
                    minutes=BACKOFF_MINUTES[note.attempts])
        await s.commit()


async def flush(bot=None) -> dict:
    """Отправить всё, что готово. Возвращает сводку — удобно тестам.

    bot передаётся снаружи, чтобы не создавать сессию к Telegram на каждое
    сообщение (так делал прежний код) и чтобы тесты могли подставить свой.
    """
    ids = await _claim(BATCH)
    if not ids:
        return {"sent": 0, "blocked": 0, "retry": 0, "failed": 0}

    own = bot is None
    if own:
        if not BOT_TOKEN:
            # Без токена отправлять нечем: возвращаем записи в очередь,
            # чтобы они не зависли в «sending» навсегда.
            for note_id in ids:
                await _finish(note_id, ok=False, error="BOT_TOKEN не задан")
            return {"sent": 0, "blocked": 0, "retry": len(ids), "failed": 0}
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    stats = {"sent": 0, "blocked": 0, "retry": 0, "failed": 0}
    try:
        for note_id in ids:
            async with Session() as s:
                note = await s.get(Notification, note_id)
                if not note:
                    continue
                chat_id, text, mode = note.chat_id, note.text, note.parse_mode
                attempts = note.attempts
            try:
                await bot.send_message(chat_id, text, parse_mode=mode or None)
            except Exception as e:                      # noqa: BLE001
                if _is_blocked(e):
                    await _finish(note_id, ok=False, blocked=True, error=str(e))
                    stats["blocked"] += 1
                    log.info("уведомление %s: адресат заблокировал бота", note_id)
                else:
                    await _finish(note_id, ok=False, error=str(e))
                    if attempts + 1 >= MAX_ATTEMPTS:
                        stats["failed"] += 1
                        log.warning("уведомление %s исчерпало попытки: %s", note_id, e)
                    else:
                        stats["retry"] += 1
            else:
                await _finish(note_id, ok=True)
                stats["sent"] += 1
    finally:
        if own:
            await bot.session.close()
    return stats


async def worker(stop: asyncio.Event | None = None) -> None:
    """Фоновый цикл. Запускается и в боте, и в API — забор порции атомарный."""
    log.info("воркер уведомлений запущен")
    while not (stop and stop.is_set()):
        try:
            stats = await flush()
            if any(stats.values()):
                log.info("уведомления: %s", stats)
        except Exception:                               # noqa: BLE001
            # Воркер не имеет права умереть: он один на весь процесс,
            # и без него очередь копится молча.
            log.exception("сбой в воркере уведомлений")
        try:
            await asyncio.wait_for(stop.wait() if stop else asyncio.sleep(POLL_SECONDS),
                                   timeout=POLL_SECONDS)
        except (asyncio.TimeoutError, AttributeError):
            pass


async def requeue_stuck(older_than_minutes: int = 5) -> int:
    """Вернуть в очередь застрявшие в «sending».

    Процесс мог упасть между захватом строки и отправкой. Такие записи
    иначе не увидит никто и никогда.
    """
    edge = utc_now() - dt.timedelta(minutes=older_than_minutes)
    async with Session() as s:
        res = await s.execute(
            update(Notification)
            .where(Notification.status == "sending",
                   Notification.created_at <= edge)
            .values(status="pending", next_attempt_at=utc_now()))
        await s.commit()
        if res.rowcount:
            log.info("возвращено в очередь застрявших: %d", res.rowcount)
        return res.rowcount
