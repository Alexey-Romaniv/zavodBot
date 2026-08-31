"""Промежуточные слои aiogram."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject, User

import db
import keyboards as kb

log = logging.getLogger(__name__)


class RememberUser(BaseMiddleware):
    """Запоминает ник и имя отправителя перед тем, как отдать апдейт хендлеру.

    Так админке есть что показать вместо голого user_id. Одно место вместо
    правки каждого db.ensure_user, и профиль всегда свежий — ник в Telegram
    пользователь может сменить когда угодно.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is not None and not user.is_bot:
            try:
                db.remember_profile(user.id, user.username, user.first_name, user.last_name)
            except Exception:  # запись профиля не повод терять сам апдейт
                log.exception("Не смог запомнить профиль пользователя %s", user.id)
        return await handler(event, data)


class FreshMenu(BaseMiddleware):
    """Досдаёт главное меню тем, у кого оно устарело.

    Reply-клавиатура хранится в клиенте Telegram: пока бот не пришлёт сообщение
    с новой, у пользователя остаются кнопки той версии, что он получил когда-то.
    После правки меню это значит «новых кнопок никто не увидит, пока не наберёт
    /start» — ровно то, чего мы хотели избежать. Поэтому сравниваем версию меню,
    которую человек уже получил, с текущей и один раз присылаем свежую.

    Работает после хендлера, чтобы клавиатура пришла последним сообщением
    и не перебивала ответ на само действие.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        result = await handler(event, data)

        user: User | None = data.get("event_from_user")
        message = event if isinstance(event, Message) else None
        if user is None or user.is_bot or message is None:
            return result
        try:
            if db.menu_version(user.id) >= kb.MENU_VERSION:
                return result
            db.set_menu_version(user.id, kb.MENU_VERSION)
            await message.answer(
                "⌨️ Кнопки обновились — теперь тут же настройки и отчёт.",
                reply_markup=kb.main_menu(),
            )
        except Exception:  # не даём сбою досылки сломать сам ответ бота
            log.exception("Не смог обновить меню пользователю %s", user.id)
        return result
