"""
Regression tests for Telegram webhook setup on startup.

Root cause (2026-10-11): every instance ran delete_webhook(drop_pending_updates=True)
then set_webhook on startup. Two instances starting together raced: one set the
webhook, the other deleted it and then hit Telegram flood control (429) on
set_webhook, leaving no webhook registered and approvals silently unreachable.
"""

import asyncio
from types import SimpleNamespace

import pytest
from telegram.error import RetryAfter

from src.approval.telegram import TelegramBot

URL = "https://example.run.app/webhooks/telegram"


class FakeBot:
    def __init__(self, set_failures=0, current_url=""):
        self.set_failures = set_failures
        self.current_url = current_url
        self.set_calls = []
        self.delete_calls = 0

    async def delete_webhook(self, **kwargs):
        self.delete_calls += 1
        self.current_url = ""
        return True

    async def set_webhook(self, **kwargs):
        self.set_calls.append(kwargs)
        if self.set_failures:
            self.set_failures -= 1
            raise RetryAfter(1)
        self.current_url = kwargs["url"]
        return True

    async def get_webhook_info(self):
        return SimpleNamespace(url=self.current_url)


def make_bot(fake):
    bot = TelegramBot(token="123:fake", allowed_users=[1])
    bot._application = SimpleNamespace(bot=fake)
    return bot


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", instant)


def run(coro):
    return asyncio.run(coro)


def test_never_deletes_webhook():
    """Deleting first opens a window with no webhook and drops pending updates."""
    fake = FakeBot()
    assert run(make_bot(fake).setup_webhook(URL, secret_token="s3cret")) is True
    assert fake.delete_calls == 0
    assert fake.set_calls[0]["secret_token"] == "s3cret"
    assert fake.set_calls[0].get("drop_pending_updates") in (None, False)


def test_retries_after_flood_control():
    fake = FakeBot(set_failures=2)
    assert run(make_bot(fake).setup_webhook(URL)) is True
    assert len(fake.set_calls) == 3
    assert fake.current_url == URL


def test_flood_control_ok_when_another_instance_already_set_it():
    """If retries are exhausted but the webhook already points at us, that's fine."""
    fake = FakeBot(set_failures=99, current_url=URL)
    assert run(make_bot(fake).setup_webhook(URL)) is True


def test_flood_control_fails_when_webhook_missing():
    fake = FakeBot(set_failures=99, current_url="")
    assert run(make_bot(fake).setup_webhook(URL)) is False
