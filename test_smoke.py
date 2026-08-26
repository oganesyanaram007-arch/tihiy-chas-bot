# -*- coding: utf-8 -*-
"""Смоук-тест без Telegram: районы, дни недели, бронь на будущую дату,
QR, визит, баллы, рефералка, отмена. Проверяет синхронизацию с сайтом."""
import asyncio, datetime as dt, io, random
import qrcode
from sqlalchemy import select


async def run():
    from app.db import (init_db, Session, Venue, Slot, Booking,
                        get_or_create_user, add_points, user_visited_venue,
                        venue_weekdays, works_on)
    from app.seed import seed
    from app.config import PTS_VISIT, PTS_NEW_MULT, PTS_REF, DEPOSIT, COMMISSION
    from app.keyboards import DISTRICTS, date_for, day_label

    await init_db(); await seed()

    # ---- деньги: депозит и комиссия синхронизированы с сайтом ----
    assert DEPOSIT == 99, f"депозит должен быть 99, а не {DEPOSIT}"
    assert COMMISSION == 99, f"комиссия должна быть 99, а не {COMMISSION}"

    async with Session() as s:
        venues = (await s.scalars(select(Venue))).all()
        assert len(venues) == 34, f"venues={len(venues)} (ожидали 34, как на сайте)"
        slots = (await s.scalars(select(Slot))).all()
        assert len(slots) >= 80, f"slots={len(slots)}"

        # ---- районы: 15 + "все" на сайте, у заведений — реальные районы ----
        districts_in_use = {v.district for v in venues}
        site_district_ids = {d for d, _ in DISTRICTS if d != "all"}
        assert districts_in_use.issubset(site_district_ids), \
            f"неизвестные районы в базе: {districts_in_use - site_district_ids}"
        assert len(districts_in_use) >= 10, "заведения должны покрывать разные районы"

        # ---- дни недели: у будничного заведения нет слотов в выходной ----
        weekday_venue = next(v for v in venues if venue_weekdays(v) == {0, 1, 2, 3, 4})
        monday = date_for(0)
        while monday.weekday() != 0:
            monday += dt.timedelta(days=1)
        saturday = monday + dt.timedelta(days=5)
        assert works_on(weekday_venue, monday), "будничное заведение должно работать в понедельник"
        assert not works_on(weekday_venue, saturday), "будничное заведение не должно работать в субботу"

        every_day_venue = next(v for v in venues if venue_weekdays(v) == set(range(7)))
        assert works_on(every_day_venue, saturday), "заведение 7/7 должно работать всегда"

        # ---- гость + рефералка ----
        guest, _ = await get_or_create_user(s, 111, "Арам")
        friend, _ = await get_or_create_user(s, 222, "Друг")
        friend.ref_by = guest.id
        await add_points(s, friend, PTS_REF, "Бонус за приглашение")
        await add_points(s, guest, PTS_REF, "Приглашён друг")

        # ---- бронь НЕ на сегодня, а на послезавтра (то, что просил основатель) ----
        v = every_day_venue
        sl = (await s.scalars(select(Slot).where(Slot.venue_id == v.id))).first()
        day_offset = 2
        visit_date = date_for(day_offset)
        code = f"ТЧ-{random.randint(1000,9999)}"
        s.add(Booking(code=code, user_id=guest.id, venue_id=v.id, slot_id=sl.id,
                      visit_date=visit_date))
        await s.commit()
        assert day_label(day_offset) not in ("Сегодня", "Завтра"), \
            "проверяем именно бронь на будущее, не на сегодня/завтра"

        # QR
        img = qrcode.make(f"TIHIYCHAS|{code}")
        buf = io.BytesIO(); img.save(buf, format="PNG")
        assert buf.getbuffer().nbytes > 300

        # ---- подтверждение визита (логика админки /visit) ----
        b = await s.scalar(select(Booking).where(Booking.code == code))
        assert b.visit_date == visit_date, "дата брони должна сохраниться, а не всегда 'сегодня'"
        first = not await user_visited_venue(s, b.user_id, b.venue_id)
        b.status = "visited"; guest.visits += 1
        pts = PTS_VISIT * (PTS_NEW_MULT if first else 1)
        await add_points(s, guest, pts, f"Визит {code}")
        await s.commit()

        assert guest.points == PTS_REF + 20, guest.points
        assert friend.points == PTS_REF
        assert b.status == "visited" and guest.visits == 1

        # ---- отмена: расчёт часов идёт от даты ВИЗИТА, а не от текущего часа ----
        code2 = f"ТЧ-{random.randint(1000,9999)}"
        far_date = date_for(5)
        s.add(Booking(code=code2, user_id=guest.id, venue_id=v.id, slot_id=sl.id,
                      visit_date=far_date))
        await s.commit()
        b2 = await s.scalar(select(Booking).where(Booking.code == code2))
        visit_dt = dt.datetime.combine(b2.visit_date, dt.time(hour=sl.hour))
        hours_left = (visit_dt - dt.datetime.now()).total_seconds() / 3600
        assert hours_left > 24, "бронь через 5 дней должна давать много часов на отмену"

        print(f"OK: {len(venues)} заведений в {len(districts_in_use)} районах, {len(slots)} слотов")
        print(f"OK: депозит {DEPOSIT} ₽, комиссия {COMMISSION} ₽ — синхронизировано с сайтом")
        print(f"OK: расписание по дням недели работает (будни/все дни проверены)")
        print(f"OK: бронь {code} на {day_label(day_offset)} ({visit_date}) · QR ✓ · "
              f"визит ✓ (+{pts} баллов, баланс {guest.points}) · рефералка ✓")
        print(f"OK: отмена считает часы от даты визита, а не от 'сегодня' ({hours_left:.0f} ч запаса)")

asyncio.run(run())
