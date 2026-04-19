"""Programmatic Alembic migration runner for bot startup."""

from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text


logger = structlog.get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJECT_ROOT / 'alembic.ini'


def _get_alembic_config() -> Config:
    """Build Alembic Config pointing at the project root."""
    from app.config import settings

    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option('sqlalchemy.url', settings.get_database_url())
    return cfg


async def _detect_db_state() -> str:
    """Detect database state: 'fresh', 'legacy', or 'managed'.

    - fresh: no tables at all — brand new database
    - legacy: has tables but no alembic_version (transition from universal_migration)
    - managed: has alembic_version — already managed by Alembic
    """
    from app.database.database import engine

    async with engine.connect() as conn:
        has_alembic = await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table('alembic_version'))
        if has_alembic:
            return 'managed'
        has_users = await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table('users'))
        return 'legacy' if has_users else 'fresh'


_INITIAL_REVISION = '0001'
_LEGACY_PAYMENT_BRANCH_STAMPS = {
    '0058': '0061',
    '0059': '0062',
    '0060': '0063',
}


async def _bootstrap_fresh_db() -> None:
    """Bootstrap a fresh database: create all tables from models and stamp at head.

    On a fresh DB, running all migrations sequentially would fail because
    migration 0001 uses Base.metadata.create_all() which creates ALL tables
    from the current models.py (including columns/constraints/indexes added
    by later migrations), and then those later migrations try to re-create
    the same objects.  Instead, we create the full schema directly and stamp
    the migration history at HEAD so Alembic considers all migrations applied.
    """
    from app.database.database import engine
    from app.database.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info('Свежая БД: все таблицы созданы из моделей')


async def _repair_legacy_payment_branch_revision() -> None:
    """Restamp legacy payment-provider revisions that used conflicting IDs.

    Before the upstream merge on 2026-04-19, payment-provider migrations used
    revision IDs 0058/0059/0060. Upstream introduced different migrations with
    the same IDs, which makes Alembic's graph ambiguous after the merge.

    If a database was already stamped with the legacy payment branch, detect it
    by schema markers and restamp it to the new unique payment branch IDs so the
    remaining upstream migrations can be applied normally.
    """
    from app.database.database import engine

    async with engine.connect() as conn:
        repair = await conn.run_sync(_detect_legacy_payment_branch_repair)

    if repair is None:
        return

    current_revision, target_revision = repair
    logger.warning(
        'Обнаружена устаревшая платёжная ветка Alembic с конфликтующим revision ID — выполняется repair stamp',
        current_revision=current_revision,
        target_revision=target_revision,
    )
    await _stamp_alembic_revision(target_revision)


def _detect_legacy_payment_branch_repair(sync_conn) -> tuple[str, str] | None:
    inspector = inspect(sync_conn)
    if not inspector.has_table('alembic_version'):
        return None

    current_revisions = list(sync_conn.execute(text('SELECT version_num FROM alembic_version')).scalars())
    if len(current_revisions) != 1:
        return None

    current_revision = current_revisions[0]
    if current_revision not in _LEGACY_PAYMENT_BRANCH_STAMPS:
        return None

    has_personal_data_consents = inspector.has_table('personal_data_consents')
    has_paypear_payments = inspector.has_table('paypear_payments')
    has_rollypay_payments = inspector.has_table('rollypay_payments')
    has_aurapay_payments = inspector.has_table('aurapay_payments')

    subscription_columns = set()
    if inspector.has_table('subscriptions'):
        subscription_columns = {column['name'] for column in inspector.get_columns('subscriptions')}
    has_subscription_name = 'name' in subscription_columns

    if current_revision == '0058' and has_paypear_payments and not has_personal_data_consents:
        return current_revision, _LEGACY_PAYMENT_BRANCH_STAMPS[current_revision]

    if current_revision == '0059' and has_rollypay_payments and not has_personal_data_consents:
        return current_revision, _LEGACY_PAYMENT_BRANCH_STAMPS[current_revision]

    if current_revision == '0060' and has_aurapay_payments and not has_subscription_name:
        return current_revision, _LEGACY_PAYMENT_BRANCH_STAMPS[current_revision]

    return None


async def run_alembic_upgrade() -> None:
    """Run ``alembic upgrade head``, handling fresh and legacy databases."""
    import asyncio

    db_state = await _detect_db_state()

    if db_state == 'fresh':
        logger.warning('Обнаружена пустая БД — создание схемы из моделей + stamp head')
        await _bootstrap_fresh_db()
        await _stamp_alembic_revision('head')
        return

    if db_state == 'legacy':
        logger.warning(
            'Обнаружена существующая БД без alembic_version — автоматический stamp 0001 (переход с universal_migration)'
        )
        await _stamp_alembic_revision(_INITIAL_REVISION)
    elif db_state == 'managed':
        await _repair_legacy_payment_branch_revision()

    cfg = _get_alembic_config()
    loop = asyncio.get_running_loop()
    # run_in_executor offloads to a thread where env.py can safely
    # call asyncio.run() to create its own event loop.
    await loop.run_in_executor(None, command.upgrade, cfg, 'head')
    logger.info('Alembic миграции применены')


async def stamp_alembic_head() -> None:
    """Stamp the DB as being at head without running migrations (for existing DBs)."""
    await _stamp_alembic_revision('head')


async def _stamp_alembic_revision(revision: str) -> None:
    """Stamp the DB at a specific revision without running migrations."""
    import asyncio

    cfg = _get_alembic_config()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, command.stamp, cfg, revision)
    logger.info('Alembic: база отмечена как актуальная', revision=revision)
