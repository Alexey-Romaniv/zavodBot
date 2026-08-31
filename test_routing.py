"""Тесты маршрутизации: чей хендлер получит сообщение.

Проверяем на настоящем Dispatcher с теми же роутерами, что в bot.py, потому что
ошибка тут возникает именно на стыке роутеров: aiogram отдаёт апдейт первому
роутеру, чей хендлер согласился его взять, а не самому конкретному фильтру.
Так и вышло, что общий разбор текста съедал пароль админки и текст рассылки.

Запуск: .venv/bin/python test_routing.py
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime

os.environ.setdefault("SHIFTBOT_DB", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SHIFTBOT_ADMIN_PASSWORD", "секрет")

from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.methods import SendMessage  # noqa: E402
from aiogram.types import Chat, Message, Update, User  # noqa: E402

import admin  # noqa: E402
import bot  # noqa: E402
import db  # noqa: E402
import handlers_admin  # noqa: E402

UID = 4242
TOKEN = "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


class FakeSession:
    """Сессия, которая никуда не ходит и запоминает исходящие вызовы."""

    def __init__(self) -> None:
        self.calls: list = []

    async def __call__(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message.model_construct(
                message_id=len(self.calls),
                date=datetime.now(),
                chat=Chat.model_construct(id=method.chat_id, type="private"),
                text=method.text,
            )
        return True

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.calls if isinstance(m, SendMessage)]

    async def close(self) -> None:
        pass

    def middleware(self, *args, **kwargs):  # Dispatcher проверяет наличие
        return None


def make_update(text: str, update_id: int = 1) -> Update:
    return Update.model_construct(
        update_id=update_id,
        message=Message.model_construct(
            message_id=update_id,
            date=datetime.now(),
            chat=Chat.model_construct(id=UID, type="private"),
            from_user=User.model_construct(id=UID, is_bot=False, first_name="Тест"),
            text=text,
        ),
    )


# Берём ту самую сборку, что работает в бою: если продублировать её здесь,
# тест перестанет замечать поломку порядка роутеров в bot.py.
# Один на весь модуль — роутер нельзя подключить к двум диспетчерам.
_DP = bot.build_dispatcher()

_UPDATE_ID = 0


def make_bot(session: FakeSession) -> Bot:
    return Bot(
        token=TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )


def fsm(bot: Bot) -> FSMContext:
    return FSMContext(
        storage=_DP.storage, key=StorageKey(bot_id=bot.id, chat_id=UID, user_id=UID)
    )


async def feed(bot: Bot, texts: list[str]) -> None:
    global _UPDATE_ID
    for text in texts:
        _UPDATE_ID += 1
        await _DP.feed_update(bot, make_update(text, update_id=_UPDATE_ID))


def run(texts: list[str], state=None) -> FakeSession:
    """Прогнать сообщения через настоящий Dispatcher и вернуть, что бот ответил."""
    db.init()
    db.ensure_user(UID)

    async def main() -> FakeSession:
        session = FakeSession()
        bot = make_bot(session)
        await fsm(bot).set_state(state)      # чистое состояние между тестами
        await feed(bot, texts)
        await bot.session.close()
        return session

    return asyncio.run(main())


# --- Админка -------------------------------------------------------------

def test_broadcast_text_reaches_admin_handler():
    """Текст рассылки не должен попадать в общий разбор текста.

    Раньше «Настя, иди поспи» уходило в on_loose_text, и админ получал
    «я работаю кнопками» вместо превью рассылки.
    """
    db.init()
    db.revoke_admin(UID)
    admin.login(UID)
    session = run(["Настя, иди поспи"], state=handlers_admin.AdminFlow.broadcast)
    answers = "\n".join(session.texts)
    assert "кнопками" not in answers, f"текст рассылки перехватил общий разбор:\n{answers}"
    assert "Настя, иди поспи" in answers, f"нет превью рассылки:\n{answers}"
    assert "Отправить" in answers or "получател" in answers.lower(), answers


def test_admin_password_is_not_parsed_as_shifts():
    """Пароль, введённый отдельным сообщением, должен дойти до админки.

    Иначе он не только не сработает, но и останется висеть в чате: удаляет
    сообщение с паролем именно хендлер админки.
    """
    db.init()
    db.revoke_admin(UID)
    session = run(["/admin", "секрет"])
    answers = "\n".join(session.texts)
    assert "кнопками" not in answers, f"пароль перехватил общий разбор:\n{answers}"
    assert admin.is_admin(UID), f"вход не выполнен:\n{answers}"


# --- Обычный пользователь -------------------------------------------------

def test_loose_text_still_parses_shifts():
    """Починка не должна отнять разбор «5.08 1» у обычного текста."""
    db.init()
    db.revoke_admin(UID)
    session = run(["5.08 1"])
    answers = "\n".join(session.texts)
    assert "Записал" in answers, answers


def test_unknown_text_gets_hint():
    db.init()
    session = run(["абракадабра"])
    answers = "\n".join(session.texts)
    assert "кнопками" in answers, answers


def test_menu_is_delivered_once_through_dispatcher():
    """FreshMenu досылает клавиатуру, но только один раз."""
    db.init()
    db.set_menu_version(UID, 0)
    session = run(["абракадабра", "ещё раз"])
    assert sum(1 for t in session.texts if "Кнопки обновились" in t) == 1, session.texts


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:
                failed += 1
                print(f"ERR  {name}: {type(exc).__name__}: {exc}")
    print("\n" + ("все тесты прошли" if not failed else f"провалено: {failed}"))
    raise SystemExit(1 if failed else 0)
