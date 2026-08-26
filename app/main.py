# -*- coding: utf-8 -*-
"""«Тихий Час» — Telegram-бот. Запуск: python -m app.main"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

from .config import BOT_TOKEN, MINIAPP_URL
from .db import init_db
from .seed import seed
from .handlers import guest, partner

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Заполните BOT_TOKEN в .env (токен из @BotFather)")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(guest.router)
    dp.include_router(partner.router)

    await init_db()
    await seed()

    me = await bot.get_me()
    # username бота нужен для реферальных ссылок
    dp["bot_username"] = me.username

    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
    ])
    if MINIAPP_URL:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Тихий Час",
                                         web_app=WebAppInfo(url=MINIAPP_URL)))

    logging.info("Бот @%s запущен", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
