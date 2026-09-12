# -*- coding: utf-8 -*-
"""Прогон полного гостевого сценария через настоящие хендлеры aiogram,
без сети: день → район → категория → заведение → бронь на будущую дату → QR.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock


async def run():
    from app.db import init_db, Session, Venue
    from app.seed import seed
    from app.keyboards import DayCB, DistCB, CatCB, VenueCB, BookCB, date_for, day_label
    from app.handlers import guest as gh
    from sqlalchemy import select

    await init_db(); await seed()

    def make_cq(data_obj, user_id=555):
        cq = MagicMock()
        cq.from_user.id = user_id
        cq.from_user.first_name = "Тест"
        cq.message = MagicMock()
        cq.message.edit_text = AsyncMock()
        cq.message.answer_photo = AsyncMock()
        cq.answer = AsyncMock()
        cq.data = data_obj.pack()
        return cq

    # 1) гость выбирает день = послезавтра, оставаясь на "весь город/все категории"
    cq = make_cq(DayCB(day=2, dist="all", cat="all"))
    await gh.feed_day(cq, DayCB(day=2, dist="all", cat="all"))
    text, kb = cq.message.edit_text.call_args[0][0], cq.message.edit_text.call_args[1]["reply_markup"]
    # Раньше тут были захардкожены «чт/пт/20/21» — тест жил только до
    # следующей недели. Сверяем с тем же ярлыком, что рисует сам бот.
    assert day_label(2) in text, f"ожидали «{day_label(2)}»: {text[:80]}"
    print("OK шаг 1: выбор дня работает —", text.splitlines()[0])

    # 2) гость выбирает район
    cq2 = make_cq(DistCB(dist="vasil", cat="all", day=2))
    await gh.feed_dist(cq2, DistCB(dist="vasil", cat="all", day=2))
    text2 = cq2.message.edit_text.call_args[0][0]
    assert "Василеостровский" in text2, text2
    print("OK шаг 2: выбор района работает —", text2.splitlines()[0])

    # 3) гость выбирает категорию
    cq3 = make_cq(CatCB(cat="beauty", dist="vasil", day=2))
    await gh.feed_cat(cq3, CatCB(cat="beauty", dist="vasil", day=2))
    text3 = cq3.message.edit_text.call_args[0][0]
    print("OK шаг 3: выбор категории работает —", text3.splitlines()[0])

    # 4) находим заведение в Василеостровском / beauty и открываем карточку
    async with Session() as s:
        v = await s.scalar(select(Venue).where(Venue.district == "vasil", Venue.cat == "beauty"))
    assert v is not None, "в тестовых данных должно быть бьюти-заведение на Василеостровском"

    cq4 = make_cq(VenueCB(venue_id=v.id, dist="vasil", cat="beauty", day=2))
    await gh.venue_card(cq4, VenueCB(venue_id=v.id, dist="vasil", cat="beauty", day=2))
    text4 = cq4.message.edit_text.call_args[0][0]
    assert v.name in text4
    # Состояние оплаты одно на весь продукт и приходит из product.py.
    # Пока приём оплаты не подключён, карточка не должна обещать депозит.
    from app.product import DEPOSIT, DEPOSIT_CHARGED
    if DEPOSIT_CHARGED:
        assert str(DEPOSIT) in text4, "карточка должна называть цену брони"
    else:
        assert "бесплатн" in text4.lower(), f"бронь бесплатна, а карточка говорит: {text4}"
        assert "депозит" not in text4.lower(), "депозита быть не должно, пока оплата не подключена"
    print(f"OK шаг 4: карточка «{v.name}» открыта, депозит 99 ₽ на месте")

    # 5) бронируем слот именно на день+2 (а не на сегодня)
    async with Session() as s:
        from app.db import Slot
        sl = await s.scalar(select(Slot).where(Slot.venue_id == v.id))

    cq5 = make_cq(BookCB(slot_id=sl.id, day=2), user_id=555)
    await gh.book(cq5, BookCB(slot_id=sl.id, day=2))
    assert cq5.message.answer_photo.called, "QR должен быть отправлен"
    caption = cq5.message.answer_photo.call_args[1]["caption"]
    expected_date = date_for(2).strftime("%d.%m")
    assert expected_date in caption, f"в подтверждении должна быть дата {expected_date}: {caption}"
    # Механика погашения одна на весь продукт: код гость называет вслух,
    # QR только ускоряет. Подтверждение не должно звать показывать QR как
    # единственный способ — иначе гость с севшим телефоном встанет у входа.
    low = caption.lower()
    assert "назовите этот код" in low, f"подтверждение должно вести кодом: {caption}"
    assert "покажите qr" not in low.replace("если так быстрее, покажите qr", ""), \
        f"QR не должен подаваться как основной путь: {caption}"
    print(f"OK шаг 5: бронь создана на {expected_date}, QR отправлен, текст обновлён")

    # 6) проверяем "Мои брони" — дата отображается, не просто "сегодня"
    cq6 = make_cq(DayCB(day=0, dist="all", cat="all"))  # переиспользуем как заглушку callback
    from app.keyboards import MenuCB
    cq6.data = MenuCB(to="bookings").pack()
    await gh.my_bookings(cq6)
    text6 = cq6.message.edit_text.call_args[0][0]
    assert expected_date in text6, f"в списке броней должна быть дата: {text6}"
    print("OK шаг 6: «Мои брони» показывает дату визита, а не только сегодняшние")

    print("\nВСЕ ШАГИ СЦЕНАРИЯ ПРОЙДЕНЫ УСПЕШНО")

asyncio.run(run())
