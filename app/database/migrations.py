"""Programmatic Alembic migration runner for bot startup."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import structlog
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect


logger = structlog.get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_UPSTREAM_ALEMBIC_INI = _PROJECT_ROOT / 'alembic.ini'
_CUSTOM_ALEMBIC_INI = _PROJECT_ROOT / 'alembic_custom.ini'

_UPSTREAM_VERSION_TABLE = 'alembic_version'
_CUSTOM_VERSION_TABLE = 'alembic_version_custom'
_UPSTREAM_INITIAL_REVISION = '0001'
_UPSTREAM_BASE_REVISION_BEFORE_SPLIT = '0057'
_UPSTREAM_PERSONAL_DATA_CONSENTS_REVISION = '0058'
_UPSTREAM_DROP_INDEX_REVISION = '0059'
_UPSTREAM_SUBSCRIPTION_NAME_REVISION = '0060'
_CUSTOM_PAYPEAR_REVISION = 'vlt_0001'
_CUSTOM_ROLLYPAY_REVISION = 'vlt_0002'
_CUSTOM_AURAPAY_REVISION = 'vlt_0003'

_CUSTOM_SCHEMA_REVISIONS = (
    ('aurapay_payments', _CUSTOM_AURAPAY_REVISION),
    ('rollypay_payments', _CUSTOM_ROLLYPAY_REVISION),
    ('paypear_payments', _CUSTOM_PAYPEAR_REVISION),
)
_SPLIT_TRANSITION_SOURCE_REVISIONS = {'0061', '0062', '0063', '0064'}
_AMBIGUOUS_SHARED_REVISIONS = {'0058', '0059', '0060'}


def _get_alembic_config(ini_path: Path) -> Config:
    """Build Alembic Config pointing at the project root."""
    from app.config import settings

    cfg = Config(str(ini_path))
    cfg.set_main_option('sqlalchemy.url', settings.get_database_url())
    return cfg


async def _run_alembic_command(cfg: Config, fn: Callable[..., Any], *args: str) -> None:
    import asyncio

    loop = asyncio.get_running_loop()
    # run_in_executor offloads to a thread where env.py can safely
    # call asyncio.run() to create its own event loop.
    await loop.run_in_executor(None, fn, cfg, *args)


async def _detect_db_state() -> str:
    """Detect database state: 'fresh', 'legacy', or 'managed'.

    - fresh: no tables at all — brand new database
    - legacy: has tables but no alembic_version (transition from universal_migration)
    - managed: has alembic_version — already managed by Alembic
    """
    from app.database.database import engine

    async with engine.connect() as conn:
        has_alembic = await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table(_UPSTREAM_VERSION_TABLE))
        if has_alembic:
            return 'managed'
        has_users = await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table('users'))
        return 'legacy' if has_users else 'fresh'


async def _bootstrap_fresh_db() -> None:
    """Bootstrap a fresh database and stamp both migration histories at head."""
    from app.database.database import engine
    from app.database.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info('Свежая БД: все таблицы созданы из моделей')


def _build_version_table(table_name: str) -> sa.Table:
    return sa.Table(
        table_name,
        sa.MetaData(),
        sa.Column('version_num', sa.String(32), nullable=False, primary_key=True),
    )


def _read_version_table_revisions(sync_conn, table_name: str) -> list[str]:
    inspector = inspect(sync_conn)
    if not inspector.has_table(table_name):
        return []

    version_table = _build_version_table(table_name)
    return list(sync_conn.execute(sa.select(version_table.c.version_num)).scalars())


def _set_version_table_revision(sync_conn, table_name: str, revision: str) -> None:
    version_table = _build_version_table(table_name)
    version_table.create(bind=sync_conn, checkfirst=True)
    sync_conn.execute(sa.delete(version_table))
    sync_conn.execute(sa.insert(version_table).values(version_num=revision))


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    if not inspector.has_table(table_name):
        return False
    return any(index['name'] == index_name for index in inspector.get_indexes(table_name))


def _infer_upstream_schema_revision(sync_conn) -> str:
    inspector = inspect(sync_conn)

    if inspector.has_table('subscriptions'):
        subscription_columns = {column['name'] for column in inspector.get_columns('subscriptions')}
        if 'name' in subscription_columns:
            return _UPSTREAM_SUBSCRIPTION_NAME_REVISION

    if inspector.has_table('personal_data_consents'):
        if _has_index(inspector, 'subscriptions', 'uq_subscriptions_user_tariff_active'):
            return _UPSTREAM_PERSONAL_DATA_CONSENTS_REVISION
        return _UPSTREAM_DROP_INDEX_REVISION

    return _UPSTREAM_BASE_REVISION_BEFORE_SPLIT


def _infer_custom_schema_revision(sync_conn) -> str | None:
    inspector = inspect(sync_conn)
    for table_name, revision in _CUSTOM_SCHEMA_REVISIONS:
        if inspector.has_table(table_name):
            return revision
    return None


def _needs_upstream_version_realignment(
    main_revisions: list[str],
    inferred_upstream_revision: str,
    inferred_custom_revision: str | None,
) -> bool:
    if not main_revisions:
        return False

    if len(main_revisions) != 1:
        return True

    current_revision = main_revisions[0]
    if current_revision in _SPLIT_TRANSITION_SOURCE_REVISIONS:
        return True

    if (
        current_revision in _AMBIGUOUS_SHARED_REVISIONS
        and inferred_custom_revision is not None
        and current_revision != inferred_upstream_revision
    ):
        return True

    return False


def _needs_custom_version_realignment(
    custom_revisions: list[str],
    inferred_custom_revision: str | None,
) -> bool:
    return inferred_custom_revision is not None and custom_revisions != [inferred_custom_revision]


async def _transition_to_split_histories() -> None:
    """Split old combined migration history into upstream + custom version tables."""
    from app.database.database import engine

    async with engine.begin() as conn:
        transition = await conn.run_sync(_transition_to_split_histories_sync)

    if transition is None:
        return

    logger.warning(
        'Обнаружена объединённая история Alembic — выполняется переход на split histories',
        **transition,
    )


def _transition_to_split_histories_sync(sync_conn) -> dict[str, object] | None:
    inspector = inspect(sync_conn)
    if not inspector.has_table(_UPSTREAM_VERSION_TABLE):
        return None

    main_revisions = _read_version_table_revisions(sync_conn, _UPSTREAM_VERSION_TABLE)
    custom_revisions = _read_version_table_revisions(sync_conn, _CUSTOM_VERSION_TABLE)
    inferred_upstream_revision = _infer_upstream_schema_revision(sync_conn)
    inferred_custom_revision = _infer_custom_schema_revision(sync_conn)

    needs_main_realignment = _needs_upstream_version_realignment(
        main_revisions,
        inferred_upstream_revision,
        inferred_custom_revision,
    )
    needs_custom_realignment = _needs_custom_version_realignment(
        custom_revisions,
        inferred_custom_revision,
    )
    if not needs_main_realignment and not needs_custom_realignment:
        return None

    if needs_main_realignment:
        _set_version_table_revision(sync_conn, _UPSTREAM_VERSION_TABLE, inferred_upstream_revision)

    if needs_custom_realignment and inferred_custom_revision is not None:
        _set_version_table_revision(sync_conn, _CUSTOM_VERSION_TABLE, inferred_custom_revision)

    return {
        'upstream_before': ', '.join(main_revisions) if main_revisions else 'none',
        'upstream_after': inferred_upstream_revision,
        'custom_before': ', '.join(custom_revisions) if custom_revisions else 'none',
        'custom_after': inferred_custom_revision or 'none',
    }


async def run_alembic_upgrade() -> None:
    """Run upstream and custom Alembic histories with split-version compatibility."""
    upstream_cfg = _get_alembic_config(_UPSTREAM_ALEMBIC_INI)
    custom_cfg = _get_alembic_config(_CUSTOM_ALEMBIC_INI)
    db_state = await _detect_db_state()

    if db_state == 'fresh':
        logger.warning('Обнаружена пустая БД — создание схемы из моделей + stamp head для upstream/custom')
        await _bootstrap_fresh_db()
        await _stamp_alembic_revision(upstream_cfg, 'head')
        await _stamp_alembic_revision(custom_cfg, 'head')
        return

    if db_state == 'legacy':
        logger.warning(
            'Обнаружена существующая БД без alembic_version — автоматический stamp 0001 (переход с universal_migration)'
        )
        await _stamp_alembic_revision(upstream_cfg, _UPSTREAM_INITIAL_REVISION)

    await _transition_to_split_histories()
    await _run_alembic_command(upstream_cfg, command.upgrade, 'head')
    logger.info('Alembic upstream миграции применены')
    await _run_alembic_command(custom_cfg, command.upgrade, 'head')
    logger.info('Alembic custom миграции применены')


async def stamp_alembic_head() -> None:
    """Stamp the DB as being at head in both migration histories."""
    upstream_cfg = _get_alembic_config(_UPSTREAM_ALEMBIC_INI)
    custom_cfg = _get_alembic_config(_CUSTOM_ALEMBIC_INI)
    await _stamp_alembic_revision(upstream_cfg, 'head')
    await _stamp_alembic_revision(custom_cfg, 'head')


async def _stamp_alembic_revision(cfg: Config, revision: str) -> None:
    """Stamp the DB at a specific revision without running migrations."""
    await _run_alembic_command(cfg, command.stamp, revision)
    logger.info(
        'Alembic: база отмечена как актуальная',
        revision=revision,
        script_location=cfg.get_main_option('script_location'),
    )
