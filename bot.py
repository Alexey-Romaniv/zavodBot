"""Точка входа: python bot.py"""
from __future__ import annotations

import asyncio
import logging
import socket
import sys
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

import admin
import config
import db
import handlers
import handlers_admin
import middlewares
import scheduler

COMMANDS = [
    BotCommand(command="week", description="Смены на неделю"),
    BotCommand(command="shifts", description="Предстоящие смены / отмена"),
    BotCommand(command="add", description="Вписать смены вручную"),
    BotCommand(command="confirm", description="Подтвердить прошедшие смены"),
    BotCommand(command="money", description="Деньги и дата выплаты"),
    BotCommand(command="penalties", description="Штрафы и удержания"),
    BotCommand(command="achievements", description="Достижения и звание"),
    BotCommand(command="help", description="Справка"),
]


class IPv6OnlySession(AiohttpSession):
    """Сессия, которая соединяется с Telegram только по IPv6.

    На машине без внешнего IPv4 (но с внутренним IPv4-маршрутом) попытка пойти по IPv4
    не отваливается сразу, а висит до таймаута — сообщения задерживаются или теряются.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._connector_init["family"] = socket.AF_INET6


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db.init()
    session = IPv6OnlySession() if config.FORCE_IPV6 else None
    if session is not None:
        logging.info("Соединения с Telegram — только по IPv6")
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(middlewares.RememberUser())
    dp.include_router(handlers.router)
    dp.include_router(handlers_admin.router)

    sched = scheduler.setup(bot)
    sched.start()
    await bot.set_my_commands(COMMANDS)
    logging.info("Бот запущен. Часовой пояс: %s", config.TZ)
    logging.info(
        "Админка: %s",
        "включена, вход по /admin" if admin.configured() else "выключена (нет SHIFTBOT_ADMIN_PASSWORD)",
    )
    try:
        await dp.start_polling(bot)
    finally:
        await scheduler.shutdown(sched)
        await bot.session.close()


if __name__ == "__main__":
    if not config.BOT_TOKEN:
        sys.exit("Не задан BOT_TOKEN. Скопируй .env.example в .env и впиши токен от @BotFather.")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Остановлено пользователем")
