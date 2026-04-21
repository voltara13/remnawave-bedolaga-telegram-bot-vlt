"""Миграция подписок из старой 3x-ui панели (эндпоинты личного кабинета)."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.services.x_ui_migration_service import (
    XUiMigrationError,
    migrate_vless_subscription,
)

from ..dependencies import get_cabinet_db, get_current_cabinet_user


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/x-ui-migration', tags=['Cabinet 3x-ui Migration'])


class XUiMigrateRequest(BaseModel):
    """Запрос на перенос подписки из 3x-ui по VLESS-ссылке или UUID."""

    link: str = Field(..., min_length=1, max_length=4096, description='VLESS-ссылка или UUID клиента из старой панели')


class XUiMigrateResponse(BaseModel):
    success: bool
    tariff_id: int
    tariff_name: str
    subscription_id: int
    apology_days: int
    was_unlimited: bool
    days_left: int
    expires_at: str | None


_ERROR_HTTP_STATUSES = {
    'invalid_url': status.HTTP_400_BAD_REQUEST,
    'not_found': status.HTTP_404_NOT_FOUND,
    'expired': status.HTTP_409_CONFLICT,
    'already_migrated': status.HTTP_409_CONFLICT,
    'tariff_missing': status.HTTP_503_SERVICE_UNAVAILABLE,
}


@router.post('/migrate', response_model=XUiMigrateResponse)
async def migrate(
    request: XUiMigrateRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> XUiMigrateResponse:
    """Перенести подписку из 3x-ui в текущую систему."""
    if not settings.is_x_ui_migration_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='3x-ui migration is disabled',
        )

    try:
        result = await migrate_vless_subscription(db, user, request.link.strip())
    except XUiMigrationError as error:
        http_status = _ERROR_HTTP_STATUSES.get(error.code, status.HTTP_400_BAD_REQUEST)
        raise HTTPException(status_code=http_status, detail={'code': error.code, 'message': error.message})
    except Exception:
        logger.exception('Cabinet 3x-ui migration failed', user_id=user.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Internal error while migrating subscription',
        )

    subscription = result.subscription
    expires_at = subscription.end_date.isoformat() if subscription.end_date else None
    days_left = int(getattr(subscription, 'days_left', 0) or 0)

    return XUiMigrateResponse(
        success=True,
        tariff_id=result.tariff.id,
        tariff_name=result.tariff.name or '',
        subscription_id=subscription.id,
        apology_days=result.apology_days,
        was_unlimited=result.was_unlimited,
        days_left=days_left,
        expires_at=expires_at,
    )
