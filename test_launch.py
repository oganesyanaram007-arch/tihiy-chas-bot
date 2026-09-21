# -*- coding: utf-8 -*-
"""Генеральная проверка перед запуском: весь путь гостя и сотрудника.

Прогоняет то, что произойдёт утром на самом деле, через настоящие
эндпоинты: регистрация гостя → бронь → код и QR → экран смены у партнёра
→ погашение → повтор → отмена → деградированный режим.
"""
import asyncio, datetime as dt, os, pathlib, tempfile

os.environ["BOT_TOKEN"] = "123456:TESTTOKENabcdefghijklmnopqrstuvw"
os.environ.setdefault("ADMIN_IDS", "999999")
os.environ["DB_URL"] = f"sqlite+aiosqlite:///{pathlib.Path(tempfile.mkdtemp())/'launch.db'}"

OK = lambda m: print(f"  ✓ {m}")
BAD = []


def check(cond, msg):
    if cond:
        OK(msg)
    else:
        BAD.append(msg)
        print(f"  ✗ {msg}")


async def run():
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.api import api
    from app import booking_flow
    from app.db import Booking, BookingEvent, Session, Slot, User, Venue, init_db
    from app.models_partner import (CabSession, ManualReview, Partner,
                                    PartnerUser, VenueProfile)
    from app.cabinet import _digest
    from app.tz import msk_now, msk_today

    await init_db()
    CAB = "launch-cab-token-000000"

    async with Session() as s:
        p = Partner(title="Тбилисо", status="active"); s.add(p); await s.flush()
        pu = PartnerUser(tg_id=910001, partner_id=p.id, name="Марина",
                         role="owner", active=True); s.add(pu); await s.flush()
        v = Venue(name="Тбилисо", cat="food", district="petro", place="грузинская",
                  check_note="чек ~1 600 ₽", left_note="", weekdays="0,1,2,3,4,5,6",
                  active=True); s.add(v); await s.flush()
        s.add(VenueProfile(venue_id=v.id, partner_id=p.id, status="active"))
        now = msk_now()
        # Окно для создания брони — на завтра: большинство броней такие,
        # и проверка не зависит от того, в котором часу её запускают.
        future_h = 15
        sl = Slot(venue_id=v.id, hour=future_h, discount=35); s.add(sl); await s.flush()
        # Текущее окно — для погашения кода: так и бывает в жизни, гость
        # забронировал заранее и пришёл внутрь окна.
        sl_now = Slot(venue_id=v.id, hour=now.hour, discount=30)
        s.add(sl_now); await s.flush()
        s.add(CabSession(token_hash=_digest(CAB), partner_user_id=pu.id,
                         expires_at=dt.datetime.utcnow() + dt.timedelta(days=1)))
        ids = dict(venue=v.id, slot=sl.id, hour=future_h, partner=p.id, pu=pu.id,
                   slot_now=sl_now.id, hour_now=now.hour)
        await s.commit()

    c = TestClient(api)
    CH = {"Cookie": f"tc_cab={CAB}"}

    # ---------------------------------------------------------------
    print("\n1. Гость регистрируется на сайте")
    r = c.post("/api/auth/register", json={
        "email": "launch.guest@test.ru", "password": "launch12345",
        "name": "Аркадий", "phone": "+7 921 000-11-22",
        "role": "guest", "consent": True})
    check(r.status_code == 200, f"регистрация принята ({r.status_code})")
    check(r.json()["user"]["role"] == "guest", "роль guest выдана сервером")
    guest_cookie = {"Cookie": f"tc_web={c.cookies.get('tc_web')}"}

    print("\n2. Гость пытается зарегистрироваться как партнёр тем же адресом")
    r = c.post("/api/auth/register", json={
        "email": "launch.guest@test.ru", "password": "other12345",
        "role": "partner", "consent": True})
    check(r.status_code == 409, f"дубль почты отклонён ({r.status_code})")

    # ---------------------------------------------------------------
    print("\n3. Гость бронирует тихое окно на завтра")
    tomorrow = (msk_today() + dt.timedelta(days=1)).isoformat()
    r = c.post("/api/guest/bookings",
               json={"venue_id": ids["venue"], "hour": ids["hour"], "date": tomorrow},
               headers=guest_cookie)
    check(r.status_code == 200, f"бронь создана ({r.status_code}) {r.text[:80]}")
    data = r.json()
    code, bk_id = data["code"], data["id"]
    from app.product import CODE_ALPHABET, CODE_LENGTH
    check(len(code) == CODE_LENGTH, f"код длиной {CODE_LENGTH}: {code}")
    check(all(ch in CODE_ALPHABET for ch in code), "код только из разрешённого алфавита")
    check(not set(code) & set("01OIL"), "в коде нет похожих знаков 0 O 1 I L")

    print("\n4. Повторная бронь того же окна тем же гостем")
    r2 = c.post("/api/guest/bookings",
                json={"venue_id": ids["venue"], "hour": ids["hour"], "date": tomorrow},
                headers=guest_cookie)
    check(r2.status_code == 409, f"дубль отклонён ({r2.status_code})")

    print("\n5. Гость видит бронь у себя")
    r = c.get("/api/guest/bookings", headers=guest_cookie)
    mine = r.json()["bookings"]
    check(len(mine) == 1 and mine[0]["code"] == code, "бронь видна в кабинете гостя")
    check(bool(mine[0].get("when")), f"время подписано: {mine[0].get('when')}")
    guest_when = mine[0]["when"]

    print("\n6. QR брони")
    r = c.get(f"/api/guest/bookings/{bk_id}/qr.png", headers=guest_cookie)
    check(r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n",
          f"настоящий PNG, {len(r.content)} байт")

    # ---------------------------------------------------------------
    print("\n6b. Бронь на текущее окно (гость забронировал заранее)")
    async with Session() as s:
        guest_row = await s.scalar(select(User).where(
            User.email == "launch.guest@test.ru"))
        live_code = await booking_flow.new_code(s)
        live = Booking(code=live_code, user_id=guest_row.id, venue_id=ids["venue"],
                       slot_id=ids["slot_now"], visit_date=msk_today(),
                       status="active")
        s.add(live); await s.flush()
        live_id = live.id
        await s.commit()
    OK(f"код {live_code} на окно {ids['hour_now']}:00")

    print("\n7. Партнёр видит гостя на экране смены")
    r = c.get("/api/cab/bookings/today", headers=CH)
    shift = r.json()
    check(r.status_code == 200, f"экран смены отвечает ({r.status_code})")
    check(shift["waiting"] == 1 and shift["arrived"] == 0,
          f"ждём 1, пришло 0 (получено {shift['waiting']}/{shift['arrived']})")
    codes_on_screen = {b["code"] for b in shift["bookings"]}
    check(live_code in codes_on_screen, "сегодняшний гость на экране смены")
    check(code not in codes_on_screen,
          "завтрашняя бронь на сегодняшний экран не лезет")
    row = next(b for b in shift["bookings"] if b["code"] == live_code)
    check(bool(row["when"]), f"время подписано: {row['when']}")

    print("\n8. Сотрудник гасит код")
    r = c.post("/api/cab/bookings/redeem", json={"code": live_code.lower()}, headers=CH)
    check(r.status_code == 200, f"погашение прошло ({r.status_code}) {r.text[:90]}")

    print("\n9. Повторное погашение того же кода")
    r = c.post("/api/cab/bookings/redeem", json={"code": live_code}, headers=CH)
    msg = r.json().get("detail", "")
    check(r.status_code == 409, f"отказ ({r.status_code})")
    check("уже погашен" in msg and "Марина" in msg, f"сказано кто и когда: {msg}")

    print("\n10. Экран смены обновился")
    shift = c.get("/api/cab/bookings/today", headers=CH).json()
    check(shift["waiting"] == 0 and shift["arrived"] == 1,
          f"ждём 0, пришло 1 (получено {shift['waiting']}/{shift['arrived']})")

    print("\n11. Журнал событий записал переход")
    async with Session() as s:
        ev = (await s.scalars(select(BookingEvent).where(
            BookingEvent.booking_id == live_id))).all()
    redeem_ev = [e for e in ev if e.action == "redeem"]
    check(len(redeem_ev) == 1, f"ровно одна запись о погашении (всего событий {len(ev)})")
    check(redeem_ev[0].status_from == "active" and redeem_ev[0].status_to == "visited",
          "зафиксирован прежний и новый статус")
    check(bool(redeem_ev[0].actor_name), f"актор записан: {redeem_ev[0].actor_name}")

    print("\n12. Баллы начислены один раз")
    async with Session() as s:
        from app.db import PointsLedger
        rows = (await s.scalars(select(PointsLedger))).all()
    visits = [x for x in rows if x.reason.startswith("Визит")]
    from app.product import POINTS_NEW_VENUE_MULT, POINTS_PER_VISIT
    check(len(visits) == 1, f"одно начисление за визит (всего записей {len(rows)})")
    check(visits[0].delta == POINTS_PER_VISIT * POINTS_NEW_VENUE_MULT,
          f"{visits[0].delta} = {POINTS_PER_VISIT} × {POINTS_NEW_VENUE_MULT} за новое заведение")

    # ---------------------------------------------------------------
    print("\n13. Деградированный режим: код не проходит")
    r = c.post("/api/cab/bookings/manual",
               json={"code": "ZZZZZZ", "guest_hint": "Пара, столик у окна"},
               headers=CH)
    check(r.status_code == 200, f"кнопка сработала ({r.status_code})")
    check("можно впустить" in r.json().get("message", ""),
          f"сотруднику сказано впустить: {r.json().get('message','')}")
    r = c.get("/api/cab/manual-reviews", headers=CH)
    check(r.json()["open_count"] == 1, "случай попал в разбор")

    print("\n14. Отмена брони гостем")
    r = c.post("/api/guest/bookings",
               json={"venue_id": ids["venue"], "hour": ids["hour"],
                     "date": (msk_today() + dt.timedelta(days=2)).isoformat()},
               headers=guest_cookie)
    check(r.status_code == 200, f"бронь на послезавтра создана ({r.status_code}) {r.text[:70]}")
    bk2 = r.json()["id"]
    r = c.post("/api/guest/bookings/cancel", json={"booking_id": bk2},
               headers=guest_cookie)
    check(r.status_code == 200, f"отмена прошла ({r.status_code})")
    r = c.post("/api/guest/bookings/cancel", json={"booking_id": bk2},
               headers=guest_cookie)
    check(r.status_code == 409, f"повторная отмена отклонена ({r.status_code})")

    print("\n15. Чужую бронь отменить нельзя")
    c2 = TestClient(api)
    reg = c2.post("/api/auth/register", json={
        "email": "stranger@test.ru", "password": "stranger12345",
        "role": "guest", "consent": True})
    # Куки сессии помечены Secure, и по http тестовый клиент их не хранит —
    # на проде сайт под HTTPS, поэтому берём токен из заголовка вручную.
    import re as _re
    tok = _re.search(r"tc_web=([^;]+)", reg.headers.get("set-cookie", "")).group(1)
    other = {"Cookie": f"tc_web={tok}"}
    r = c2.post("/api/guest/bookings/cancel", json={"booking_id": bk_id}, headers=other)
    check(r.status_code == 404, f"чужая бронь не найдена для постороннего ({r.status_code})")

    print("\n16. Чужой QR не отдаётся")
    r = c2.get(f"/api/guest/bookings/{bk_id}/qr.png", headers=other)
    check(r.status_code == 404, f"отказ ({r.status_code})")

    print("\n16b. Посторонний не гасит код чужого заведения")
    r = c2.post("/api/cab/bookings/redeem", json={"code": code}, headers=other)
    check(r.status_code in (401, 403), f"в кабинет не пускают ({r.status_code})")

    print("\n17. Роль нельзя поднять через вход")
    r = c.post("/api/auth/login", json={"email": "launch.guest@test.ru",
                                        "password": "launch12345",
                                        "role": "partner"})
    check(r.status_code == 200 and r.json()["user"]["role"] == "guest",
          "сервер вернул guest, несмотря на role=partner в теле")
    r = c.get("/api/cab/bookings/today", headers={"Cookie": f"tc_web={c.cookies.get('tc_web')}"})
    check(r.status_code in (401, 403), f"в кабинет заведения гостя не пускают ({r.status_code})")

    print("\n" + "─" * 54)
    if BAD:
        print(f"ПРОВАЛЕНО: {len(BAD)}")
        for b in BAD:
            print("  ✗", b)
        raise SystemExit(1)
    print("ВСЁ ПРОШЛО")


asyncio.run(run())
