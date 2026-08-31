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

# Список для меню Telegram: те же действия доступны кнопками, но кому-то
# привычнее выбрать из «/» — пусть будет и там.
COMMANDS = [
    BotCommand(command="week", description="📅 Смены на неделю"),
    BotCommand(command="shifts", description="🗓 Предстоящие смены / отмена"),
    BotCommand(command="money", description="💰 Деньги и дата выплаты"),
    BotCommand(command="penalties", description="⚖️ Штрафы и удержания"),
    BotCommand(command="confirm", description="⏳ Подтвердить прошедшие смены"),
    BotCommand(command="add", description="✍️ Вписать смены задним числом"),
    BotCommand(command="export", description="📄 Выгрузить период в таблицу"),
    BotCommand(command="achievements", description="🏅 Достижения и звание"),
    BotCommand(command="settings", description="⚙️ Уведомления и тихие часы"),
    BotCommand(command="mute", description="🔇 Помолчать до конца дня"),
    BotCommand(command="help", description="❓ Справка"),
]


def build_dispatcher() -> Dispatcher:
    """Собрать Dispatcher: роутеры и промежуточные слои.

    Отдельной функцией, чтобы тесты маршрутизации проверяли ту же сборку,
    что работает в бою: порядок роутеров тут значим — апдейт достаётся первому
    роутеру, чей хендлер его взял, а не самому конкретному фильтру.
    """
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(middlewares.RememberUser())
    dp.message.middleware(middlewares.FreshMenu())
    # Админка первой: её шаги ввода (пароль, текст рассылки) должны опережать
    # общий разбор текста в handlers.
    dp.include_router(handlers_admin.router)
    dp.include_router(handlers.router)
    return dp


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
    dp = build_dispatcher()

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
