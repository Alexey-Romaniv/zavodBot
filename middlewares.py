"""Промежуточные слои aiogram."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

import db

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
