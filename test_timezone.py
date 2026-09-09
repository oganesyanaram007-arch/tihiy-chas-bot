# -*- coding: utf-8 -*-
"""Часовые пояса: одна бронь читается одинаково везде.

Запуск:
    PYTHONPATH=. python3 test_timezone.py

До правок это падало. Хранение было в UTC, а «сегодня» каждый модуль
считал сам: guest_api по Москве, кабинет и бот — по времени сервера.
Сервер живёт в UTC, поэтому с полуночи до трёх ночи по Москве гость
видел бронь сегодняшней, а кабинет партнёра — вчерашней, и календарь
бота предлагал вчерашний день.
"""
import asyncio, datetime as dt, os, pathlib, tempfile

TOKEN = "123456:TESTTOKENabcdefghijklmnopqrstuvw"
os.environ["BOT_TOKEN"] = TOKEN
os.environ.setdefault("ADMIN_IDS", "999999")
os.environ["DB_URL"] = f"sqlite+aiosqlite:///{pathlib.Path(tempfile.mkdtemp())/'t.db'}"

OK = lambda m: print(f"  ✓ {m}")


async def run():
    from fastapi.testclient import TestClient
    from app.api import api
    from app.cabinet import _digest
    from app.db import Booking, Session, Slot, User, Venue, WebSession, init_db
    from app.models_partner import CabSession, Partner, PartnerUser, VenueProfile
    from app.tz import MSK, fmt_slot, msk_today, to_msk, utc_now
    from app.webauth import _digest as wd
    from app import booking_flow

    await init_db()
    CAB, WEB = "tz-cab-token-000000", "tz-web-token-000000"

    async with Session() as s:
        p = Partner(title="Тест", status="active"); s.add(p); await s.flush()
        pu = PartnerUser(tg_id=810001, partner_id=p.id, name="Хостес",
                         role="owner", active=True); s.add(pu); await s.flush()
        v = Venue(name="Тбилисо", cat="food", district="petro", place="тест",
                  check_note="", left_note="", weekdays="0,1,2,3,4,5,6", active=True)
        s.add(v); await s.flush()
        s.add(VenueProfile(venue_id=v.id, partner_id=p.id, status="active"))
        sl = Slot(venue_id=v.id, hour=15, discount=35); s.add(sl); await s.flush()
        g = User(id=810900, name="Гость"); s.add(g)
        s.add(CabSession(token_hash=_digest(CAB), partner_user_id=pu.id,
                         expires_at=dt.datetime.utcnow() + dt.timedelta(days=1)))
        s.add(WebSession(token_hash=wd(WEB), user_id=g.id,
                         expires_at=dt.datetime.utcnow() + dt.timedelta(days=1)))
        bk = Booking(code="TZ4KMP", user_id=g.id, venue_id=v.id, slot_id=sl.id,
                     visit_date=msk_today(), status="active")
        s.add(bk); await s.flush()
        ids = dict(booking=bk.id, venue=v.id, slot=sl.id, pu=pu.id, partner=p.id)
        await s.commit()

    c = TestClient(api)
    GH = {"Cookie": f"tc_web={WEB}"}
    CH = {"Cookie": f"tc_cab={CAB}"}

    print("\n1. Одна бронь — одна строка времени в трёх интерфейсах")
    guest = c.get("/api/guest/bookings", headers=GH).json()["bookings"][0]
    cab = c.get("/api/cab/bookings/today", headers=CH).json()["bookings"][0]
    out = await booking_flow.redeem("", partner_id=ids["partner"],
                                    partner_user_id=ids["pu"],
                                    booking_id=ids["booking"], enforce_window=False)
    bot = out.booking
    assert guest["when"] == cab["when"] == bot["when"], \
        f"разошлись: гость={guest['when']!r} кабинет={cab['when']!r} бот={bot['when']!r}"
    OK(f"все трое печатают «{guest['when']}»")

    print("\n2. Строка собрана единой утилитой, а не совпала случайно")
    assert guest["when"] == fmt_slot(msk_today(), 15), guest["when"]
    OK(f"fmt_slot даёт ровно это: {fmt_slot(msk_today(), 15)}")

    print("\n3. Хранение в UTC, показ в Москве")
    async with Session() as s:
        row = await s.get(Booking, ids["booking"])
        stored = row.redeemed_at
    assert stored.tzinfo is None, "в базе должно лежать наивное UTC-время"
    delta = abs((dt.datetime.utcnow() - stored).total_seconds())
    assert delta < 120, f"записано не UTC: разница {delta:.0f} с"
    shown = to_msk(stored)
    assert shown.utcoffset() == dt.timedelta(hours=3), shown.utcoffset()
    OK(f"в базе {stored:%H:%M} UTC → на экране {shown:%H:%M} МСК")

    print("\n4. Полночь по Москве не сдвигает «сегодня»")
    # 21:30 UTC — это уже следующий день в Москве. Раньше кабинет считал
    # «сегодня» по серверу и в этот час терял сегодняшние брони.
    utc_late = dt.datetime(2026, 6, 10, 21, 30, tzinfo=dt.timezone.utc)
    assert utc_late.date() == dt.date(2026, 6, 10)
    assert utc_late.astimezone(MSK).date() == dt.date(2026, 6, 11)
    OK("в 21:30 UTC сервер сказал бы 10 июня, Москва — 11 июня")

    print("\n5. Календарь бота считает дни по Москве")
    from app.keyboards import date_for
    assert date_for(0) == msk_today(), f"{date_for(0)} != {msk_today()}"
    OK(f"date_for(0) = {date_for(0)} = сегодня по Москве")

    print("\nВСЁ ПРОШЛО")


asyncio.run(run())
