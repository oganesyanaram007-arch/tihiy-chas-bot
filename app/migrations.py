# -*- coding: utf-8 -*-
"""Миграции схемы.

Раньше схема создавалась одним `Base.metadata.create_all()`. Он умеет
только создавать недостающие таблицы: новая колонка в уже существующей
таблице на живой базе не появлялась никогда. То есть любое изменение
модели молча не доезжало до прода, а код, который на неё рассчитывал,
падал на первом же запросе.

Как это работает
----------------
Миграции — упорядоченный список именованных шагов. Применённые записаны
в таблице `schema_migrations`, при старте выполняются только новые.
Запускается из `init_db()`, то есть на каждом старте бота и API —
деплой у нас и есть рестарт сервисов.

Правила
-------
· Шаги **только добавляют**: новые таблицы, колонки, индексы. Ничего
  не удаляют и не переписывают данные. Удаление чего-либо на живой базе
  согласуется отдельно и делается руками, а не этим файлом.
· Каждый шаг идемпотентен сам по себе (`add_column` проверяет наличие,
  индексы создаются через IF NOT EXISTS). Бот и API стартуют почти
  одновременно и могут войти сюда параллельно: повторное применение
  не должно ничего ломать.
· Уже применённый шаг не редактируется — на прод он не поедет второй раз.
  Изменение оформляется новым шагом.
· Имена шагов нумерованные и неизменные: по ним определяется, что сделано.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Помощники: идемпотентные и не зависящие от диалекта
# ---------------------------------------------------------------------

async def _table_names(conn) -> set[str]:
    return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))


async def _column_names(conn, table: str) -> set[str]:
    if table not in await _table_names(conn):
        return set()
    cols = await conn.run_sync(lambda c: inspect(c).get_columns(table))
    return {c["name"] for c in cols}


async def add_column(conn, table: str, column: str, ddl: str) -> bool:
    """ALTER TABLE ... ADD COLUMN, если колонки ещё нет.

    ddl — тип и умолчание, например "VARCHAR(64) DEFAULT ''".
    SQLite не умеет добавлять NOT NULL без умолчания и UNIQUE вовсе,
    поэтому все добавляемые колонки — необязательные.
    """
    if column in await _column_names(conn, table):
        return False
    await conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
    log.info("миграция: %s.%s добавлена", table, column)
    return True


async def create_index(conn, name: str, table: str, columns: str) -> None:
    await conn.execute(text(f'CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})'))


async def create_table(conn, name: str, body: str) -> None:
    await conn.execute(text(f'CREATE TABLE IF NOT EXISTS {name} ({body})'))


# ---------------------------------------------------------------------
# Сами шаги
# ---------------------------------------------------------------------

async def _m0001_booking_redemption(conn) -> None:
    """Кто и когда погасил код — в самой брони.

    До этого ответ «код уже погашен в 15:12, отметила Марина» собирался
    из журнала действий кабинета: колонок в таблице не было, а добавить
    их было нечем. Теперь есть.
    """
    await add_column(conn, "bookings", "redeemed_at", "DATETIME")
    await add_column(conn, "bookings", "redeemed_by", "INTEGER")
    await add_column(conn, "bookings", "cancelled_at", "DATETIME")
    # Погашение ищет бронь по коду на каждом вводе — без индекса это
    # полный проход по таблице на глазах у гостя.
    await create_index(conn, "ix_bookings_code", "bookings", "code")
    # Экран «брони на сегодня» фильтрует ровно по этой паре.
    await create_index(conn, "ix_bookings_venue_date", "bookings",
                       "venue_id, visit_date")


async def _m0002_booking_events(conn) -> None:
    """Журнал переходов: append-only, пишется на каждую смену статуса.

    Без него споры «мы гасили — нет, не гасили» неразрешимы: таблица
    броней хранит только последнее состояние и не помнит, как в него
    пришли. Здесь — прежний и новый статус, актор, время, устройство.

    Отдельно от audit_log: тот про действия партнёра в кабинете
    (создал заведение, позвал сотрудника), этот — про жизнь брони,
    и писать в него будут веб, бот и админка одинаково.
    """
    await create_table(conn, "booking_events", """
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        booking_id INTEGER NOT NULL,
        action VARCHAR(32) NOT NULL,
        status_from VARCHAR(16) DEFAULT '',
        status_to VARCHAR(16) DEFAULT '',
        actor_kind VARCHAR(16) DEFAULT '',
        actor_id INTEGER,
        actor_name VARCHAR(128) DEFAULT '',
        ip VARCHAR(64) DEFAULT '',
        device VARCHAR(200) DEFAULT '',
        note VARCHAR(300) DEFAULT '',
        created_at DATETIME NOT NULL
    """)
    await create_index(conn, "ix_booking_events_booking", "booking_events", "booking_id")
    await create_index(conn, "ix_booking_events_created", "booking_events", "created_at")


MIGRATIONS: list[tuple[str, object]] = [
    ("0001_booking_redemption", _m0001_booking_redemption),
    ("0002_booking_events", _m0002_booking_events),
]


# ---------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------

async def _ensure_ledger(conn) -> None:
    await create_table(conn, "schema_migrations", """
        name VARCHAR(120) PRIMARY KEY,
        applied_at DATETIME NOT NULL
    """)


async def applied_names(conn) -> set[str]:
    await _ensure_ledger(conn)
    rows = await conn.execute(text("SELECT name FROM schema_migrations"))
    return {r[0] for r in rows}


async def run(engine) -> list[str]:
    """Применяет невыполненные шаги. Возвращает имена применённых.

    Каждый шаг — в своей транзакции: упавший не утаскивает за собой
    предыдущие, и после починки достаточно перезапустить сервис.
    """
    async with engine.begin() as conn:
        done = await applied_names(conn)

    fresh: list[str] = []
    for name, step in MIGRATIONS:
        if name in done:
            continue
        async with engine.begin() as conn:
            await step(conn)
            try:
                await conn.execute(text(
                    "INSERT INTO schema_migrations (name, applied_at) "
                    "VALUES (:n, CURRENT_TIMESTAMP)"), {"n": name})
            except IntegrityError:
                # Второй процесс успел раньше: шаги идемпотентны,
                # так что это не ошибка, а обычная гонка при рестарте.
                log.info("миграция %s уже записана другим процессом", name)
                continue
        fresh.append(name)
        log.info("миграция применена: %s", name)

    if fresh:
        log.info("миграций применено: %d", len(fresh))
    return fresh


async def pending(engine) -> list[str]:
    """Что ещё не применено — для проверки перед выкаткой."""
    async with engine.begin() as conn:
        done = await applied_names(conn)
    return [n for n, _ in MIGRATIONS if n not in done]
