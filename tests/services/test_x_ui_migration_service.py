from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import app.services.x_ui_migration_service as migration_service
from app.config import settings
from app.utils.x_ui_migration import XUiClient


class DummyDB:
    def __init__(self):
        self.added: list[object] = []
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    def add(self, obj):
        self.added.append(obj)


async def test_migrate_vless_creates_new_subscription_for_same_tariff_in_multi_mode(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True, raising=False)

    db = DummyDB()
    user = SimpleNamespace(id=10)
    finite_expiry_ms = int((datetime.now(UTC) + timedelta(days=345)).timestamp() * 1000)
    tariff = SimpleNamespace(
        id=7,
        name='Standard',
        traffic_limit_gb=100,
        device_limit=3,
        allowed_squads=['sq-1'],
        get_available_periods=lambda: [30],
    )

    clients = {
        'uuid-1': XUiClient(
            uuid='uuid-1',
            email='first@example.com',
            expiry_time_ms=finite_expiry_ms,
            enable=True,
            total_gb=0,
            source_db='old-1.db',
            inbound_id=1,
        ),
        'uuid-2': XUiClient(
            uuid='uuid-2',
            email='second@example.com',
            expiry_time_ms=finite_expiry_ms,
            enable=True,
            total_gb=0,
            source_db='old-2.db',
            inbound_id=2,
        ),
    }

    created_calls: list[dict] = []
    created_subscriptions: list[SimpleNamespace] = []

    async def fake_create_paid_subscription(**kwargs):
        created_calls.append(kwargs)
        subscription = SimpleNamespace(
            id=len(created_calls),
            user_id=kwargs['user_id'],
            tariff_id=kwargs['tariff_id'],
            end_date=datetime.now(UTC) + timedelta(days=kwargs['duration_days']),
            name=None,
        )
        created_subscriptions.append(subscription)
        return subscription

    extend_mock = AsyncMock()
    create_remnawave_mock = AsyncMock(return_value=None)

    class DummySubscriptionService:
        async def create_remnawave_user(self, *args, **kwargs):
            return await create_remnawave_mock(*args, **kwargs)

    monkeypatch.setattr(migration_service, 'extract_uuid_from_vless', lambda value: value)
    monkeypatch.setattr(migration_service, '_get_existing_migration', AsyncMock(return_value=None))
    monkeypatch.setattr(migration_service, '_apology_days', lambda: 0)
    monkeypatch.setattr(migration_service, 'find_client_by_uuid', lambda uuid: clients.get(uuid))
    monkeypatch.setattr(migration_service, '_get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(migration_service, 'create_paid_subscription', fake_create_paid_subscription)
    monkeypatch.setattr(migration_service, 'extend_subscription', extend_mock)
    monkeypatch.setattr(migration_service, 'SubscriptionService', lambda: DummySubscriptionService())

    result_1 = await migration_service.migrate_vless_subscription(db, user, 'uuid-1')
    result_2 = await migration_service.migrate_vless_subscription(db, user, 'uuid-2')

    extend_mock.assert_not_awaited()
    assert len(created_calls) == 2
    assert [call['duration_days'] for call in created_calls] == [345, 345]
    assert result_1.subscription.id == 1
    assert result_2.subscription.id == 2
    assert created_subscriptions[0].name == 'first@example.com'
    assert created_subscriptions[1].name == 'second@example.com'
    assert [record.old_uuid for record in db.added] == ['uuid-1', 'uuid-2']
    assert [record.subscription_id for record in db.added] == [1, 2]
    assert create_remnawave_mock.await_count == 2


async def test_migrate_vless_reuses_existing_subscription_in_single_mode(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False, raising=False)

    db = DummyDB()
    user = SimpleNamespace(id=22)
    finite_expiry_ms = int((datetime.now(UTC) + timedelta(days=120)).timestamp() * 1000)
    tariff = SimpleNamespace(
        id=7,
        name='Standard',
        traffic_limit_gb=100,
        device_limit=3,
        allowed_squads=['sq-1'],
        get_available_periods=lambda: [30],
    )
    existing_subscription = SimpleNamespace(
        id=99,
        user_id=user.id,
        tariff_id=tariff.id,
        device_limit=5,
        end_date=datetime.now(UTC) + timedelta(days=15),
        name=None,
    )
    migrated_subscription = SimpleNamespace(
        id=99,
        user_id=user.id,
        tariff_id=tariff.id,
        device_limit=5,
        end_date=datetime.now(UTC) + timedelta(days=60),
        name=None,
    )

    client = XUiClient(
        uuid='uuid-1',
        email='single@example.com',
        expiry_time_ms=finite_expiry_ms,
        enable=True,
        total_gb=0,
        source_db='old.db',
        inbound_id=1,
    )

    create_paid_mock = AsyncMock()
    extend_mock = AsyncMock(return_value=migrated_subscription)
    resolve_existing_mock = AsyncMock(return_value=existing_subscription)
    create_remnawave_mock = AsyncMock(return_value=None)

    class DummySubscriptionService:
        async def create_remnawave_user(self, *args, **kwargs):
            return await create_remnawave_mock(*args, **kwargs)

    monkeypatch.setattr(migration_service, 'extract_uuid_from_vless', lambda value: value)
    monkeypatch.setattr(migration_service, '_get_existing_migration', AsyncMock(return_value=None))
    monkeypatch.setattr(migration_service, '_apology_days', lambda: 0)
    monkeypatch.setattr(migration_service, 'find_client_by_uuid', lambda uuid: client if uuid == 'uuid-1' else None)
    monkeypatch.setattr(migration_service, '_get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(migration_service, '_resolve_existing_subscription', resolve_existing_mock)
    monkeypatch.setattr(migration_service, 'create_paid_subscription', create_paid_mock)
    monkeypatch.setattr(migration_service, 'extend_subscription', extend_mock)
    monkeypatch.setattr(migration_service, 'SubscriptionService', lambda: DummySubscriptionService())

    result = await migration_service.migrate_vless_subscription(db, user, 'uuid-1')

    resolve_existing_mock.assert_awaited_once_with(db, user, tariff)
    create_paid_mock.assert_not_awaited()
    extend_mock.assert_awaited_once()
    assert extend_mock.await_args.kwargs['days'] == 120
    assert result.subscription.id == existing_subscription.id
    assert migrated_subscription.name == 'single@example.com'
    assert db.added[0].subscription_id == existing_subscription.id
    create_remnawave_mock.assert_awaited_once()


async def test_migrate_vless_rejects_expired_legacy_subscription(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True, raising=False)

    db = DummyDB()
    user = SimpleNamespace(id=30)
    tariff = SimpleNamespace(
        id=7,
        name='Standard',
        traffic_limit_gb=100,
        device_limit=3,
        allowed_squads=['sq-1'],
        get_available_periods=lambda: [30],
    )
    expired_client = XUiClient(
        uuid='uuid-expired',
        email='expired@example.com',
        expiry_time_ms=int((datetime.now(UTC) - timedelta(days=2)).timestamp() * 1000),
        enable=True,
        total_gb=0,
        source_db='old.db',
        inbound_id=1,
    )

    create_paid_mock = AsyncMock()
    expired_uuid = 'uuid-expired'

    monkeypatch.setattr(migration_service, 'extract_uuid_from_vless', lambda value: value)
    monkeypatch.setattr(migration_service, '_get_existing_migration', AsyncMock(return_value=None))
    monkeypatch.setattr(
        migration_service,
        'find_client_by_uuid',
        lambda uuid: expired_client if uuid == expired_uuid else None,
    )
    monkeypatch.setattr(migration_service, '_get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(migration_service, 'create_paid_subscription', create_paid_mock)

    try:
        await migration_service.migrate_vless_subscription(db, user, expired_uuid)
    except migration_service.XUiMigrationError as error:
        assert error.code == 'expired'
    else:
        raise AssertionError('expired legacy subscription should raise XUiMigrationError')

    create_paid_mock.assert_not_awaited()
    assert db.added == []
