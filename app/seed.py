# -*- coding: utf-8 -*-
"""Наполнение базы заведениями. Список синхронизирован 1:1 с данными
на сайте (tihiy-chas-v10.html, массив const V) — 34 заведения, 15 районов
Санкт-Петербурга, у каждого своё расписание дней недели."""
from sqlalchemy import select
from .db import Session, Venue, Slot

# 0=пн, 1=вт, 2=ср, 3=чт, 4=пт, 5=сб, 6=вс — как в Python datetime.weekday()
WD5 = "0,1,2,3,4"          # будни
WD6 = "0,1,2,3,4,5"        # будни + суббота
WD7 = "0,1,2,3,4,5,6"      # все дни
WD4 = "0,1,2,3"            # пн–чт

VENUES = [
    # ---------- Петроградский ----------
    dict(name="Киото бар", cat="food", district="petro", weekdays=WD5,
         place="суши · Большой пр. П.С.", check_note="чек ~1 800 ₽",
         left_note="осталось 4 стола", slots=[(15, 40), (16, 40), (17, 30)]),
    dict(name="Крем", cat="coffee", district="petro", weekdays=WD7,
         place="кофейня · Большая Монетная", check_note="чек ~700 ₽",
         left_note="6 мест у окна", slots=[(10, 30), (11, 30)]),
    dict(name="Тбилисо", cat="food", district="petro", weekdays=WD5,
         place="грузинская · Кронверкский пр.", check_note="чек ~1 600 ₽",
         left_note="осталось 5 столов", slots=[(14, 35), (15, 35), (16, 35)]),
    dict(name="Мята", cat="beauty", district="petro", weekdays=WD6,
         place="маникюр · Каменноостровский пр.", check_note="чек ~1 500 ₽",
         left_note="3 окна у мастеров", slots=[(11, 35), (12, 35), (13, 35)]),
    dict(name="Право.", cat="beauty", district="petro", weekdays=WD6,
         place="барбершоп · Большой пр. П.С.", check_note="чек ~1 200 ₽",
         left_note="2 кресла свободно", slots=[(12, 30), (13, 30), (14, 30)]),
    dict(name="Пар", cat="spa", district="petro", weekdays=WD7,
         place="спа и массаж · Чкаловская наб.", check_note="чек ~2 500 ₽",
         left_note="4 слота сегодня", slots=[(12, 40), (14, 40), (15, 40)]),
    dict(name="Квест Хаус", cat="fun", district="petro", weekdays=WD7,
         place="квесты · Петроградская наб.", check_note="1 900 ₽ команда",
         left_note="3 слота на сегодня", slots=[(12, 50), (14, 50), (16, 50)]),

    # ---------- Центральный ----------
    dict(name="Вход с Рубинштейна", cat="food", district="centr", weekdays=WD4,
         place="европейская · ул. Рубинштейна", check_note="чек ~2 000 ₽",
         left_note="осталось 6 столов", slots=[(13, 35), (14, 35), (15, 40)]),
    dict(name="Полторы комнаты", cat="coffee", district="centr", weekdays=WD7,
         place="кофейня · Литейный пр.", check_note="чек ~650 ₽",
         left_note="8 мест", slots=[(10, 25), (11, 30), (12, 30)]),
    dict(name="Фонтанка Nails", cat="beauty", district="centr", weekdays=WD6,
         place="маникюр · наб. Фонтанки", check_note="чек ~1 600 ₽",
         left_note="4 окна у мастеров", slots=[(12, 35), (13, 35), (16, 30)]),
    dict(name="Лофт-квест", cat="fun", district="centr", weekdays=WD7,
         place="квесты · Лиговский пр.", check_note="1 700 ₽ команда",
         left_note="2 слота", slots=[(14, 45), (16, 50)]),

    # ---------- Адмиралтейский ----------
    dict(name="Сенная 12", cat="food", district="adm", weekdays=WD5,
         place="бистро · Сенная площадь", check_note="чек ~1 100 ₽",
         left_note="осталось 9 столов", slots=[(12, 30), (13, 35), (14, 35)]),
    dict(name="Банный день", cat="spa", district="adm", weekdays=WD7,
         place="спа · Измайловский пр.", check_note="чек ~2 200 ₽",
         left_note="3 слота", slots=[(11, 40), (13, 40), (15, 35)]),
    dict(name="Мойка на Обводном", cat="auto", district="adm", weekdays=WD6,
         place="автомойка · Обводный канал", check_note="чек ~1 300 ₽",
         left_note="без очереди", slots=[(10, 30), (12, 30), (14, 25)]),

    # ---------- Василеостровский ----------
    dict(name="Гавань", cat="coffee", district="vasil", weekdays=WD6,
         place="кофейня · Большой пр. В.О.", check_note="чек ~700 ₽",
         left_note="5 мест у окна", slots=[(10, 30), (11, 25), (15, 30)]),
    dict(name="Линия 7", cat="food", district="vasil", weekdays=WD5,
         place="паста · 7-я линия В.О.", check_note="чек ~1 400 ₽",
         left_note="осталось 4 стола", slots=[(14, 40), (15, 40), (16, 35)]),
    dict(name="Штиль", cat="beauty", district="vasil", weekdays=WD6,
         place="барбершоп · Средний пр. В.О.", check_note="чек ~1 100 ₽",
         left_note="2 кресла", slots=[(11, 30), (12, 30), (13, 30)]),

    # ---------- Московский ----------
    dict(name="Меридиан", cat="food", district="mosk", weekdays=WD5,
         place="грузинская · Московский пр.", check_note="чек ~1 500 ₽",
         left_note="осталось 7 столов", slots=[(13, 35), (15, 40), (16, 40)]),
    dict(name="Тихая вода", cat="spa", district="mosk", weekdays=WD7,
         place="массаж · Звёздная", check_note="чек ~2 300 ₽",
         left_note="4 слота сегодня", slots=[(12, 40), (14, 40)]),
    dict(name="Детейлинг Пулково", cat="auto", district="mosk", weekdays=WD5,
         place="детейлинг · Пулковское ш.", check_note="чек ~3 500 ₽",
         left_note="2 поста", slots=[(11, 30), (13, 30)]),

    # ---------- Невский ----------
    dict(name="Дыбенко Кухня", cat="food", district="nev", weekdays=WD5,
         place="азиатская · пр. Большевиков", check_note="чек ~900 ₽",
         left_note="осталось 8 столов", slots=[(12, 35), (14, 40), (16, 35)]),
    dict(name="Невский Стиль", cat="beauty", district="nev", weekdays=WD6,
         place="салон · ул. Дыбенко", check_note="чек ~1 400 ₽",
         left_note="5 окон", slots=[(10, 35), (11, 35), (13, 30)]),

    # ---------- Приморский ----------
    dict(name="Комендантский", cat="coffee", district="primor", weekdays=WD7,
         place="кофейня · Комендантский пр.", check_note="чек ~650 ₽",
         left_note="6 мест", slots=[(10, 30), (11, 30), (14, 25)]),
    dict(name="Картинг Приморский", cat="fun", district="primor", weekdays=WD7,
         place="картинг · Планерная", check_note="2 200 ₽",
         left_note="4 слота", slots=[(13, 45), (15, 50)]),
    dict(name="Ольга Студия", cat="beauty", district="primor", weekdays=WD6,
         place="маникюр · Богатырский пр.", check_note="чек ~1 500 ₽",
         left_note="3 окна", slots=[(11, 35), (12, 35), (15, 30)]),

    # ---------- Выборгский ----------
    dict(name="Просвещения 40", cat="food", district="vyb", weekdays=WD5,
         place="бургеры · пр. Просвещения", check_note="чек ~1 000 ₽",
         left_note="осталось 6 столов", slots=[(13, 35), (14, 35), (17, 30)]),
    dict(name="Мойка Озерки", cat="auto", district="vyb", weekdays=WD6,
         place="автомойка · Озерки", check_note="чек ~1 200 ₽",
         left_note="без очереди", slots=[(10, 30), (12, 30), (14, 30)]),

    # ---------- Калининский ----------
    dict(name="Академка", cat="coffee", district="kalin", weekdays=WD6,
         place="кофейня · Академическая", check_note="чек ~600 ₽",
         left_note="7 мест", slots=[(10, 30), (11, 30)]),
    dict(name="Мурино Beauty", cat="beauty", district="kalin", weekdays=WD6,
         place="салон · Гражданский пр.", check_note="чек ~1 300 ₽",
         left_note="4 окна", slots=[(12, 35), (13, 35), (16, 30)]),

    # ---------- Кировский / Фрунзенский / Красногвардейский / Красносельский / Пушкинский ----------
    dict(name="Нарвская 5", cat="food", district="kirov", weekdays=WD5,
         place="домашняя кухня · Нарвская", check_note="чек ~900 ₽",
         left_note="осталось 5 столов", slots=[(12, 35), (13, 35), (15, 35)]),
    dict(name="Купчино Спа", cat="spa", district="frunz", weekdays=WD7,
         place="спа · Бухарестская", check_note="чек ~2 000 ₽",
         left_note="3 слота", slots=[(11, 40), (14, 40)]),
    dict(name="Охта Гриль", cat="food", district="krasnog", weekdays=WD6,
         place="гриль · Охта", check_note="чек ~1 300 ₽",
         left_note="осталось 6 столов", slots=[(14, 35), (16, 40)]),
    dict(name="Ветеранов Nails", cat="beauty", district="krasnos", weekdays=WD6,
         place="маникюр · пр. Ветеранов", check_note="чек ~1 200 ₽",
         left_note="4 окна", slots=[(11, 35), (13, 35)]),
    dict(name="Царское зерно", cat="coffee", district="push", weekdays=WD7,
         place="кофейня · Пушкин", check_note="чек ~650 ₽",
         left_note="5 мест", slots=[(10, 30), (12, 30), (15, 25)]),
]


async def seed() -> None:
    async with Session() as s:
        if await s.scalar(select(Venue.id).limit(1)):
            return  # уже наполнено
        for v in VENUES:
            venue = Venue(name=v["name"], cat=v["cat"], district=v["district"],
                          place=v["place"], check_note=v["check_note"],
                          left_note=v["left_note"], weekdays=v["weekdays"])
            s.add(venue)
            await s.flush()
            for hour, disc in v["slots"]:
                s.add(Slot(venue_id=venue.id, hour=hour, discount=disc))
        await s.commit()
