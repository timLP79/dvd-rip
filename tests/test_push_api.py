import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_ROOT", str(tmp_path / "output"))
    monkeypatch.setenv("DVD_DEVICE", "/dev/null")
    monkeypatch.setenv("TMDB_API_KEY", "")
    monkeypatch.setenv("LIBRARY_DB", str(tmp_path / "library.db"))
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "test-public-key")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-private-key")
    from importlib import reload
    from app import main as main_module
    reload(main_module)
    with TestClient(main_module.app) as c:
        yield c


def test_push_key_reports_enabled(client):
    body = client.get("/api/push/key").json()
    assert body["enabled"] is True
    assert body["key"] == "test-public-key"


def test_subscribe_then_unsubscribe(client):
    sub = {"endpoint": "https://fcm.googleapis.com/fcm/send/xyz",
           "keys": {"p256dh": "p", "auth": "a"}}
    assert client.post("/api/push/subscribe", json=sub).status_code == 200
    # stored?
    from app import main as main_module
    assert len(main_module.app.state.library.list_subscriptions()) == 1
    assert client.post("/api/push/unsubscribe",
                       json={"endpoint": sub["endpoint"]}).status_code == 200
    assert main_module.app.state.library.list_subscriptions() == []


def test_test_endpoint_counts_sends(client, monkeypatch):
    from app import push as push_mod
    monkeypatch.setattr(push_mod, "webpush", lambda **kw: None)
    client.post("/api/push/subscribe",
                json={"endpoint": "https://fcm.googleapis.com/fcm/send/e1",
                      "keys": {"p256dh": "p", "auth": "a"}})
    body = client.post("/api/push/test").json()
    assert body["sent"] == 1


def test_subscribe_rejects_unknown_endpoint(client):
    bad = {"endpoint": "https://evil.example/inject",
           "keys": {"p256dh": "p", "auth": "a"}}
    assert client.post("/api/push/subscribe", json=bad).status_code == 400
    # http (non-TLS) is also rejected
    insecure = {"endpoint": "http://fcm.googleapis.com/fcm/send/x",
                "keys": {"p256dh": "p", "auth": "a"}}
    assert client.post("/api/push/subscribe", json=insecure).status_code == 400


def test_subscribe_caps_count(client, monkeypatch):
    from app import main as main_module
    monkeypatch.setattr(main_module, "MAX_PUSH_SUBSCRIPTIONS", 2)
    for n in "abc":
        res = client.post("/api/push/subscribe", json={
            "endpoint": f"https://fcm.googleapis.com/fcm/send/{n}",
            "keys": {"p256dh": "p", "auth": "a"}})
    assert res.status_code == 429  # the third one is rejected
    assert len(main_module.app.state.library.list_subscriptions()) == 2


def test_push_key_disabled_without_vapid(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_ROOT", str(tmp_path / "output"))
    monkeypatch.setenv("DVD_DEVICE", "/dev/null")
    monkeypatch.setenv("TMDB_API_KEY", "")
    monkeypatch.setenv("LIBRARY_DB", str(tmp_path / "library.db"))
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    from importlib import reload
    from app import main as main_module
    reload(main_module)
    with TestClient(main_module.app) as c:
        assert c.get("/api/push/key").json()["enabled"] is False
