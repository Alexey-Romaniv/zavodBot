"""Хендлеры админки: вход по паролю, статистика по всем, рассылка.

Отдельный роутер — чтобы обычные команды бота и админские не путались.
Подключается в bot.py после основного, поэтому /cancel и /start из handlers.py
работают и внутри админских состояний.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import admin
import config
import db
import keyboards as kb

log = logging.getLogger(__name__)

router = Router(name="admin")


class AdminFlow(StatesGroup):
    password = State()
    broadcast = State()


MENU_TEXT = (
    "<b>🛠 Админка</b>\n\n"
    "Статистика по всем пользователям и рассылка.\n"
    "<i>Вход действует {left} мин., потом пароль спросят снова.</i>"
)

ASK_PASSWORD = (
    "🔒 <b>Вход в админку</b>\n\n"
    "Пришли пароль одним сообщением — я его сразу удалю из чата.\n"
    "Отмена — /cancel"
)


def _menu_text(user_id: int) -> str:
    return MENU_TEXT.format(left=max(1, int(admin.session_left(user_id).total_seconds() // 60)))


async def _drop(message: Message) -> None:
    """Убрать из чата сообщение с паролем."""
    try:
        await message.delete()
    except Exception:  # нет прав или сообщение уже удалено — не повод падать
        log.debug("Не смог удалить сообщение с паролем", exc_info=True)


async def _open_menu(message: Message, user_id: int) -> None:
    await message.answer(_menu_text(user_id), reply_markup=kb.admin_menu())


async def _try_password(message: Message, state: FSMContext, raw: str) -> None:
    """Проверить пароль. Успех — открываем меню, промах — считаем попытки."""
    user_id = message.from_user.id
    left = admin.lock_left(user_id)
    if left is not None:
        await state.clear()
        await message.answer(
            f"⛔️ Слишком много попыток. Попробуй через {int(left.total_seconds() // 60) + 1} мин."
        )
        return
    if admin.password_ok(raw):
        await state.clear()
        admin.login(user_id)
        await _open_menu(message, user_id)
        return
    attempts_left = admin.note_failure(user_id)
    if attempts_left == 0:
        await state.clear()
        minutes = int(config.ADMIN_LOCKOUT.total_seconds() // 60)
        await message.answer(f"⛔️ Пароль неверный. Вход заблокирован на {minutes} мин.")
        return
    await state.set_state(AdminFlow.password)
    await message.answer(
        f"❌ Пароль неверный. Осталось попыток: {attempts_left}.\nОтмена — /cancel"
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    user_id = message.from_user.id
    db.ensure_user(user_id)

    if not admin.configured():
        await message.answer(
            "Админка выключена: не задан <code>SHIFTBOT_ADMIN_PASSWORD</code> в .env."
        )
        return
    if not admin.allowed(user_id):
        log.warning("Админка: попытка входа не из списка, user_id=%s", user_id)
        await message.answer("Нет доступа.")
        return
    if admin.is_admin(user_id):
        await _open_menu(message, user_id)
        return

    if command.args:  # пароль пришёл прямо в команде: /admin мойпароль
        await _drop(message)
        await _try_password(message, state, command.args)
        return

    await state.set_state(AdminFlow.password)
    await message.answer(ASK_PASSWORD)


@router.message(AdminFlow.password, F.text)
async def on_password(message: Message, state: FSMContext) -> None:
    raw = message.text
    await _drop(message)
    await _try_password(message, state, raw)


# --- Меню -----------------------------------------------------------------

async def _guard(call: CallbackQuery) -> bool:
    """Сессия могла истечь, пока сообщение висело в чате."""
    if admin.is_admin(call.from_user.id):
        return True
    await call.message.edit_text("🔒 Сессия админки истекла. Войти заново: /admin")
    await call.answer("Нужен вход", show_alert=True)
    return False


@router.callback_query(kb.AdminCb.filter(F.action == "menu"))
async def cb_admin_menu(call: CallbackQuery, state: FSMContext) -> None:
    if not await _guard(call):
        return
    await state.clear()
    await call.message.edit_text(_menu_text(call.from_user.id), reply_markup=kb.admin_menu())
    await call.answer()


@router.callback_query(kb.AdminCb.filter(F.action == "stats"))
async def cb_admin_stats(call: CallbackQuery) -> None:
    if not await _guard(call):
        return
    await call.message.edit_text(admin.overview_report(), reply_markup=kb.admin_back())
    await call.answer()


@router.callback_query(kb.AdminCb.filter(F.action == "users"))
async def cb_admin_users(call: CallbackQuery) -> None:
    if not await _guard(call):
        return
    users = admin.collect()
    await call.message.edit_text(
        admin.users_report(users), reply_markup=kb.admin_users(users)
    )
    await call.answer()


@router.callback_query(kb.AdminCb.filter(F.action == "user"))
async def cb_admin_user(call: CallbackQuery, callback_data: kb.AdminCb) -> None:
    if not await _guard(call):
        return
    if not callback_data.arg.isdigit():
        await call.answer("Не тот пользователь", show_alert=True)
        return
    await call.message.edit_text(
        admin.user_card(int(callback_data.arg)), reply_markup=kb.admin_user_card()
    )
    await call.answer()


@router.callback_query(kb.AdminCb.filter(F.action == "logout"))
async def cb_admin_logout(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    admin.logout(call.from_user.id)
    await call.message.edit_text("🚪 Вышел из админки. Войти снова: /admin")
    await call.answer("Вышел")


@router.callback_query(kb.AdminCb.filter(F.action == "close"))
async def cb_admin_close(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Ок. Админка: /admin")
    await call.answer()


# --- Рассылка -------------------------------------------------------------

@router.callback_query(kb.AdminCb.filter(F.action == "cast"))
async def cb_broadcast_ask(call: CallbackQuery, state: FSMContext) -> None:
    if not await _guard(call):
        return
    count = len(db.all_user_ids())
    if not count:
        await call.answer("Некому рассылать — пользователей нет", show_alert=True)
        return
    await state.set_state(AdminFlow.broadcast)
    await call.message.edit_text(
        f"<b>📣 Рассылка</b>\n\n"
        f"Напиши текст — покажу, как он будет выглядеть, и спрошу подтверждение.\n"
        f"Получателей сейчас: <b>{count}</b> (ты в их числе).\n"
        f"Форматирование делай средствами Telegram — жирный, курсив, ссылки сохранятся.\n\n"
        f"Отмена — /cancel",
        reply_markup=kb.admin_back(),
    )
    await call.answer()


@router.message(AdminFlow.broadcast, F.text)
async def on_broadcast_text(message: Message, state: FSMContext) -> None:
    if not admin.is_admin(message.from_user.id):
        await state.clear()
        await message.answer("🔒 Сессия админки истекла. Войти заново: /admin")
        return
    text = message.html_text
    recipients = db.all_user_ids()
    await state.update_data(text=text)
    await message.answer(
        "<b>Так это увидят получатели:</b>\n\n"
        "———\n"
        f"{text}\n"
        "———\n\n"
        f"Отправить <b>{len(recipients)}</b> получателям?",
        reply_markup=kb.admin_broadcast(len(recipients)),
    )


@router.callback_query(kb.AdminCb.filter(F.action == "cast_go"))
async def cb_broadcast_send(call: CallbackQuery, state: FSMContext) -> None:
    if not await _guard(call):
        return
    data = await state.get_data()
    text = data.get("text")
    await state.clear()
    if not text:
        await call.message.edit_text(
            "Текст рассылки потерялся (бот перезапускался). Начни заново.",
            reply_markup=kb.admin_back(),
        )
        await call.answer()
        return

    await call.message.edit_text("📣 Рассылаю…")
    await call.answer()
    sent, blocked, failed = await _broadcast(call.bot, text)
    log.info("Рассылка от %s: доставлено %s, блок %s, ошибок %s",
             call.from_user.id, sent, blocked, failed)
    await call.message.edit_text(
        "<b>📣 Рассылка закончена</b>\n\n"
        f"✅ Доставлено: <b>{sent}</b>\n"
        f"🚫 Заблокировали бота: {blocked}\n"
        f"⚠️ Не доставлено: {failed}",
        reply_markup=kb.admin_back(),
    )


async def _broadcast(bot, text: str) -> tuple[int, int, int]:
    """Разослать текст всем. Один сбой не должен ронять остальную рассылку."""
    sent = blocked = failed = 0
    for user_id in db.all_user_ids():
        try:
            try:
                await bot.send_message(user_id, text)
            except TelegramRetryAfter as e:  # упёрлись в лимит — подождём и повторим
                await asyncio.sleep(e.retry_after)
                await bot.send_message(user_id, text)
        except TelegramForbiddenError:
            blocked += 1
        except Exception:
            log.exception("Рассылка: не смог отправить пользователю %s", user_id)
            failed += 1
        else:
            sent += 1
        await asyncio.sleep(config.BROADCAST_DELAY)
    return sent, blocked, failed
