"""Сервис миграции подписок из старой панели 3x-ui в текущую систему."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud.server_squad import get_all_server_squads
from app.database.crud.subscription import create_paid_subscription, extend_subscription
from app.database.models import Subscription, SubscriptionStatus, Tariff, User, XUiMigration
from app.services.subscription_service import SubscriptionService
from app.utils.x_ui_migration import (
    XUiClient,
    extract_uuid_from_vless,
    find_client_by_uuid,
)


logger = structlog.get_logger(__name__)


class XUiMigrationError(Exception):
    """Ошибка миграции 3x-ui подписки."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class XUiMigrationResult:
    subscription: Subscription
    tariff: Tariff
    old_client: XUiClient
    apology_days: int
    was_unlimited: bool


# +1 месяц «извинения»
DEFAULT_APOLOGY_DAYS = 30

# Период по умолчанию для тарифа «Стандартный», если в period_prices нет "30"
DEFAULT_STANDARD_PERIOD_DAYS = 30

# Период по умолчанию для тарифа «Навсегда» (10 лет)
DEFAULT_FOREVER_PERIOD_DAYS = 3650


def _get_tariff_name(env_key: str, default: str) -> str:
    value = os.getenv(env_key)
    return value.strip() if value and value.strip() else default


def _apology_days() -> int:
    raw = os.getenv('X_UI_MIGRATION_APOLOGY_DAYS')
    if not raw:
        return DEFAULT_APOLOGY_DAYS
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        return DEFAULT_APOLOGY_DAYS


async def _get_tariff_by_name(db: AsyncSession, name: str) -> Tariff | None:
    query = (
        select(Tariff)
        .where(func.lower(Tariff.name) == name.lower())
        .where(Tariff.is_active.is_(True))
        .options(selectinload(Tariff.allowed_promo_groups))
    )
    result = await db.execute(query)
    return result.scalars().first()


def _pick_period_days(tariff: Tariff, fallback: int) -> int:
    """Берёт минимальный доступный период тарифа, иначе fallback."""
    periods = tariff.get_available_periods() if tariff else []
    if periods:
        return periods[0]
    return fallback


async def _get_existing_migration(db: AsyncSession, old_uuid: str) -> XUiMigration | None:
    query = select(XUiMigration).where(XUiMigration.old_uuid == old_uuid.lower())
    result = await db.execute(query)
    return result.scalars().first()


async def _resolve_existing_subscription(
    db: AsyncSession, user: User, tariff: Tariff
) -> Subscription | None:
    """Возвращает активную подписку пользователя на этом тарифе, если есть.

    Для миграции используем только точное совпадение tariff_id, чтобы не
    затирать параллельные подписки пользователя на другие тарифы.
    """
    query = (
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .where(Subscription.tariff_id == tariff.id)
        .where(
            Subscription.status.in_(
                [
                    SubscriptionStatus.ACTIVE.value,
                    SubscriptionStatus.TRIAL.value,
                    SubscriptionStatus.EXPIRED.value,
                    SubscriptionStatus.LIMITED.value,
                ]
            )
        )
        .order_by(Subscription.created_at.desc())
    )
    result = await db.execute(query)
    return result.scalars().first()


async def migrate_vless_subscription(
    db: AsyncSession,
    user: User,
    vless_url_or_uuid: str,
) -> XUiMigrationResult:
    """Выдаёт пользователю подписку по UUID из старой VLESS-ссылки.

    Бросает XUiMigrationError с кодами:
    - invalid_url: не удалось распарсить UUID
    - not_found: клиент не найден в 3x-ui БД
    - already_migrated: UUID уже использовался для миграции
    - tariff_missing: не найден нужный тариф (стандартный/навсегда)
    """
    uuid = extract_uuid_from_vless(vless_url_or_uuid)
    if not uuid:
        raise XUiMigrationError('invalid_url', 'Не удалось распознать VLESS-ссылку или UUID.')

    existing = await _get_existing_migration(db, uuid)
    if existing is not None:
        raise XUiMigrationError(
            'already_migrated',
            'Эта ссылка уже была мигрирована ранее.',
        )

    old_client = find_client_by_uuid(uuid)
    if old_client is None:
        raise XUiMigrationError(
            'not_found',
            'Подписка с таким UUID не найдена в архивных базах 3x-ui.',
        )

    is_unlimited = old_client.has_unlimited_duration

    standard_name = _get_tariff_name('X_UI_MIGRATION_STANDARD_TARIFF_NAME', 'Стандартный')
    forever_name = _get_tariff_name('X_UI_MIGRATION_FOREVER_TARIFF_NAME', 'Навсегда')

    target_name = forever_name if is_unlimited else standard_name
    tariff = await _get_tariff_by_name(db, target_name)
    if tariff is None:
        raise XUiMigrationError(
            'tariff_missing',
            f'Не удалось найти активный тариф «{target_name}».',
        )

    period_days = _pick_period_days(
        tariff,
        DEFAULT_FOREVER_PERIOD_DAYS if is_unlimited else DEFAULT_STANDARD_PERIOD_DAYS,
    )
    apology_days = _apology_days()
    total_days = period_days + apology_days

    squads = list(tariff.allowed_squads or [])
    if not squads:
        all_servers, _ = await get_all_server_squads(db, available_only=True)
        squads = [s.squad_uuid for s in all_servers if s.squad_uuid]

    existing_subscription = await _resolve_existing_subscription(db, user, tariff)

    if existing_subscription is not None:
        subscription = await extend_subscription(
            db,
            existing_subscription,
            days=total_days,
            tariff_id=tariff.id,
            traffic_limit_gb=tariff.traffic_limit_gb,
            device_limit=max(tariff.device_limit or 0, existing_subscription.device_limit or 0),
            connected_squads=squads,
        )
    else:
        subscription = await create_paid_subscription(
            db=db,
            user_id=user.id,
            duration_days=total_days,
            traffic_limit_gb=tariff.traffic_limit_gb,
            device_limit=tariff.device_limit,
            connected_squads=squads,
            tariff_id=tariff.id,
        )

    migration_record = XUiMigration(
        old_uuid=uuid.lower(),
        user_id=user.id,
        subscription_id=subscription.id,
        tariff_id=tariff.id,
        old_email=old_client.email or None,
        source_db=old_client.source_db,
        old_expiry_time_ms=old_client.expiry_time_ms,
    )
    db.add(migration_record)
    await db.commit()
    await db.refresh(subscription)

    try:
        subscription_service = SubscriptionService()
        await subscription_service.create_remnawave_user(
            db,
            subscription,
            reset_traffic=True,
            reset_reason='миграция 3x-ui',
        )
    except Exception as error:
        logger.warning(
            '⚠️ Миграция 3x-ui: не удалось обновить Remnawave для подписки',
            subscription_id=subscription.id,
            error=error,
        )
        from app.services.remnawave_retry_queue import remnawave_retry_queue

        remnawave_retry_queue.enqueue(
            subscription_id=subscription.id,
            user_id=user.id,
            action='create',
        )

    logger.info(
        '✅ 3x-ui миграция: подписка выдана пользователю',
        user_id=user.id,
        tariff_id=tariff.id,
        tariff_name=tariff.name,
        total_days=total_days,
        old_uuid=uuid,
        old_email=old_client.email,
    )

    # Возвращаем результат
    end_date = subscription.end_date
    if end_date is not None and end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)
    _ = end_date or (datetime.now(UTC) + timedelta(days=total_days))

    return XUiMigrationResult(
        subscription=subscription,
        tariff=tariff,
        old_client=old_client,
        apology_days=apology_days,
        was_unlimited=is_unlimited,
    )
