import asyncio
from unittest.mock import MagicMock

from app.models import (
    ContentType, JobStatus, PushSubscription, PushSubscriptionKeys, RipRecord,
)
from app import push as push_mod
from app.push import PushSender


def _sub(endpoint="https://push.example/abc"):
    return PushSubscription(endpoint=endpoint, keys=PushSubscriptionKeys(p256dh="p", auth="a"))


def _record(**kw):
    base = dict(content_type=ContentType.MOVIE, title="Inception", year=2010,
                output_path="/output/Movies/Inception (2010)/Inception (2010).mp4",
                status=JobStatus.DONE)
    base.update(kw)
    return RipRecord(**base)


def _sender(monkeypatch, subs):
    lib = MagicMock()
    lib.list_subscriptions.return_value = subs
    s = PushSender(lib, public_key="pub", private_key="priv", subject="mailto:a@b.c")
    return s, lib


def test_disabled_when_keys_missing():
    s = PushSender(MagicMock(), public_key="", private_key="")
    assert s.enabled is False
    assert asyncio.run(s.notify(_record())) == 0


def test_notify_sends_to_each_subscription(monkeypatch):
    sent = []
    monkeypatch.setattr(push_mod, "webpush", lambda **kw: sent.append(kw))
    s, lib = _sender(monkeypatch, [_sub("e1"), _sub("e2")])
    n = asyncio.run(s.notify(_record()))
    assert n == 2
    assert len(sent) == 2
    assert sent[0]["subscription_info"]["endpoint"] == "e1"
    assert "Inception" in sent[0]["data"]
    assert "done" in sent[0]["data"]


def test_notify_prunes_gone_subscription(monkeypatch):
    class FakeResp:
        status_code = 410

    def boom(**kw):
        raise push_mod.WebPushException("gone", response=FakeResp())

    monkeypatch.setattr(push_mod, "webpush", boom)
    s, lib = _sender(monkeypatch, [_sub("dead")])
    asyncio.run(s.notify(_record()))
    lib.remove_subscription.assert_called_once_with("dead")


def test_tv_label(monkeypatch):
    sent = []
    monkeypatch.setattr(push_mod, "webpush", lambda **kw: sent.append(kw))
    s, _ = _sender(monkeypatch, [_sub()])
    asyncio.run(s.notify(_record(content_type=ContentType.TV, title="Deadwood",
                                 year=None, season=1, episode=3, status=JobStatus.FAILED)))
    assert "Deadwood S01E03" in sent[0]["data"]
    assert "failed" in sent[0]["data"]
