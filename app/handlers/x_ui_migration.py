"""Миграция подписок из старой панели 3x-ui по VLESS-ссылке."""

from __future__ import annotations

import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.localization.texts import get_texts
from app.services.x_ui_migration_service import (
    XUiMigrationError,
    migrate_vless_subscription,
)
from app.states import XUiMigrationStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


ENTRY_CALLBACK = 'x_ui_migration_start'


def build_entry_button(language: str = 'ru') -> InlineKeyboardButton:
    """Кнопка для главного меню."""
    texts = get_texts(language)
    text = texts.t('X_UI_MIGRATION_BUTTON', '🔁 Перенести подписку 3x-ui')
    return InlineKeyboardButton(text=text, callback_data=ENTRY_CALLBACK)


def _prompt_text(texts) -> str:
    return texts.t(
        'X_UI_MIGRATION_PROMPT',
        (
            '🔁 <b>Перенос подписки из 3x-ui</b>\n\n'
            'Отправьте VLESS-ссылку из старой панели. Мы найдём вашу подписку '
            'и выдадим аналог в текущей системе.\n\n'
            'Пример:\n'
            '<code>vless://UUID@host:port?...</code>'
        ),
    )


@error_handler
async def show_x_ui_migration_menu(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    prompt = _prompt_text(texts)
    keyboard = get_back_keyboard(db_user.language)

    if isinstance(callback.message, InaccessibleMessage):
        await callback.message.answer(prompt, reply_markup=keyboard)
    else:
        try:
            await callback.message.edit_text(prompt, reply_markup=keyboard)
        except TelegramBadRequest as error:
            if 'there is no text in the message to edit' in str(error).lower():
                await callback.message.answer(prompt, reply_markup=keyboard)
            else:
                raise

    await state.set_state(XUiMigrationStates.waiting_for_vless_link)
    await callback.answer()


def _format_success(texts, *, tariff_name: str, was_unlimited: bool, apology_days: int, days_left: int) -> str:
    lines = [
        texts.t(
            'X_UI_MIGRATION_SUCCESS_TITLE',
            '✅ <b>Подписка перенесена!</b>',
        ),
        '',
        texts.t(
            'X_UI_MIGRATION_SUCCESS_TARIFF',
            '📦 Тариф: <b>{tariff}</b>',
        ).format(tariff=html.escape(tariff_name)),
    ]
    if was_unlimited:
        lines.append(
            texts.t(
                'X_UI_MIGRATION_SUCCESS_FOREVER',
                '♾️ Старый срок: без ограничения',
            )
        )
    if apology_days > 0:
        lines.append(
            texts.t(
                'X_UI_MIGRATION_SUCCESS_APOLOGY',
                '🎁 Бонус-извинение: +{days} дн.',
            ).format(days=apology_days)
        )
    if days_left:
        lines.append(
            texts.t(
                'X_UI_MIGRATION_SUCCESS_DAYS_LEFT',
                '⏳ До окончания: {days} дн.',
            ).format(days=days_left)
        )
    lines.append('')
    lines.append(
        texts.t(
            'X_UI_MIGRATION_SUCCESS_HINT',
            'Откройте «Моя подписка», чтобы получить ссылку для подключения.',
        )
    )
    return '\n'.join(lines)


_ERROR_MESSAGES = {
    'invalid_url': 'X_UI_MIGRATION_ERR_INVALID',
    'not_found': 'X_UI_MIGRATION_ERR_NOT_FOUND',
    'already_migrated': 'X_UI_MIGRATION_ERR_ALREADY',
    'tariff_missing': 'X_UI_MIGRATION_ERR_TARIFF',
}

_ERROR_DEFAULTS = {
    'invalid_url': '❌ Не удалось распознать VLESS-ссылку. Проверьте формат и попробуйте снова.',
    'not_found': '❌ Подписка с таким UUID не найдена в архивах 3x-ui.',
    'already_migrated': 'ℹ️ Эта ссылка уже была мигрирована ранее.',
    'tariff_missing': '❌ Не удалось найти подходящий тариф. Обратитесь в поддержку.',
}


def _format_error(texts, code: str, fallback: str) -> str:
    key = _ERROR_MESSAGES.get(code)
    default = _ERROR_DEFAULTS.get(code, fallback)
    if key:
        return texts.t(key, default)
    return default


@error_handler
async def process_vless_link(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    back_kb = get_back_keyboard(db_user.language)

    raw = (message.text or '').strip()
    if not raw:
        await message.answer(
            texts.t(
                'X_UI_MIGRATION_EMPTY',
                '❌ Отправьте VLESS-ссылку текстом.',
            ),
            reply_markup=back_kb,
        )
        return

    try:
        result = await migrate_vless_subscription(db, db_user, raw)
    except XUiMigrationError as error:
        await message.answer(_format_error(texts, error.code, error.message), reply_markup=back_kb)
        if error.code != 'invalid_url':
            await state.clear()
        return
    except Exception as error:
        logger.exception('Неожиданная ошибка миграции 3x-ui', user_id=db_user.id, error=error)
        await message.answer(
            texts.t(
                'X_UI_MIGRATION_ERR_INTERNAL',
                '❌ Произошла ошибка при переносе подписки. Попробуйте позже или обратитесь в поддержку.',
            ),
            reply_markup=back_kb,
        )
        await state.clear()
        return

    tariff_name = result.tariff.name or ''
    days_left = getattr(result.subscription, 'days_left', 0) or 0
    text = _format_success(
        texts,
        tariff_name=tariff_name,
        was_unlimited=result.was_unlimited,
        apology_days=result.apology_days,
        days_left=days_left,
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('MY_SUBSCRIPTION_BUTTON', '📱 Моя подписка'),
                    callback_data='menu_subscription',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '🏠 Главное меню'),
                    callback_data='back_to_menu',
                )
            ],
        ]
    )

    await message.answer(text, reply_markup=keyboard)
    await state.clear()


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_x_ui_migration_menu, F.data == ENTRY_CALLBACK)
    dp.message.register(process_vless_link, XUiMigrationStates.waiting_for_vless_link)
