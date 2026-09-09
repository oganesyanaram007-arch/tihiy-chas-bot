# -*- coding: utf-8 -*-
"""Очередь уведомлений и экран смены.

Запуск:
    PYTHONPATH=. python3 test_notify.py

Что здесь проверяется и почему. Раньше сообщение уходило прямо из
обработчика: гость ждал ответа, пока мы стучались в Telegram, ошибки
глушились голым except, а партнёр, заблокировавший бота, выглядел как
временный сбой. Главное требование — недоставка не должна ломать бронь,
а бронь должна быть видна в кабинете независимо от мессенджера.
"""
import asyncio, datetime as dt, os, pathlib, tempfile

TOKEN = "123456:TESTTOKENabcdefghijklmnopqrstuvw"
os.environ["BOT_TOKEN"] = TOKEN
os.environ.setdefault("ADMIN_IDS", "999999")
_db = pathlib.Path(tempfile.mkdtemp()) / "n.db"
os.environ["DB_URL"] = f"sqlite+aiosqlite:///{_db}"

OK = lambda m: print(f"  ✓ {m}")


class FakeBot:
    """Telegram, который можно заставить вести себя как угодно."""

    def __init__(self):
        self.sent, self.fail_with, self.fail_times = [], None, 0

    async def send_message(self, chat_id, text, parse_mode=None, **kw):
        if self.fail_times:
            self.fail_times -= 1
            raise self.fail_with
        self.sent.append((chat_id, text))

    class _S:
        async def close(self): pass
    session = _S()


class Blocked(Exception):
    def __str__(self): return "Forbidden: bot was blocked by the user"


async def run():
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app import notify
    from app.api import api
    from app.cabinet import _digest
    from app.db import (Booking, Notification, Session, Slot, User, Venue,
                        WebSession, init_db)
    from app.models_partner import CabSession, Partner, PartnerUser, VenueProfile
    from app.tz import msk_now, msk_today, utc_now
    from app.webauth import _digest as wd

    await init_db()

    async with Session() as s:
        p = Partner(title="Сеть", status="active"); s.add(p); await s.flush()
        pu = PartnerUser(tg_id=770101, partner_id=p.id, name="Марина",
                         role="owner", active=True); s.add(pu); await s.flush()
        v = Venue(name="Тбилисо", cat="food", district="petro", place="грузинская",
                  check_note="", left_note="", weekdays="0,1,2,3,4,5,6", active=True)
        s.add(v); await s.flush()
        s.add(VenueProfile(venue_id=v.id, partner_id=p.id, status="active"))
        # Час, который сегодня ещё не прошёл: бронировать в прошлое нельзя,
        # а тестам нужны и сегодняшние брони, и будущие.
        # Два окна: будущее — чтобы создавать брони через API (в прошлое
        # бронировать нельзя), и текущее — чтобы код можно было погасить,
        # он действует только в своём окне ±30 минут.
        hour = min(23, msk_now().hour + 2)
        sl = Slot(venue_id=v.id, hour=hour, discount=35); s.add(sl); await s.flush()
        sl_now = Slot(venue_id=v.id, hour=msk_now().hour, discount=30)
        s.add(sl_now); await s.flush()
        g = User(id=880101, name="Аркадий", phone="+7 921 000-11-22"); s.add(g)
        s.add(CabSession(token_hash=_digest("cab-token-notify"), partner_user_id=pu.id,
                         expires_at=dt.datetime.utcnow() + dt.timedelta(days=1)))
        s.add(WebSession(token_hash=wd("web-token-notify"), user_id=g.id,
                         expires_at=dt.datetime.utcnow() + dt.timedelta(days=1)))
        await s.commit()
        venue_id, slot_hour, slot_now_id = v.id, sl.hour, sl_now.id

    c = TestClient(api)
    CAB = {"Cookie": "tc_cab=cab-token-notify"}
    WEB = {"Cookie": "tc_web=web-token-notify"}

    async def queue(status=None):
        async with Session() as s:
            q = select(Notification)
            if status:
                q = q.where(Notification.status == status)
            return list(await s.scalars(q.order_by(Notification.id)))

    print("\n1. Бронь ставит уведомление в очередь, а не шлёт из обработчика")
    # На завтра: сегодняшнее окно могло уже пройти, а проверять мы хотим
    # постановку в очередь, а не календарь.
    r = c.post("/api/guest/bookings", headers=WEB,
               json={"venue_id": venue_id, "hour": slot_hour,
                     "date": (msk_today() + dt.timedelta(days=1)).isoformat()})
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    pending = await queue("pending")
    assert len(pending) == 1, [n.status for n in await queue()]
    assert pending[0].chat_id == 770101 and code in pending[0].text
    assert pending[0].booking_id == r.json()["id"]
    OK(f"в очереди одна запись для хостес, код {code} внутри")

    print("\n2. Отправка одной порцией, своим ботом")
    bot = FakeBot()
    stats = await notify.flush(bot)
    assert stats["sent"] == 1, stats
    assert bot.sent[0][0] == 770101
    assert (await queue("sent"))[0].sent_at is not None
    OK(f"отправлено {stats['sent']}, статус sent, время проставлено")

    print("\n3. Повторный прогон не шлёт то же самое снова")
    again = await notify.flush(bot)
    assert again["sent"] == 0 and len(bot.sent) == 1, (again, bot.sent)
    OK("очередь пуста, дублей нет")

    print("\n4. Временный сбой — попытка переносится, а не теряется")
    r = c.post("/api/guest/bookings", headers=WEB,
               json={"venue_id": venue_id, "hour": slot_hour,
                     "date": (msk_today() + dt.timedelta(days=3)).isoformat()})
    assert r.status_code == 200, r.text
    bot.fail_with, bot.fail_times = RuntimeError("Bad Gateway"), 1
    stats = await notify.flush(bot)
    assert stats["retry"] == 1, stats
    note = (await queue("pending"))[0]
    assert note.attempts == 1 and note.next_attempt_at > utc_now(), note.next_attempt_at
    OK(f"попытка 1, следующая через паузу, ошибка записана: «{note.last_error[:24]}…»")

    print("\n5. Паузы между попытками растут")
    delays = []
    for attempt in range(1, 4):
        async with Session() as s:
            n = await s.get(Notification, note.id)
            n.attempts, n.status = attempt, "pending"
            n.next_attempt_at = utc_now() - dt.timedelta(minutes=1)
            await s.commit()
        bot.fail_with, bot.fail_times = RuntimeError("Bad Gateway"), 1
        await notify.flush(bot)
        async with Session() as s:
            fresh = await s.get(Notification, note.id)
            delays.append(round((fresh.next_attempt_at - utc_now()).total_seconds() / 60))
    assert delays == sorted(delays) and delays[0] < delays[-1], delays
    OK(f"паузы в минутах: {delays}")

    print("\n6. Попытки кончаются — запись помечается failed, а не висит вечно")
    async with Session() as s:
        n = await s.get(Notification, note.id)
        n.attempts, n.status = notify.MAX_ATTEMPTS - 1, "pending"
        n.next_attempt_at = utc_now() - dt.timedelta(minutes=1)
        await s.commit()
    bot.fail_with, bot.fail_times = RuntimeError("Bad Gateway"), 1
    stats = await notify.flush(bot)
    assert stats["failed"] == 1, stats
    assert (await queue())[1].status == "failed"
    OK(f"после {notify.MAX_ATTEMPTS} попыток — failed")

    print("\n7. Партнёр заблокировал бота — отдельный статус, без повторов")
    r = c.post("/api/guest/bookings", headers=WEB,
               json={"venue_id": venue_id, "hour": slot_hour,
                     "date": (msk_today() + dt.timedelta(days=2)).isoformat()})
    assert r.status_code == 200, r.text
    bot.fail_with, bot.fail_times = Blocked(), 1
    stats = await notify.flush(bot)
    assert stats["blocked"] == 1, stats
    blocked = [n for n in await queue() if n.status == "blocked"]
    assert len(blocked) == 1
    before = len(bot.sent)
    assert (await notify.flush(bot))["sent"] == 0 and len(bot.sent) == before
    OK("статус blocked, повторных попыток нет")

    print("\n8. Недоставка не сломала ни одной брони")
    async with Session() as s:
        bookings = list(await s.scalars(select(Booking)))
    assert len(bookings) == 3, [b.code for b in bookings]
    assert all(b.status == "active" and b.code for b in bookings)
    OK(f"броней {len(bookings)}, все активны и с кодами")

    print("\n9. Партнёр видит все брони в кабинете, несмотря на блокировку")
    # Бронь на сегодня заводим напрямую: экран смены проверяем отдельно от
    # календарных ограничений на создание.
    async with Session() as s:
        s.add(Booking(code="TODAY1", user_id=880101, venue_id=venue_id,
                      slot_id=slot_now_id, visit_date=msk_today(), status="active"))
        await s.commit()
    r = c.get("/api/cab/bookings/today", headers=CAB)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["waiting"] == 1, data
    assert data["notifications_blocked"] is True, "кабинет должен показать, что бот заблокирован"
    assert data["bookings"][0]["code"], data
    OK(f"на сегодня: ждём {data['waiting']}, код {data['bookings'][0]['code']}, "
       f"блокировка бота показана")

    print("\n10. Экран смены отдаёт только сегодняшние и по времени")
    codes_today = [b["code"] for b in data["bookings"]]
    async with Session() as s:
        all_today = list(await s.scalars(
            select(Booking.code).where(Booking.visit_date == msk_today())))
    assert set(codes_today) == set(all_today), (codes_today, all_today)
    hours = [b["hour"] for b in data["bookings"]]
    assert hours == sorted(hours), hours
    OK(f"сегодня {len(codes_today)}, завтрашние не попали, порядок по времени")

    print("\n11. Погашение с экрана смены и отметка времени")
    r = c.post("/api/cab/bookings/redeem", headers=CAB,
               json={"code": data["bookings"][0]["code"]})
    assert r.status_code == 200, r.text
    after = c.get("/api/cab/bookings/today", headers=CAB).json()
    assert after["arrived"] == 1 and after["waiting"] == 0, after
    assert after["bookings"][0]["redeemed_at"], after["bookings"][0]
    OK(f"пришёл 1, ждём 0, отмечено в {after['bookings'][0]['redeemed_at']}")

    print("\n12. Застрявшие в «sending» возвращаются в очередь")
    async with Session() as s:
        stuck = Notification(kind="test", chat_id=770101, text="висяк",
                             status="sending", attempts=0,
                             created_at=utc_now() - dt.timedelta(minutes=30),
                             next_attempt_at=utc_now())
        s.add(stuck); await s.commit(); stuck_id = stuck.id
    n = await notify.requeue_stuck(older_than_minutes=5)
    assert n == 1, n
    async with Session() as s:
        assert (await s.get(Notification, stuck_id)).status == "pending"
    OK("процесс мог упасть между захватом и отправкой — запись не потеряна")

    print("\n13. Два воркера не отправляют одно сообщение дважды")
    async with Session() as s:
        for i in range(6):
            s.add(Notification(kind="race", chat_id=770101, text=f"гонка {i}",
                               status="pending", attempts=0,
                               created_at=utc_now(), next_attempt_at=utc_now()))
        await s.commit()
    b1, b2 = FakeBot(), FakeBot()
    await asyncio.gather(notify.flush(b1), notify.flush(b2))
    texts = [t for _, t in b1.sent] + [t for _, t in b2.sent]
    race = [t for t in texts if t.startswith("гонка")]
    assert len(race) == len(set(race)) == 6, sorted(race)
    OK(f"шесть сообщений, каждое ровно один раз ({len(b1.sent)} + {len(b2.sent)})")

    print("\nВСЁ ПРОШЛО")


asyncio.run(run())
