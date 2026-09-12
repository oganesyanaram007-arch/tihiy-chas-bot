# -*- coding: utf-8 -*-
"""Гонка за последний слот: два устройства жмут «забронировать» разом.

Запуск:
    PYTHONPATH=. python3 test_slot_race.py

До правок лимит мест проверялся чтением, а запись шла отдельно — между
ними оставалась щель. Внутрипроцессная блокировка закрывала её только
внутри одного процесса, а брони создают два: API и бот. Причём бот лимит
не проверял вовсе, и гость из Telegram занимал стол сверх ёмкости —
заведение узнавало об этом уже на входе.
"""
import asyncio, datetime as dt, os, pathlib, tempfile

os.environ["BOT_TOKEN"] = "123456:TESTTOKENabcdefghijklmnopqrstuvw"
os.environ.setdefault("ADMIN_IDS", "999999")
os.environ["DB_URL"] = f"sqlite+aiosqlite:///{pathlib.Path(tempfile.mkdtemp())/'t.db'}"

OK = lambda m: print(f"  ✓ {m}")


async def run():
    from sqlalchemy import func, select
    from app.db import Booking, Session, Slot, User, Venue, init_db
    from app.models_partner import Partner, PartnerUser, QuietWindow, VenueProfile
    from app.tz import msk_today
    from app import booking_flow

    await init_db()
    day = msk_today() + dt.timedelta(days=1)

    async def fresh_venue(capacity: int | None):
        """Заведение с одним окном. capacity=None — партнёр лимит не задал."""
        async with Session() as s:
            p = Partner(title="Гонка", status="active"); s.add(p); await s.flush()
            s.add(PartnerUser(tg_id=830000 + p.id, partner_id=p.id,
                              name="Хозяин", role="owner", active=True))
            v = Venue(name=f"Место {p.id}", cat="food", district="petro",
                      place="тест", check_note="", left_note="",
                      weekdays="0,1,2,3,4,5,6", active=True)
            s.add(v); await s.flush()
            s.add(VenueProfile(venue_id=v.id, partner_id=p.id, status="active"))
            sl = Slot(venue_id=v.id, hour=15, discount=30); s.add(sl); await s.flush()
            if capacity is not None:
                s.add(QuietWindow(venue_id=v.id, weekday=day.weekday(), hour=15,
                                  discount=30, capacity=capacity, active=True))
            await s.commit()
            return v.id, sl.id

    async def guests(n: int, base: int):
        async with Session() as s:
            for i in range(n):
                s.add(User(id=base + i, name=f"Гость {i}"))
            await s.commit()
        return [base + i for i in range(n)]

    async def taken(venue_id, slot_id):
        async with Session() as s:
            return await s.scalar(select(func.count(Booking.id)).where(
                Booking.venue_id == venue_id, Booking.slot_id == slot_id,
                Booking.visit_date == day, Booking.status == "active"))

    print("\n1. Последнее место, два устройства одновременно")
    vid, sid = await fresh_venue(capacity=1)
    two = await guests(2, 840100)
    res = await asyncio.gather(*[
        booking_flow.create(u, vid, sid, day, 15) for u in two])
    won = [r for r in res if r.ok]
    lost = [r for r in res if not r.ok]
    assert len(won) == 1, [(r.ok, r.reason) for r in res]
    assert lost[0].reason == "no_seats", lost[0].reason
    assert await taken(vid, sid) == 1, await taken(vid, sid)
    OK(f"подтверждена ровно одна, второй — «{lost[0].message}»")

    print("\n2. Восемь устройств на четыре места")
    vid, sid = await fresh_venue(capacity=4)
    many = await guests(8, 840200)
    res = await asyncio.gather(*[
        booking_flow.create(u, vid, sid, day, 15) for u in many])
    assert sum(1 for r in res if r.ok) == 4, [r.ok for r in res]
    assert await taken(vid, sid) == 4
    OK("прошли ровно четыре, ни одной лишней")

    print("\n3. Ёмкость по умолчанию, когда партнёр лимит не задал")
    vid, sid = await fresh_venue(capacity=None)
    many = await guests(10, 840300)
    res = await asyncio.gather(*[
        booking_flow.create(u, vid, sid, day, 15) for u in many])
    got = sum(1 for r in res if r.ok)
    assert got == booking_flow.DEFAULT_CAPACITY, got
    OK(f"прошли {got} — ровно столько, сколько мест по умолчанию")

    print("\n4. Один гость не занимает два места в том же окне")
    vid, sid = await fresh_venue(capacity=5)
    one = (await guests(1, 840400))[0]
    res = await asyncio.gather(*[
        booking_flow.create(one, vid, sid, day, 15) for _ in range(3)])
    assert sum(1 for r in res if r.ok) == 1, [r.reason for r in res]
    assert {r.reason for r in res if not r.ok} == {"duplicate"}, \
        [r.reason for r in res if not r.ok]
    assert await taken(vid, sid) == 1
    OK("одна бронь, остальные — «вы уже забронировали это окно»")

    print("\n5. Отменённая бронь освобождает место")
    vid, sid = await fresh_venue(capacity=1)
    two = await guests(2, 840500)
    first = await booking_flow.create(two[0], vid, sid, day, 15)
    assert first.ok
    blocked = await booking_flow.create(two[1], vid, sid, day, 15)
    assert not blocked.ok and blocked.reason == "no_seats"
    await booking_flow.cancel(first.booking["id"], guest_id=two[0])
    second = await booking_flow.create(two[1], vid, sid, day, 15)
    assert second.ok, second.message
    assert await taken(vid, sid) == 1
    OK("после отмены место снова свободно, но всего одно")

    print("\n6. Соседнее окно не страдает от занятого")
    async with Session() as s:
        sl2 = Slot(venue_id=vid, hour=16, discount=30); s.add(sl2); await s.flush()
        s.add(QuietWindow(venue_id=vid, weekday=day.weekday(), hour=16,
                          discount=30, capacity=1, active=True))
        sid2 = sl2.id
        await s.commit()
    other = await booking_flow.create(two[0], vid, sid2, day, 16)
    assert other.ok, other.message
    OK("лимит считается по окну, а не по заведению целиком")

    print("\nВСЁ ПРОШЛО")


asyncio.run(run())
