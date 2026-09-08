# -*- coding: utf-8 -*-
"""Сквозной тест кабинета партнёра, этап 2 — онбординг заведения.

Прогоняет весь путь через настоящие эндпоинты FastAPI (не моки):
создание заявки на сайте → регистрация в кабинете → заполнение
карточки → фото → тихие часы → отправка на модерацию → решение
администратора → проверка, что заведение реально видно в публичной
ленте (той самой, что читают бот и сайт).
"""
import asyncio
import base64
import os

TOKEN = "123456:TESTTOKENabcdefghijklmnopqrstuvw"
os.environ["BOT_TOKEN"] = TOKEN          # обязательно до импорта app.* — config.py
os.environ.setdefault("ADMIN_IDS", "999999")  # читает переменные при загрузке модуля


async def run():
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.api import api
    from app.auth import build_init_data
    from app.db import Session, Venue, Slot, init_db
    from app.models_partner import Partner, PartnerUser, VenueProfile

    OWNER = {"id": 900100200, "first_name": "Наталья", "last_name": "Р.", "username": "nata_biz"}

    async def seed_partner_and_owner():
        """Ставим Partner+PartnerUser вручную — как будто заявка с сайта
        уже одобрена и владельцу выдан доступ (этап 1 уже покрыт тестами)."""
        await init_db()
        async with Session() as s:
            p = Partner(title="Тестовая сеть", status="new")
            s.add(p)
            await s.flush()
            s.add(PartnerUser(tg_id=OWNER["id"], partner_id=p.id, name="Наталья",
                              role="owner"))
            await s.commit()

    await seed_partner_and_owner()
    init_data = build_init_data(OWNER, TOKEN)
    H = {"X-Init-Data": init_data}

    with TestClient(api) as c:
        # --- Шаг 1: карточка заведения ---
        r = c.post("/api/cab/venues", json={
            "name": "Полынь", "cat": "coffee", "district": "kalin",
            "address": "пр. Науки, 15", "phone": "+7 921 555-00-11", "url": ""
        }, headers=H)
        assert r.status_code == 200, r.text
        venue_id = r.json()["venue_id"]
        print(f"OK шаг 1: заведение #{venue_id} создано")

        # чужой пользователь не должен видеть/редактировать
        stranger = build_init_data({"id": 1, "first_name": "Чужой"}, TOKEN)
        r_forbidden = c.get(f"/api/cab/venues/{venue_id}",
                            headers={"X-Init-Data": stranger})
        assert r_forbidden.status_code == 403, "чужой аккаунт без доступа должен получить 403"
        print("OK: чужой Telegram-аккаунт не видит заведение (403)")

        # попытка отправить на модерацию без фото и часов — должна упасть с понятной ошибкой
        r_early = c.post(f"/api/cab/venues/{venue_id}/submit", headers=H)
        assert r_early.status_code == 422
        assert "фото" in r_early.json()["detail"] and "тихие часы" in r_early.json()["detail"]
        print("OK: отправка без фото/часов отклонена с понятным списком недостающего")

        # --- Шаг 2: фото ---
        tiny_png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 100).decode()
        r = c.post(f"/api/cab/venues/{venue_id}/photos",
                  json={"data_url": f"data:image/png;base64,{tiny_png}"}, headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["is_cover"] is True
        print("OK шаг 2: фото загружено и стало обложкой")

        # слишком большое фото должно отклоняться
        from app.cabinet import MAX_PHOTO_BYTES
        huge = base64.b64encode(b"0" * (MAX_PHOTO_BYTES + 1_000)).decode()
        r_huge = c.post(f"/api/cab/venues/{venue_id}/photos",
                        json={"data_url": f"data:image/png;base64,{huge}"}, headers=H)
        assert r_huge.status_code == 413, r_huge.status_code
        limit_mb = MAX_PHOTO_BYTES // 1_000_000
        assert f"{limit_mb} МБ" in r_huge.json()["detail"], r_huge.json()
        print(f"OK: фото больше {limit_mb} МБ отклоняется (413), и текст называет тот же предел")

        # --- Шаг 3: тихие часы (будни, три окна) ---
        r = c.put(f"/api/cab/venues/{venue_id}/quiet-hours", json={
            "weekdays": [0, 1, 2, 3, 4],
            "slots": [{"hour": 11, "discount": 30, "capacity": 6},
                     {"hour": 12, "discount": 25, "capacity": 6}]
        }, headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["windows"] == 10, "5 дней × 2 слота = 10 окон"
        print("OK шаг 3: сетка тихих часов сохранена (5 будней × 2 часа = 10 окон)")

        # заведение с модерацией нельзя редактировать — проверим после отправки
        # --- Шаг 4: отправка на модерацию ---
        r = c.post(f"/api/cab/venues/{venue_id}/submit", headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "moderation"
        print("OK шаг 4: отправлено на модерацию")

        # повторная отправка должна быть отклонена
        r_dup = c.post(f"/api/cab/venues/{venue_id}/submit", headers=H)
        assert r_dup.status_code == 409
        print("OK: повторная отправка на модерацию отклонена (409)")

        # редактирование во время модерации запрещено
        r_edit_locked = c.patch(f"/api/cab/venues/{venue_id}", json={
            "name": "Полынь", "cat": "coffee", "district": "kalin",
            "address": "другой адрес", "phone": "", "url": ""
        }, headers=H)
        assert r_edit_locked.status_code == 409
        print("OK: редактирование во время модерации заблокировано (409)")

        # --- Модерация: проверяем, что заведение реально ЕЩЁ НЕ в витрине ---
        async with Session() as s:
            v_before = await s.get(Venue, venue_id)
            assert v_before.active is False, "черновик не должен быть виден гостям до одобрения"
        print("OK: до одобрения заведение НЕ активно в публичной витрине")

        # --- Администратор одобряет (эмулируем callback напрямую, как в test_scenario.py) ---
        from unittest.mock import AsyncMock, MagicMock
        from app.handlers.partner import approve_venue, ApproveCB

        cq = MagicMock()
        cq.from_user.id = 999999  # ADMIN_IDS из окружения
        cq.message = MagicMock()
        cq.message.text = "карточка на модерации"
        cq.message.edit_text = AsyncMock()
        cq.answer = AsyncMock()
        cq.bot.send_message = AsyncMock()

        await approve_venue(cq, ApproveCB(venue_id=venue_id))
        assert cq.message.edit_text.called, "администратор должен увидеть подтверждение"
        assert cq.bot.send_message.called, "владелец должен получить уведомление о публикации"
        print("OK: администратор одобрил заведение")

        # --- Проверяем, что заведение появилось в ТОЙ ЖЕ публичной таблице,
        #     которую читают бот и сайт (Venue.active + Slot) ---
        async with Session() as s:
            v_after = await s.get(Venue, venue_id)
            assert v_after.active is True, "после одобрения заведение должно быть активно"
            assert v_after.weekdays == "0,1,2,3,4", f"дни не совпали: {v_after.weekdays}"
            slots = (await s.scalars(select(Slot).where(Slot.venue_id == venue_id))).all()
            hours = sorted((sl.hour, sl.discount) for sl in slots)
            assert hours == [(11, 30), (12, 25)], f"слоты витрины не совпали: {hours}"

            profile = await s.scalar(select(VenueProfile).where(
                VenueProfile.venue_id == venue_id))
            assert profile.status == "active"
            partner = await s.get(Partner, profile.partner_id)
            assert partner.status == "active"
        print("OK: заведение опубликовано в РЕАЛЬНОЙ витрине (Venue.active=True, "
              "2 слота, статус партнёра = active) — та же таблица, что читает бот/сайт")

        # --- Убедимся, что это заведение теперь видно через гостевую функцию бота ---
        from app.db import venue_weekdays
        async with Session() as s:
            v_final = await s.get(Venue, venue_id)
        assert 0 in venue_weekdays(v_final), "понедельник должен быть в рабочих днях"
        assert 5 not in venue_weekdays(v_final), "суббота не настраивалась — не должно быть"
        print("OK: гостевая логика бота (venue_weekdays) видит опубликованное заведение корректно")

    print("\nВСЕ ШАГИ ЭТАПА 2 ПРОЙДЕНЫ УСПЕШНО — от регистрации до публикации в витрине")

asyncio.run(run())


async def run_reject_scenario():
    """Отдельно проверяем путь отклонения: партнёр должен получить причину
    и снова смочь редактировать/отправить заявку."""
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from unittest.mock import AsyncMock, MagicMock

    from app.api import api
    from app.auth import build_init_data
    from app.db import Session, Venue
    from app.models_partner import Partner, PartnerUser, VenueProfile
    from app.handlers.partner import (RejectCB, RejectForm, reject_venue_start,
                                      reject_venue_finish)

    OWNER2 = {"id": 900100201, "first_name": "Дмитрий"}
    async with Session() as s:
        p = Partner(title="Вторая сеть", status="new")
        s.add(p); await s.flush()
        s.add(PartnerUser(tg_id=OWNER2["id"], partner_id=p.id, name="Дмитрий", role="owner"))
        await s.commit()

    init_data = build_init_data(OWNER2, TOKEN)
    H = {"X-Init-Data": init_data}

    with TestClient(api) as c:
        r = c.post("/api/cab/venues", json={
            "name": "Дом у моря", "cat": "spa", "district": "vasil",
            "address": "наб. Макарова, 4", "phone": "+7 900 000-00-00", "url": ""
        }, headers=H)
        venue_id = r.json()["venue_id"]

        c.post(f"/api/cab/venues/{venue_id}/photos",
              json={"data_url": "data:image/png;base64," + base64.b64encode(b"1"*50).decode()},
              headers=H)
        c.put(f"/api/cab/venues/{venue_id}/quiet-hours", json={
            "weekdays": [5, 6], "slots": [{"hour": 12, "discount": 30, "capacity": 4}]
        }, headers=H)
        c.post(f"/api/cab/venues/{venue_id}/submit", headers=H)

        async with Session() as s:
            profile = await s.scalar(select(VenueProfile).where(
                VenueProfile.venue_id == venue_id))
            assert profile.status == "moderation"

        # админ жмёт "Отклонить"
        cq = MagicMock()
        cq.from_user.id = 999999
        cq.message = MagicMock(); cq.message.reply = AsyncMock()
        cq.answer = AsyncMock()
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={})

        await reject_venue_start(cq, RejectCB(venue_id=venue_id), state)
        assert cq.message.reply.called, "админ должен получить запрос причины"
        assert state.set_state.called
        print("OK: отклонение запрашивает причину у администратора")

        # админ присылает причину текстом
        state2 = AsyncMock()
        state2.get_data = AsyncMock(return_value={"venue_id": venue_id})
        m = MagicMock()
        m.text = "Нет фото интерьера, только логотип — добавьте реальные фото зала"
        m.answer = AsyncMock()
        m.bot.send_message = AsyncMock()

        await reject_venue_finish(m, state2)
        assert m.answer.called
        assert m.bot.send_message.called, "владелец должен получить причину отказа"
        sent_text = m.bot.send_message.call_args[0][1]
        assert "фото интерьера" in sent_text, "причина должна дойти до партнёра дословно"
        print("OK: причина отказа доставлена владельцу заведения")

        async with Session() as s:
            profile2 = await s.scalar(select(VenueProfile).where(
                VenueProfile.venue_id == venue_id))
            assert profile2.status == "draft", "после отклонения статус должен вернуться в draft"
            venue2 = await s.get(Venue, venue_id)
            assert venue2.active is False, "отклонённое заведение не должно быть в витрине"
        print("OK: статус вернулся в draft, заведение НЕ опубликовано")

        # партнёр снова может редактировать (draft, не moderation)
        r_edit = c.patch(f"/api/cab/venues/{venue_id}", json={
            "name": "Дом у моря", "cat": "spa", "district": "vasil",
            "address": "наб. Макарова, 4, вход со двора", "phone": "+7 900 000-00-00", "url": ""
        }, headers=H)
        assert r_edit.status_code == 200, "после отклонения редактирование должно быть разрешено"
        print("OK: партнёр может исправить заявку и отправить снова")

    print("\nСЦЕНАРИЙ ОТКЛОНЕНИЯ ПРОЙДЕН УСПЕШНО")

asyncio.run(run_reject_scenario())
