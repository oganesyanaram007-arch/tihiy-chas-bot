# -*- coding: utf-8 -*-
"""Деградированный режим: код не проходит, а гость уже на входе.

Запуск:
    PYTHONPATH=. python3 test_degraded.py

До правок такого пути не было вовсе: если код не принимался, сотруднику
оставалось либо не впустить гостя, либо впустить и ничего не записать.
Первое бьёт по гостю, который уже пришёл; второе делает спор неразрешимым.
"""
import asyncio, datetime as dt, os, pathlib, tempfile

TOKEN = "123456:TESTTOKENabcdefghijklmnopqrstuvw"
os.environ["BOT_TOKEN"] = TOKEN
os.environ.setdefault("ADMIN_IDS", "999999")
os.environ["DB_URL"] = f"sqlite+aiosqlite:///{pathlib.Path(tempfile.mkdtemp())/'t.db'}"

OK = lambda m: print(f"  ✓ {m}")


async def run():
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.api import api
    from app.cabinet import _digest
    from app.db import Booking, BookingEvent, Session, Slot, User, Venue, init_db
    from app.models_partner import (CabSession, ManualReview, Partner,
                                    PartnerUser, VenueProfile)
    from app.tz import msk_today
    from app import booking_flow

    await init_db()
    OWNER, STAFF = "dg-owner-token-00000", "dg-staff-token-00000"

    async with Session() as s:
        p = Partner(title="Тест", status="active"); s.add(p); await s.flush()
        own = PartnerUser(tg_id=820001, partner_id=p.id, name="Владелец",
                          role="owner", active=True)
        stf = PartnerUser(tg_id=820002, partner_id=p.id, name="Хостес Оля",
                          role="staff", active=True)
        s.add_all([own, stf]); await s.flush()
        v = Venue(name="Тбилисо", cat="food", district="petro", place="тест",
                  check_note="", left_note="", weekdays="0,1,2,3,4,5,6", active=True)
        s.add(v); await s.flush()
        s.add(VenueProfile(venue_id=v.id, partner_id=p.id, status="active"))
        sl = Slot(venue_id=v.id, hour=15, discount=35); s.add(sl); await s.flush()
        g = User(id=820900, name="Гость"); s.add(g)
        # Бронь на вчера: код заведомо просрочен — типичный повод для разбора.
        bk = Booking(code="DG4KMP", user_id=g.id, venue_id=v.id, slot_id=sl.id,
                     visit_date=msk_today() - dt.timedelta(days=1), status="active")
        s.add(bk); await s.flush()
        s.add(CabSession(token_hash=_digest(OWNER), partner_user_id=own.id,
                         expires_at=dt.datetime.utcnow() + dt.timedelta(days=1)))
        s.add(CabSession(token_hash=_digest(STAFF), partner_user_id=stf.id,
                         expires_at=dt.datetime.utcnow() + dt.timedelta(days=1)))
        ids = dict(booking=bk.id, partner=p.id, owner=own.id, staff=stf.id)
        await s.commit()

    c = TestClient(api)
    O = {"Cookie": f"tc_cab={OWNER}"}
    S = {"Cookie": f"tc_cab={STAFF}"}

    print("\n1. Код просрочен — обычное погашение отказывает")
    r = c.post("/api/cab/bookings/redeem", json={"code": "DG4KMP"}, headers=S)
    assert r.status_code == 409 and "просрочен" in r.json()["detail"], r.text
    OK(f"отказ: {r.json()['detail']}")

    print("\n2. «Код не проходит» — гостя впускаем, случай записан")
    r = c.post("/api/cab/bookings/manual",
               json={"code": "DG4KMP", "guest_hint": "Аркадий, столик у окна"},
               headers=S)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "можно впустить" in body["message"], body["message"]
    assert body["booking_id"] == ids["booking"], "бронь должна была найтись"
    OK(f"{body['message']}")

    print("\n3. Ручной разбор ничего не меняет в самой брони")
    async with Session() as s:
        row = await s.get(Booking, ids["booking"])
        assert row.status == "active", row.status
        assert row.redeemed_at is None
    OK("статус остался active — это не тихое погашение за спиной")

    print("\n4. След остался и в журнале брони")
    async with Session() as s:
        ev = await s.scalar(select(BookingEvent).where(
            BookingEvent.booking_id == ids["booking"],
            BookingEvent.action == "manual_admit"))
    assert ev is not None, "события manual_admit нет"
    assert ev.actor_name == "Хостес Оля", ev.actor_name
    OK(f"событие записано, актор — {ev.actor_name}")

    print("\n5. Случай виден на экране смены")
    shift = c.get("/api/cab/bookings/today", headers=S).json()
    assert shift["manual_open"] == 1, shift
    OK("экран смены показывает 1 открытый случай")

    print("\n6. Код вообще не назван — всё равно впускаем")
    r = c.post("/api/cab/bookings/manual",
               json={"code": "", "guest_hint": "телефон сел, имени не помнит"},
               headers=S)
    assert r.status_code == 200, r.text
    assert r.json()["booking_id"] is None
    OK("случай без кода тоже принят — гость не остаётся у двери")

    print("\n7. Лимит неверных попыток не блокирует ручной разбор")
    booking_flow.reset_attempts(ids["staff"])
    for i in range(booking_flow.ATTEMPT_LIMIT + 2):
        c.post("/api/cab/bookings/redeem", json={"code": f"ZZZZ{i:02d}"}, headers=S)
    blocked = c.post("/api/cab/bookings/redeem", json={"code": "DG4KMP"}, headers=S)
    assert blocked.status_code == 429, blocked.status_code
    r = c.post("/api/cab/bookings/manual",
               json={"code": "DG4KMP", "guest_hint": "после блокировки"}, headers=S)
    assert r.status_code == 200, r.text
    OK("ввод заблокирован, а впустить гостя по-прежнему можно")

    print("\n8. Разбирает владелец, не хостес")
    reviews = c.get("/api/cab/manual-reviews", headers=O).json()
    assert reviews["open_count"] == 3, reviews["open_count"]
    first = reviews["reviews"][0]["id"]
    denied = c.post("/api/cab/manual-reviews/resolve",
                    json={"review_id": first, "note": "ок"}, headers=S)
    assert denied.status_code == 403, denied.status_code
    done = c.post("/api/cab/manual-reviews/resolve",
                  json={"review_id": first, "note": "гость был, всё верно"}, headers=O)
    assert done.status_code == 200, done.text
    again = c.post("/api/cab/manual-reviews/resolve",
                   json={"review_id": first, "note": "ещё раз"}, headers=O)
    assert again.status_code == 409, again.status_code
    OK("хостес получает 403, владелец закрывает, повтор — 409")

    print("\n9. Чужой партнёр случаев не видит")
    async with Session() as s:
        p2 = Partner(title="Чужие", status="active"); s.add(p2); await s.flush()
        pu2 = PartnerUser(tg_id=820777, partner_id=p2.id, name="Чужой",
                          role="owner", active=True); s.add(pu2); await s.flush()
        s.add(CabSession(token_hash=_digest("dg-other-token-0000"),
                         partner_user_id=pu2.id,
                         expires_at=dt.datetime.utcnow() + dt.timedelta(days=1)))
        await s.commit()
    other = c.get("/api/cab/manual-reviews",
                  headers={"Cookie": "tc_cab=dg-other-token-0000"}).json()
    assert other["open_count"] == 0, other
    OK("у чужого партнёра пусто")

    print("\nВСЁ ПРОШЛО")


asyncio.run(run())
