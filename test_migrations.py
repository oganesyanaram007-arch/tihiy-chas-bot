# -*- coding: utf-8 -*-
"""Миграции на базе со старой схемой — то есть в ситуации прода.

Запуск:
    PYTHONPATH=. python3 test_migrations.py

Проверяем главное: живая база, созданная до появления новых колонок,
доводится до текущей модели, и данные при этом остаются на месте.
До появления app/migrations.py этого не происходило вовсе — create_all()
умеет только создавать таблицы целиком и новую колонку не добавляет.
"""
import asyncio, datetime as dt, os, pathlib, sqlite3, tempfile

os.environ["BOT_TOKEN"] = "123456:TESTTOKENabcdefghijklmnopqrstuvw"
os.environ.setdefault("ADMIN_IDS", "999999")
DB = pathlib.Path(tempfile.mkdtemp()) / "old.db"
os.environ["DB_URL"] = f"sqlite+aiosqlite:///{DB}"

OK = lambda m: print(f"  ✓ {m}")

# Схема ровно та, что была на проде до этих правок: без redeemed_at,
# redeemed_by, cancelled_at и без таблицы booking_events.
OLD_SCHEMA = """
CREATE TABLE users (
    id BIGINT NOT NULL PRIMARY KEY, name VARCHAR(128), points INTEGER,
    visits INTEGER, ref_by BIGINT, created_at DATETIME, email VARCHAR(160),
    password_hash VARCHAR(200), phone VARCHAR(32), role VARCHAR(12),
    consent_at DATETIME, consent_ip VARCHAR(64));
CREATE TABLE venues (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, name VARCHAR(64),
    cat VARCHAR(16), district VARCHAR(16), place VARCHAR(128),
    check_note VARCHAR(64), left_note VARCHAR(64), weekdays VARCHAR(16),
    active BOOLEAN);
CREATE TABLE slots (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, venue_id INTEGER,
    hour INTEGER, discount INTEGER);
CREATE TABLE bookings (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, code VARCHAR(12) UNIQUE,
    user_id BIGINT, venue_id INTEGER, slot_id INTEGER, visit_date DATE,
    status VARCHAR(12), created_at DATETIME);
"""


def build_old_db():
    c = sqlite3.connect(DB)
    c.executescript(OLD_SCHEMA)
    c.execute("INSERT INTO users (id, name, points, visits) VALUES (880001, 'Аркадий', 40, 2)")
    c.execute("INSERT INTO venues (name, cat, district, place, check_note, left_note,"
              " weekdays, active) VALUES ('Тбилисо','food','petro','грузинская','чек','5 столов','0,1,2',1)")
    c.execute("INSERT INTO slots (venue_id, hour, discount) VALUES (1, 15, 35)")
    for code, status in (("ТЧ-1111", "active"), ("ТЧ-2222", "visited"),
                         ("ТЧ-3333", "cancelled")):
        c.execute("INSERT INTO bookings (code, user_id, venue_id, slot_id, visit_date,"
                  " status, created_at) VALUES (?,880001,1,1,'2026-09-09',?, '2026-09-01 10:00:00')",
                  (code, status))
    c.commit(); c.close()


def cols(table):
    c = sqlite3.connect(DB)
    out = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    c.close(); return out


def tables():
    c = sqlite3.connect(DB)
    out = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close(); return out


def rows(sql):
    c = sqlite3.connect(DB)
    out = list(c.execute(sql)); c.close(); return out


async def run():
    build_old_db()
    print("\n1. Старая база: новых колонок нет")
    assert "redeemed_at" not in cols("bookings"), cols("bookings")
    assert "booking_events" not in tables()
    before = rows("SELECT code, status FROM bookings ORDER BY id")
    assert len(before) == 3
    OK(f"колонок {len(cols('bookings'))}, броней {len(before)}, журнала нет")

    print("\n2. create_all() в одиночку колонку не добавляет")
    from app.db import Base, engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    assert "redeemed_at" not in cols("bookings"), \
        "create_all вдруг добавил колонку — тест потерял смысл"
    OK("подтверждено: без миграций схема остаётся старой")

    print("\n3. init_db() доводит схему")
    from app.db import init_db
    await init_db()
    for c_ in ("redeemed_at", "redeemed_by", "cancelled_at"):
        assert c_ in cols("bookings"), f"нет колонки {c_}: {cols('bookings')}"
    assert "booking_events" in tables()
    assert "schema_migrations" in tables()
    OK("колонки добавлены, журнал создан")

    print("\n4. Данные на месте")
    after = rows("SELECT code, status FROM bookings ORDER BY id")
    assert after == before, f"{before} → {after}"
    assert rows("SELECT name, points FROM users")[0] == ("Аркадий", 40)
    OK(f"брони и пользователи не пострадали: {after}")

    print("\n5. Индексы созданы")
    idx = {r[0] for r in rows("SELECT name FROM sqlite_master WHERE type='index'")}
    for want in ("ix_bookings_code", "ix_bookings_venue_date",
                 "ix_booking_events_booking"):
        assert want in idx, f"нет индекса {want}: {sorted(idx)}"
    OK("поиск по коду и выборка «на сегодня» больше не полный проход")

    print("\n6. Повторный запуск ничего не ломает")
    applied = rows("SELECT name FROM schema_migrations ORDER BY name")
    await init_db()
    await init_db()
    assert rows("SELECT name FROM schema_migrations ORDER BY name") == applied, \
        "шаги записались повторно"
    assert rows("SELECT code, status FROM bookings ORDER BY id") == before
    OK(f"применено ровно один раз: {[a[0] for a in applied]}")

    print("\n7. Бот и API стартуют одновременно")
    # Оба сервиса рестартуют вместе после деплоя и входят в миграции разом.
    from app.migrations import run as run_migrations
    from app.db import engine as eng
    await asyncio.gather(*[run_migrations(eng) for _ in range(4)])
    assert rows("SELECT name FROM schema_migrations ORDER BY name") == applied
    assert rows("SELECT code, status FROM bookings ORDER BY id") == before
    OK("гонка при рестарте не ломает ни схему, ни данные")

    print("\n8. pending() честно отвечает")
    from app.migrations import pending, MIGRATIONS
    left = await pending(eng)
    assert left == [], f"осталось незакрытого: {left}"
    assert len(applied) == len(MIGRATIONS)
    OK(f"незакрытых шагов нет, всего шагов {len(MIGRATIONS)}")

    print("\n9. Погашение пишет в новые колонки и в журнал")
    from app import booking_flow
    from app.db import Session, Booking
    from app.models_partner import Partner, PartnerUser, VenueProfile
    async with Session() as s:
        p = Partner(title="Тест", status="active"); s.add(p); await s.flush()
        pu = PartnerUser(tg_id=770009, partner_id=p.id, name="Марина",
                         role="owner", active=True); s.add(pu); await s.flush()
        s.add(VenueProfile(venue_id=1, partner_id=p.id, status="active"))
        await s.commit()
        partner_id, pu_id = p.id, pu.id

    out = await booking_flow.redeem("ТЧ-1111", partner_id=partner_id,
                                    partner_user_id=pu_id, enforce_window=False,
                                    ip="10.0.0.7", device="iPhone")
    assert out.ok, out.message
    bk = rows("SELECT status, redeemed_at, redeemed_by FROM bookings WHERE code='ТЧ-1111'")[0]
    assert bk[0] == "visited" and bk[1] and bk[2] == pu_id, bk
    ev = rows("SELECT action, status_from, status_to, actor_name, ip, device "
              "FROM booking_events WHERE booking_id=1")
    assert ev == [("redeem", "active", "visited", "Марина", "10.0.0.7", "iPhone")], ev
    OK(f"бронь: {bk[0]}, отметил #{bk[2]} · журнал: {ev[0]}")

    print("\n10. Повтор объясняет, кто и когда — уже из журнала")
    again = await booking_flow.redeem("ТЧ-1111", partner_id=partner_id,
                                      partner_user_id=pu_id, enforce_window=False)
    assert not again.ok and again.reason == "already", again
    assert "Марина" in again.message, again.message
    assert len(rows("SELECT * FROM booking_events")) == 1, "журнал не должен расти на отказах"
    OK(f"«{again.message}», лишних записей в журнале нет")

    print("\n11. Отмена тоже попадает в журнал")
    out = await booking_flow.cancel(1, guest_id=880001)
    assert not out.ok and out.reason == "already_visited", out
    async with Session() as s:
        fresh = Booking(code="ТЧ-4444", user_id=880001, venue_id=1, slot_id=1,
                        visit_date=dt.date(2026, 9, 9),
                        status="active")
        s.add(fresh); await s.commit(); fresh_id = fresh.id
    out = await booking_flow.cancel(fresh_id, guest_id=880001, actor_name="Аркадий",
                                    ip="10.0.0.9", device="Android")
    assert out.ok, out.message
    ev = rows(f"SELECT action, status_from, status_to, actor_kind, actor_name "
              f"FROM booking_events WHERE booking_id={fresh_id}")
    assert ev == [("cancel", "active", "cancelled", "guest", "Аркадий")], ev
    assert rows(f"SELECT cancelled_at FROM bookings WHERE id={fresh_id}")[0][0]
    OK(f"журнал: {ev[0]}")

    print("\n12. Чужую бронь не отменить")
    async with Session() as s:
        other = Booking(code="ТЧ-5555", user_id=999999, venue_id=1, slot_id=1,
                        visit_date=dt.date(2026, 9, 9),
                        status="active")
        s.add(other); await s.commit(); other_id = other.id
    out = await booking_flow.cancel(other_id, guest_id=880001)
    assert not out.ok and out.http == 404, out
    assert rows(f"SELECT status FROM bookings WHERE id={other_id}")[0][0] == "active"
    OK("404, чужая бронь не тронута")

    print("\nВСЁ ПРОШЛО")


asyncio.run(run())
