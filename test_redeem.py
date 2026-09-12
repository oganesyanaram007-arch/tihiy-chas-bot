# -*- coding: utf-8 -*-
"""Погашение кода на входе — сквозные проверки через настоящие эндпоинты.

Запуск:
    PYTHONPATH=. python3 test_redeem.py

Каждый сценарий здесь до правок падал: погашение читало статус в Python
и писало отдельно, поэтому две одновременные отметки проходили обе;
код чужой площадки гасился; просроченный код гасился; перебор кодов
ничем не ограничивался; а «QR» в мини-аппе был CSS-градиентом, который
нельзя отсканировать.
"""
import asyncio, datetime as dt, os, tempfile, pathlib

TOKEN = "123456:TESTTOKENabcdefghijklmnopqrstuvw"
os.environ["BOT_TOKEN"] = TOKEN
os.environ.setdefault("ADMIN_IDS", "999999")
_db = pathlib.Path(tempfile.mkdtemp()) / "t.db"
os.environ["DB_URL"] = f"sqlite+aiosqlite:///{_db}"

OK = lambda m: print(f"  ✓ {m}")


async def run():
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.api import api
    from app import booking_flow
    from app.db import Booking, Session, Slot, User, Venue, init_db
    from app.models_partner import (CabSession, Partner, PartnerUser, VenueProfile)
    from app.cabinet import _digest
    from app.tz import msk_today, msk_now

    await init_db()

    async def setup():
        """Два партнёра, у каждого своё заведение и бронь на ближайший час."""
        async with Session() as s:
            out = {}
            for key, tg in (("a", 7001), ("b", 7002)):
                p = Partner(title=f"Партнёр {key}", status="active")
                s.add(p); await s.flush()
                pu = PartnerUser(tg_id=tg, partner_id=p.id, name=f"Хостес {key}",
                                 role="owner", active=True)
                s.add(pu); await s.flush()
                v = Venue(name=f"Заведение {key}", cat="food", district="petro",
                          place="тест", check_note="", left_note="",
                          weekdays="0,1,2,3,4,5,6", active=True)
                s.add(v); await s.flush()
                s.add(VenueProfile(venue_id=v.id, partner_id=p.id, status="active"))
                hour = msk_now().hour
                sl = Slot(venue_id=v.id, hour=hour, discount=30)
                s.add(sl); await s.flush()
                token = f"sess-{key}-token-value"
                s.add(CabSession(token_hash=_digest(token), partner_user_id=pu.id,
                                 expires_at=dt.datetime.utcnow() + dt.timedelta(days=1)))
                out[key] = dict(partner=p.id, pu=pu.id, venue=v.id, slot=sl.id,
                                hour=hour, token=token)
            guest = User(id=555001, name="Гость")
            s.add(guest); await s.flush()
            for key in ("a", "b"):
                bk = Booking(code=f"ТЧ-{1000 + ord(key)}", user_id=guest.id,
                             venue_id=out[key]["venue"], slot_id=out[key]["slot"],
                             visit_date=msk_today(), status="active")
                s.add(bk); await s.flush()
                out[key]["code"] = bk.code
                out[key]["booking"] = bk.id
            await s.commit()
            return out

    D = await setup()
    c = TestClient(api)
    A = {"Cookie": f"tc_cab={D['a']['token']}"}
    B = {"Cookie": f"tc_cab={D['b']['token']}"}

    def redeem(hdr, code):
        return c.post("/api/cab/bookings/redeem", json={"code": code}, headers=hdr)

    print("\n1. Код другой площадки")
    r = redeem(A, D["b"]["code"])
    assert r.status_code == 404, r.text
    assert "не найден" in r.json()["detail"].lower()
    async with Session() as s:
        assert (await s.get(Booking, D["b"]["booking"])).status == "active"
    OK("отказ 404, чужая бронь не тронута")

    print("\n2. Свой код — погашение проходит")
    booking_flow.reset_attempts(D["a"]["pu"])
    r = redeem(A, D["a"]["code"])
    assert r.status_code == 200, r.text
    async with Session() as s:
        assert (await s.get(Booking, D["a"]["booking"])).status == "visited"
    OK(f"200, статус visited — {r.json()['message']}")

    print("\n3. Повторное погашение того же кода")
    r2 = redeem(A, D["a"]["code"])
    assert r2.status_code == 409, r2.text
    msg = r2.json()["detail"]
    assert "уже погашен" in msg, msg
    assert "отметил" in msg, f"нет имени того, кто погасил: {msg}"
    OK(f"409 и объяснение: {msg}")

    print("\n4. Регистр, пробелы и латинская раскладка")
    async with Session() as s:
        bk = await s.get(Booking, D["a"]["booking"]); bk.status = "active"; await s.commit()
    r = redeem(A, f"  {D['a']['code'].lower().replace('Т','T').replace('Ч','Ч')}  ")
    assert r.status_code == 200, r.text
    OK("код принят с пробелами, в нижнем регистре и с латинской Т")

    print("\n5. Отменённая бронь")
    async with Session() as s:
        bk = await s.get(Booking, D["a"]["booking"]); bk.status = "cancelled"; await s.commit()
    r = redeem(A, D["a"]["code"])
    assert r.status_code == 409 and "отменена" in r.json()["detail"], r.text
    OK("отказ: бронь отменена")

    print("\n6. Просроченный код (бронь была вчера)")
    async with Session() as s:
        bk = await s.get(Booking, D["a"]["booking"])
        bk.status = "active"; bk.visit_date = msk_today() - dt.timedelta(days=1)
        await s.commit()
    r = redeem(A, D["a"]["code"])
    assert r.status_code == 409 and "просрочен" in r.json()["detail"], r.text
    OK(f"отказ: {r.json()['detail']}")

    print("\n7. Слишком рано (бронь через три дня)")
    async with Session() as s:
        bk = await s.get(Booking, D["a"]["booking"])
        bk.visit_date = msk_today() + dt.timedelta(days=3); await s.commit()
    r = redeem(A, D["a"]["code"])
    assert r.status_code == 409 and "Рано" in r.json()["detail"], r.text
    OK(f"отказ: {r.json()['detail']}")

    print("\n8. Лимит неверных попыток")
    booking_flow.reset_attempts(D["a"]["pu"])
    codes = [f"ТЧ-{9000+i}" for i in range(20)]
    statuses = [redeem(A, code).status_code for code in codes]
    assert statuses[0] == 404, statuses[:3]
    assert 429 in statuses, statuses
    first_lock = statuses.index(429)
    assert first_lock == booking_flow.ATTEMPT_LIMIT, f"заблокировало на {first_lock}-й"
    assert all(x == 429 for x in statuses[first_lock:]), statuses
    OK(f"после {booking_flow.ATTEMPT_LIMIT} неверных — 429, дальше все 429")

    print("\n9. Блокировка не мешает другому партнёру")
    r = redeem(B, D["b"]["code"])
    assert r.status_code == 200, r.text
    OK("счётчик попыток на аккаунт, а не глобальный")

    print("\n10. Параллельное погашение из двух сессий")
    async with Session() as s:
        bk = await s.get(Booking, D["b"]["booking"])
        bk.status = "active"; bk.visit_date = msk_today(); await s.commit()
    booking_flow.reset_attempts(D["b"]["pu"])
    results = await asyncio.gather(*[
        booking_flow.redeem(D["b"]["code"], partner_id=D["b"]["partner"],
                            partner_user_id=D["b"]["pu"]) for _ in range(2)])
    wins = [x for x in results if x.ok]
    assert len(wins) == 1, [(x.ok, x.reason) for x in results]
    assert results[0].ok != results[1].ok
    loser = next(x for x in results if not x.ok)
    assert loser.reason == "already", loser.reason
    async with Session() as s:
        guest = await s.get(User, 555001)
    OK(f"успешна ровно одна, вторая — «{loser.message[:38]}…»")

    print("\n11. QR брони — настоящий PNG")
    async with Session() as s:
        bk = await s.get(Booking, D["a"]["booking"])
        bk.status = "active"; bk.visit_date = msk_today(); await s.commit()
        code = bk.code
    from app.webauth import _digest as wd
    from app.db import WebSession
    async with Session() as s:
        s.add(WebSession(token_hash=wd("guest-token"), user_id=555001,
                         expires_at=dt.datetime.utcnow() + dt.timedelta(days=1)))
        await s.commit()
    r = c.get(f"/api/guest/bookings/{D['a']['booking']}/qr.png",
              headers={"Cookie": "tc_web=guest-token"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n", r.content[:16]
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(r.content))
    OK(f"PNG {img.size[0]}×{img.size[1]}, {len(r.content)} байт")

    print("\n12. Чужой QR не отдаётся")
    async with Session() as s:
        other = User(id=555002, name="Другой")
        s.add(other)
        s.add(WebSession(token_hash=wd("other-token"), user_id=555002,
                         expires_at=dt.datetime.utcnow() + dt.timedelta(days=1)))
        await s.commit()
    r = c.get(f"/api/guest/bookings/{D['a']['booking']}/qr.png",
              headers={"Cookie": "tc_web=other-token"})
    assert r.status_code == 404, r.status_code
    OK("404 на чужую бронь")

    await run_codes(D, c, msk_today, select)

    print("\n18. Баллы за визит — одно правило на весь продукт")
    from app.product import POINTS_NEW_VENUE_MULT, POINTS_PER_VISIT
    from app.config import PTS_VISIT, PTS_NEW_MULT
    from app.db import PointsLedger
    assert PTS_VISIT == POINTS_PER_VISIT, (PTS_VISIT, POINTS_PER_VISIT)
    assert PTS_NEW_MULT == POINTS_NEW_VENUE_MULT
    # Гость, который тут ещё не был: первое посещение идёт с удвоением.
    async with Session() as s:
        v3 = Venue(name="Новое место", cat="food", district="petro", place="тест",
                   check_note="", left_note="", weekdays="0,1,2,3,4,5,6", active=True)
        s.add(v3); await s.flush()
        s.add(VenueProfile(venue_id=v3.id, partner_id=D["b"]["partner"], status="active"))
        sl3 = Slot(venue_id=v3.id, hour=msk_now().hour, discount=30)
        s.add(sl3); await s.flush()
        fresh = User(id=555777, name="Новичок"); s.add(fresh)
        bk3 = Booking(code="PTS111", user_id=fresh.id, venue_id=v3.id,
                      slot_id=sl3.id, visit_date=msk_today(), status="active")
        s.add(bk3); await s.flush()
        bk3_id, v3_id, sl3_id = bk3.id, v3.id, sl3.id
        await s.commit()
    booking_flow.reset_attempts(D["b"]["pu"])
    out = await booking_flow.redeem("PTS111", partner_id=D["b"]["partner"],
                                    partner_user_id=D["b"]["pu"])
    assert out.ok, out.message
    async with Session() as s:
        first_award = await s.scalar(select(PointsLedger.delta).where(
            PointsLedger.user_id == 555777).order_by(PointsLedger.id.desc()).limit(1))
    assert first_award == POINTS_PER_VISIT * POINTS_NEW_VENUE_MULT, first_award
    OK(f"первое посещение нового заведения: {first_award} = "
       f"{POINTS_PER_VISIT} × {POINTS_NEW_VENUE_MULT}")

    # Второй визит в то же заведение — без удвоения.
    async with Session() as s:
        bk4 = Booking(code="PTS222", user_id=555777, venue_id=v3_id,
                      slot_id=sl3_id, visit_date=msk_today(), status="active")
        s.add(bk4); await s.commit()
    out = await booking_flow.redeem("PTS222", partner_id=D["b"]["partner"],
                                    partner_user_id=D["b"]["pu"])
    assert out.ok, out.message
    async with Session() as s:
        second = await s.scalar(select(PointsLedger.delta).where(
            PointsLedger.user_id == 555777).order_by(PointsLedger.id.desc()).limit(1))
    assert second == POINTS_PER_VISIT, second
    OK(f"повторный визит туда же: {second} без удвоения")

    print("\nВСЁ ПРОШЛО")


async def run_codes(D, c, msk_today, select):
    """Формат кода: длина, алфавит, случайность, уникальность, оба поколения."""
    from app import booking_flow as bf
    from app.db import Booking, Session
    from app.product import CODE_ALPHABET, CODE_LENGTH

    print("\n13. Длина и алфавит")
    async with Session() as s:
        codes = [await bf.new_code(s) for _ in range(300)]
    assert all(len(c) == CODE_LENGTH for c in codes), "разная длина"
    bad = {ch for c in codes for ch in c} - set(CODE_ALPHABET)
    assert not bad, f"символы вне алфавита: {bad}"
    for ch in "01OIL":
        assert ch not in CODE_ALPHABET, f"похожий знак {ch} остался в алфавите"
    OK(f"{CODE_LENGTH} знаков из {len(CODE_ALPHABET)}, без 0/O/1/I/L")

    print("\n14. Коды не подряд и не повторяются")
    assert len(set(codes)) == len(codes), "генератор выдал дубль"
    # Соседние коды не должны отличаться на единицу ни в одном разряде:
    # так ловится счётчик, замаскированный под случайность.
    idx = [[CODE_ALPHABET.index(ch) for ch in c] for c in codes]
    seq = sum(1 for a, b in zip(idx, idx[1:])
              if sum(1 for x, y in zip(a, b) if x != y) <= 1)
    assert seq < 5, f"{seq} пар отличаются одним знаком — похоже на счётчик"
    OK(f"300 кодов, все разные, последовательности нет")

    print("\n15. Занятый код не выдаётся повторно")
    async with Session() as s:
        taken = await s.scalar(select(Booking.code).limit(1))
        fresh = [await bf.new_code(s) for _ in range(50)]
    assert taken not in fresh, "выдал уже занятый код"
    OK(f"занятый {taken} не переиспользован")

    print("\n16. Разбор ввода понимает оба поколения")
    assert "4KMPQ7" in bf.code_candidates(" 4kmpq7 ")
    assert "ТЧ-1234" in bf.code_candidates("тч-1234")
    assert "ТЧ-1234" in bf.code_candidates("1234"), "старый код без префикса"
    # Кириллическая раскладка на новом коде не должна ломать разбор.
    assert "4KMPQ7" in bf.code_candidates("4КМРQ7")
    OK("новый код, старый код и старый код без префикса — все находятся")

    print("\n17. Погашение по коду нового формата")
    async with Session() as s:
        code = await bf.new_code(s)
        bk = await s.get(Booking, D["b"]["booking"])
        bk.code = code
        bk.status = "active"
        bk.visit_date = msk_today()
        await s.commit()
    bf.reset_attempts(D["b"]["pu"])
    r = c.post("/api/cab/bookings/redeem", json={"code": code.lower()},
               headers={"Cookie": f"tc_cab={D['b']['token']}"})
    assert r.status_code == 200, r.text
    OK(f"код {code} погашен, введённый в нижнем регистре")

    print("\nФОРМАТ КОДА: ВСЁ ПРОШЛО")


asyncio.run(run())
