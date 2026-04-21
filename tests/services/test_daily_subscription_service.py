from types import SimpleNamespace

import pytest

from app.services import daily_subscription_service as daily_service_module
from app.services.daily_subscription_service import DailySubscriptionService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class ExplodingSubscription:
    def __init__(self) -> None:
        self.id = 101
        self.user_id = 202
        self.tariff_id = 303

    @property
    def user(self):
        if 'user' in self.__dict__:
            return self.__dict__['user']
        raise AssertionError('lazy user relationship was accessed')

    @user.setter
    def user(self, value) -> None:
        self.__dict__['user'] = value

    @property
    def tariff(self):
        if 'tariff' in self.__dict__:
            return self.__dict__['tariff']
        raise AssertionError('lazy tariff relationship was accessed')

    @tariff.setter
    def tariff(self, value) -> None:
        self.__dict__['tariff'] = value


@pytest.mark.anyio('asyncio')
async def test_process_single_charge_avoids_lazy_relationship_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = DailySubscriptionService()
    subscription = ExplodingSubscription()
    user = SimpleNamespace(id=subscription.user_id)
    tariff = SimpleNamespace(id=subscription.tariff_id, daily_price_kopeks=0)

    async def fake_get_user_by_id(db, user_id):
        assert user_id == subscription.user_id
        return user

    async def fake_get_tariff_by_id(db, tariff_id, *, with_promo_groups=True):
        assert tariff_id == subscription.tariff_id
        assert with_promo_groups is False
        return tariff

    monkeypatch.setattr(
        daily_service_module,
        'sa_inspect',
        lambda obj: SimpleNamespace(dict=obj.__dict__),
    )
    monkeypatch.setattr(daily_service_module, 'get_user_by_id', fake_get_user_by_id)
    monkeypatch.setattr(daily_service_module, 'get_tariff_by_id', fake_get_tariff_by_id)

    result = await service._process_single_charge(SimpleNamespace(), subscription)

    assert result == 'error'
    assert subscription.user is user
    assert subscription.tariff is tariff


@pytest.mark.anyio('asyncio')
async def test_notify_daily_charge_skips_unloaded_tariff_without_lazy_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = DailySubscriptionService()
    service._bot = object()
    subscription = ExplodingSubscription()
    user = SimpleNamespace(language='ru', balance_kopeks=1450)
    captured_message: dict[str, str] = {}

    async def fake_notify_daily_debit(**kwargs):
        captured_message['telegram_message'] = kwargs['telegram_message']

    monkeypatch.setattr(
        daily_service_module,
        'sa_inspect',
        lambda obj: SimpleNamespace(dict=obj.__dict__),
    )
    monkeypatch.setattr(
        daily_service_module.notification_delivery_service,
        'notify_daily_debit',
        fake_notify_daily_debit,
    )

    await service._notify_daily_charge(user, subscription, 500)

    assert 'Тариф:' not in captured_message['telegram_message']

