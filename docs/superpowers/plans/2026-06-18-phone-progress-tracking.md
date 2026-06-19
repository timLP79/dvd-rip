# Phone Progress Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user monitor rips from an Android phone over a Tailscale-served HTTPS origin, with an installable PWA that delivers a Web Push notification when a rip finishes or fails, behind a shared-password login.

**Architecture:** App code adds (1) a SQLite-backed Web Push subscription store, (2) a `PushSender` (VAPID/`pywebpush`) fired from the ripper's terminal-state hook, (3) push API endpoints, (4) a PWA shell (manifest + root-scoped service worker), (5) a notify-toggle UI, (6) shared-password session auth, and (7) mobile CSS polish. The HTTPS secure context and stable hostname are provided out-of-band by `tailscale serve` (ops/docs, no container change).

**Tech Stack:** FastAPI/Starlette, `pywebpush` (VAPID Web Push), Starlette `SessionMiddleware` (+`itsdangerous`), SQLite, vanilla JS service worker + PushManager, Tailscale.

## Global Constraints

- Python 3.12; FastAPI 0.115.0; pydantic 2.9.2 (match existing pins).
- **Feature-gating mirrors `TMDBClient.enabled`:** push disabled unless `VAPID_PUBLIC_KEY` + `VAPID_PRIVATE_KEY` set; auth disabled unless `APP_PASSWORD` set. Disabled features must not break startup or existing tests.
- The **only** module that executes SQL is `app/library.py` (extend it; do not open SQLite elsewhere).
- Subprocess calls stay argv-based (no change here, but do not introduce shell strings).
- Tests never touch a real DVD drive, real push service, or network. Mock `app.push.webpush`. `pytest.ini` sets `asyncio_mode = auto`; new async-flow tests use `asyncio.run(...)` like `tests/test_ripper.py`.
- UI stays clean/utility per recorded prefs: blue-gray palette (panels `#161922`, body `#0c0e13`, text `#e1e5ed`), Prussian Ink accent `#4b73b8` used sparingly, IBM Plex Sans/Mono only. No new fonts or decoration.
- Push fires on terminal states only (done/failed/cancelled). Notify toggle lives in the Jobs section header.
- Commit after every task with the project's trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` and the `Claude-Session:` line.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `app/models.py` | modify | Add `PushSubscriptionKeys`, `PushSubscription`, `PushUnsubscribe`. |
| `app/library.py` | modify | Add `push_subscriptions` table + `add_subscription`/`list_subscriptions`/`remove_subscription`. |
| `app/push.py` | create | `PushSender`: VAPID config, build payload, send via `pywebpush` in executor, prune dead subs. |
| `app/ripper.py` | modify | `push` param; fire-and-forget notify in `_save_record`. |
| `app/main.py` | modify | Lifespan wiring; push endpoints; `/sw.js` route; session auth middleware + `/login`. |
| `app/templates/index.html`, `library.html` | modify | PWA head tags; notify button. |
| `app/templates/login.html` | create | Password login page. |
| `app/static/manifest.webmanifest` | create | PWA manifest. |
| `app/static/icons/icon.svg` | create | App/notification icon (palette disc glyph). |
| `app/static/sw.js` | create | Service worker: `push` + `notificationclick`. |
| `app/static/app.js` | modify | Notify-toggle subscribe/unsubscribe flow. |
| `app/static/style.css` | modify | Notify button active state; mobile reflow. |
| `requirements.txt` | modify | Add `pywebpush`, `itsdangerous`. |
| `Makefile` | modify | `secrets` target (VAPID + session secret). |
| `.env.example` | modify | Document `APP_PASSWORD`, `SESSION_SECRET`, `VAPID_*`. |
| `README.md` | modify | Tailscale serve + phone setup section. |
| `tests/test_library.py`, `test_push.py`, `test_push_api.py`, `test_ripper.py`, `test_auth.py`, `test_pwa.py` | create/modify | Coverage per task. |

---

### Task 1: Push subscription storage (`library.py`)

**Files:**
- Modify: `app/library.py` (add table to `_SCHEMA`; add 3 methods)
- Modify: `app/models.py` (add subscription models)
- Test: `tests/test_library.py`

**Interfaces:**
- Produces (models): `PushSubscriptionKeys(p256dh: str, auth: str)`, `PushSubscription(endpoint: str, keys: PushSubscriptionKeys)`.
- Produces (Library): `add_subscription(endpoint: str, p256dh: str, auth: str) -> None` (upsert on endpoint), `list_subscriptions() -> list[PushSubscription]`, `remove_subscription(endpoint: str) -> None`.

- [ ] **Step 1: Add the models** in `app/models.py` (after the TMDB section, before `# --- Job tracking`):

```python
# --- Web Push subscriptions --------------------------------------------------

class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscription(BaseModel):
    """A browser PushManager subscription, as POSTed by the client."""
    endpoint: str
    keys: PushSubscriptionKeys


class PushUnsubscribe(BaseModel):
    endpoint: str
```

- [ ] **Step 2: Write the failing test** — append to `tests/test_library.py`:

```python
from app.models import PushSubscription


def test_push_subscription_crud(tmp_path):
    lib = _lib(tmp_path)
    lib.add_subscription("https://push.example/abc", "p256-key", "auth-key")
    subs = lib.list_subscriptions()
    assert len(subs) == 1
    assert isinstance(subs[0], PushSubscription)
    assert subs[0].endpoint == "https://push.example/abc"
    assert subs[0].keys.p256dh == "p256-key"
    assert subs[0].keys.auth == "auth-key"


def test_push_subscription_upsert_is_idempotent(tmp_path):
    lib = _lib(tmp_path)
    lib.add_subscription("https://push.example/abc", "k1", "a1")
    lib.add_subscription("https://push.example/abc", "k2", "a2")  # same endpoint
    subs = lib.list_subscriptions()
    assert len(subs) == 1
    assert subs[0].keys.p256dh == "k2"


def test_push_subscription_remove(tmp_path):
    lib = _lib(tmp_path)
    lib.add_subscription("https://push.example/abc", "k", "a")
    lib.remove_subscription("https://push.example/abc")
    assert lib.list_subscriptions() == []
```

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_library.py -k push -v`
Expected: FAIL (`AttributeError: 'Library' object has no attribute 'add_subscription'`).

- [ ] **Step 4: Implement.** In `app/library.py`, append the new table to `_SCHEMA` (inside the same triple-quoted string, after the `rips` table):

```python
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

Add `PushSubscription, PushSubscriptionKeys` to the `from .models import (...)` line. Add these methods to the `Library` class (after `find_existing`):

```python
    def add_subscription(self, endpoint: str, p256dh: str, auth: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(endpoint) DO UPDATE SET
                       p256dh=excluded.p256dh, auth=excluded.auth""",
                (endpoint, p256dh, auth, _now_iso()),
            )

    def list_subscriptions(self) -> list[PushSubscription]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT endpoint, p256dh, auth FROM push_subscriptions"
            ).fetchall()
        return [
            PushSubscription(
                endpoint=r["endpoint"],
                keys=PushSubscriptionKeys(p256dh=r["p256dh"], auth=r["auth"]),
            )
            for r in rows
        ]

    def remove_subscription(self, endpoint: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_library.py -v`
Expected: PASS (all, including the existing rips tests).

- [ ] **Step 6: Commit**

```bash
git add app/models.py app/library.py tests/test_library.py
git commit -m "feat(library): persist Web Push subscriptions"
```

---

### Task 2: `PushSender` — VAPID send + prune (`app/push.py`)

**Files:**
- Create: `app/push.py`
- Modify: `requirements.txt` (add `pywebpush`)
- Test: `tests/test_push.py`

**Interfaces:**
- Consumes: `Library.list_subscriptions()`, `Library.remove_subscription(endpoint)` (Task 1); `RipRecord` (`content_type`, `title`, `year`, `season`, `episode`, `status`).
- Produces: `PushSender(library, *, public_key=None, private_key=None, subject=None)`, property `.enabled -> bool`, `async def notify(record: RipRecord) -> int` (returns number of sends attempted), `async def notify_test() -> int`. Module-level `webpush`/`WebPushException` imported from `pywebpush` so tests can patch `app.push.webpush`.

- [ ] **Step 1: Add the dependency.** Append to `requirements.txt` (range avoids pinning a version that may not exist; pin the exact resolved version after install if you want to match the repo's style):

```
pywebpush>=1.14,<3
```

Reinstall into the venv: `rm -rf .venv && make venv` (or `.venv/bin/pip install 'pywebpush>=1.14,<3'`). This also brings in `py_vapid` and `cryptography` (used by `make secrets` in Task 9).

- [ ] **Step 2: Write the failing test** — create `tests/test_push.py`:

```python
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
```

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_push.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.push'`).

- [ ] **Step 4: Implement** `app/push.py`:

```python
"""Web Push (VAPID) notifications for terminal rip events.

Feature-gated like the TMDB client: disabled unless VAPID_PUBLIC_KEY and
VAPID_PRIVATE_KEY are set. The synchronous pywebpush call is offloaded to a
thread executor so it never blocks the async rip worker. Dead subscriptions
(404/410) are pruned. Best-effort throughout: a push failure never propagates.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
from typing import Optional

from pywebpush import WebPushException, webpush

from .library import Library
from .models import ContentType, JobStatus, RipRecord

log = logging.getLogger(__name__)

_STATUS_WORD = {
    JobStatus.DONE: "done",
    JobStatus.FAILED: "failed",
    JobStatus.CANCELLED: "cancelled",
}


def _label(record: RipRecord) -> str:
    ct = record.content_type
    is_tv = ct is ContentType.TV or ct == "tv"
    if is_tv and record.season is not None and record.episode is not None:
        return f"{record.title} S{record.season:02d}E{record.episode:02d}"
    if record.year:
        return f"{record.title} ({record.year})"
    return record.title


class PushSender:
    def __init__(self, library: Library, *, public_key: Optional[str] = None,
                 private_key: Optional[str] = None, subject: Optional[str] = None):
        self.library = library
        self.public_key = public_key if public_key is not None else os.getenv("VAPID_PUBLIC_KEY", "")
        self._private_key = private_key if private_key is not None else os.getenv("VAPID_PRIVATE_KEY", "")
        self._subject = subject or os.getenv("VAPID_SUBJECT", "") or "mailto:dvd-rip@localhost"

    @property
    def enabled(self) -> bool:
        return bool(self.public_key and self._private_key)

    def _payload(self, record: RipRecord) -> str:
        status = record.status if isinstance(record.status, JobStatus) else JobStatus(record.status)
        word = _STATUS_WORD.get(status, str(status))
        label = _label(record)
        return json.dumps({
            "title": "DVD Rip",
            "body": f"{label} — {word}",
            "url": "/",
            "tag": label,
        })

    def _send_one(self, sub, data: str) -> None:
        webpush(
            subscription_info={
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.keys.p256dh, "auth": sub.keys.auth},
            },
            data=data,
            vapid_private_key=self._private_key,
            vapid_claims={"sub": self._subject},
        )

    async def _broadcast(self, data: str) -> int:
        subs = self.library.list_subscriptions()
        if not subs:
            return 0
        loop = asyncio.get_running_loop()
        attempted = 0
        for sub in subs:
            attempted += 1
            try:
                await loop.run_in_executor(None, functools.partial(self._send_one, sub, data))
            except WebPushException as exc:
                code = getattr(getattr(exc, "response", None), "status_code", None)
                if code in (404, 410):
                    self.library.remove_subscription(sub.endpoint)
                else:
                    log.warning("web push failed (%s) for %s", code, sub.endpoint)
            except Exception:
                log.exception("web push error for %s", sub.endpoint)
        return attempted

    async def notify(self, record: RipRecord) -> int:
        if not self.enabled:
            return 0
        return await self._broadcast(self._payload(record))

    async def notify_test(self) -> int:
        if not self.enabled:
            return 0
        data = json.dumps({"title": "DVD Rip", "body": "Test notification", "url": "/", "tag": "test"})
        return await self._broadcast(data)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_push.py -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add app/push.py requirements.txt tests/test_push.py
git commit -m "feat(push): VAPID Web Push sender with prune-on-410"
```

---

### Task 3: Fire push from the ripper's terminal hook

**Files:**
- Modify: `app/ripper.py` (`__init__` add `push`; `_save_record` schedule notify)
- Modify: `app/main.py` (lifespan: build `PushSender`, pass to `Ripper`, store on `app.state`)
- Test: `tests/test_ripper.py`

**Interfaces:**
- Consumes: `PushSender.notify(record)`, `PushSender.enabled` (Task 2).
- Produces: `Ripper(..., push: Optional[PushSender] = None)`; on terminal record save the ripper schedules `push.notify(record)` fire-and-forget.

- [ ] **Step 1: Write the failing test** — append to `tests/test_ripper.py`. A real `Library` is required because `_save_record` returns early when `library is None`; the push call sits after the library write (in production the lifespan always supplies a library):

```python
def test_save_record_fires_push(tmp_path):
    import asyncio
    from app.library import Library
    from app.models import (
        ContentType, EncoderMode, JobStatus, MovieMetadata, TitleSelection,
    )
    from app.ripper import Ripper

    class FakePush:
        enabled = True
        def __init__(self):
            self.records = []
        async def notify(self, record):
            self.records.append(record)
            return 1

    fake = FakePush()

    async def scenario():
        lib = Library(str(tmp_path / "library.db"))
        lib.init_db()
        r = Ripper(device="/dev/null", output_root=tmp_path, library=lib, push=fake)
        job = r.enqueue(
            selection=TitleSelection(index=1, audio_tracks=[1], subtitle_tracks=[]),
            encoder_mode=EncoderMode.QUALITY,
            content_type=ContentType.MOVIE,
            movie=MovieMetadata(title="Inception", year=2010),
        )
        r._save_record(job.id, JobStatus.DONE)
        await asyncio.sleep(0)  # let the fire-and-forget task run
        return fake.records

    records = asyncio.run(scenario())
    assert len(records) == 1
    assert records[0].title == "Inception"
    assert records[0].status == JobStatus.DONE
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ripper.py -k push -v`
Expected: FAIL (`TypeError: __init__() got an unexpected keyword argument 'push'`).

- [ ] **Step 3: Implement.** In `app/ripper.py`, extend `__init__` signature and store the sender:

```python
    def __init__(self, *, device: str, output_root: Path, library: Optional[Library] = None,
                 scratch_dir: str = "/data/scratch", push: Optional["PushSender"] = None):
        ...
        self.library = library
        self.push = push
        ...
```

Add the import at the top (guard against import cycle by importing the symbol directly — `push.py` imports from `library`/`models` only, so this is safe):

```python
from .push import PushSender
```

At the **end of `_save_record`**, after the `self.library.record_rip(record)` try/except block, add:

```python
        if self.push is not None and self.push.enabled:
            try:
                asyncio.get_running_loop().create_task(self.push.notify(record))
            except RuntimeError:
                pass  # no running loop (e.g. a unit test calling _save_record directly)
```

- [ ] **Step 4: Wire it in `app/main.py` lifespan.** Add `from .push import PushSender` to the imports. In `lifespan`, after `library.init_db()`/backfill and before constructing `Ripper`:

```python
    push = PushSender(library)
    ripper = Ripper(device=DVD_DEVICE, output_root=OUTPUT_ROOT, library=library,
                    scratch_dir=MAKEMKV_SCRATCH, push=push)
    await ripper.start()
    tmdb = TMDBClient()
    app.state.ripper = ripper
    app.state.tmdb = tmdb
    app.state.library = library
    app.state.push = push
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_ripper.py -v`
Expected: PASS (including existing ripper tests).

- [ ] **Step 6: Commit**

```bash
git add app/ripper.py app/main.py tests/test_ripper.py
git commit -m "feat(ripper): notify Web Push subscribers on terminal job state"
```

---

### Task 4: Push API endpoints (`main.py`)

**Files:**
- Modify: `app/main.py` (4 routes + model imports)
- Test: `tests/test_push_api.py`

**Interfaces:**
- Consumes: `app.state.push` (PushSender), `app.state.library` (Library), `PushSubscription`/`PushUnsubscribe` models.
- Produces: `GET /api/push/key` → `{"enabled": bool, "key": str}`; `POST /api/push/subscribe` (body `PushSubscription`) → `{"ok": true}`; `POST /api/push/unsubscribe` (body `PushUnsubscribe`) → `{"ok": true}`; `POST /api/push/test` → `{"sent": int}`.

- [ ] **Step 1: Write the failing test** — create `tests/test_push_api.py`:

```python
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
    sub = {"endpoint": "https://push.example/xyz",
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
                json={"endpoint": "e1", "keys": {"p256dh": "p", "auth": "a"}})
    body = client.post("/api/push/test").json()
    assert body["sent"] == 1


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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_push_api.py -v`
Expected: FAIL (404 on `/api/push/key`).

- [ ] **Step 3: Implement.** In `app/main.py`, add `PushSubscription, PushUnsubscribe` to the `from .models import (...)` block and `from .push import PushSender` (already added in Task 3). Add these routes (after the jobs routes):

```python
@app.get("/api/push/key")
async def api_push_key(request: Request) -> dict:
    push: PushSender = request.app.state.push
    return {"enabled": push.enabled, "key": push.public_key}


@app.post("/api/push/subscribe")
async def api_push_subscribe(sub: PushSubscription, request: Request) -> dict:
    library: Library = request.app.state.library
    library.add_subscription(sub.endpoint, sub.keys.p256dh, sub.keys.auth)
    return {"ok": True}


@app.post("/api/push/unsubscribe")
async def api_push_unsubscribe(body: PushUnsubscribe, request: Request) -> dict:
    library: Library = request.app.state.library
    library.remove_subscription(body.endpoint)
    return {"ok": True}


@app.post("/api/push/test")
async def api_push_test(request: Request) -> dict:
    push: PushSender = request.app.state.push
    sent = await push.notify_test()
    return {"sent": sent}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_push_api.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_push_api.py
git commit -m "feat(api): Web Push key/subscribe/unsubscribe/test endpoints"
```

---

### Task 5: PWA shell — manifest, icon, root-scoped service worker

**Files:**
- Create: `app/static/manifest.webmanifest`, `app/static/icons/icon.svg`, `app/static/sw.js`
- Modify: `app/main.py` (add `/sw.js` route), `app/templates/index.html`, `app/templates/library.html`
- Test: `tests/test_pwa.py`

**Interfaces:**
- Produces: `/sw.js` served at the root (scope `/`); `/static/manifest.webmanifest`; templates reference both. The notify UI (Task 6) registers `/sw.js` and reads `/api/push/key`.

- [ ] **Step 1: Write the failing test** — create `tests/test_pwa.py`:

```python
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_ROOT", str(tmp_path / "output"))
    monkeypatch.setenv("DVD_DEVICE", "/dev/null")
    monkeypatch.setenv("TMDB_API_KEY", "")
    monkeypatch.setenv("LIBRARY_DB", str(tmp_path / "library.db"))
    from importlib import reload
    from app import main as main_module
    reload(main_module)
    with TestClient(main_module.app) as c:
        yield c


def test_service_worker_served_at_root_scope(client):
    res = client.get("/sw.js")
    assert res.status_code == 200
    assert "javascript" in res.headers["content-type"]
    assert "addEventListener('push'" in res.text


def test_manifest_served(client):
    res = client.get("/static/manifest.webmanifest")
    assert res.status_code == 200
    assert '"display": "standalone"' in res.text


def test_index_links_manifest_and_theme(client):
    html = client.get("/").text
    assert 'rel="manifest"' in html
    assert 'name="theme-color"' in html
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_pwa.py -v`
Expected: FAIL (404 on `/sw.js`).

- [ ] **Step 3: Create `app/static/manifest.webmanifest`:**

```json
{
  "name": "DVD Rip",
  "short_name": "DVD Rip",
  "start_url": "/",
  "scope": "/",
  "display": "standalone",
  "background_color": "#0c0e13",
  "theme_color": "#0c0e13",
  "icons": [
    {
      "src": "/static/icons/icon.svg",
      "sizes": "any",
      "type": "image/svg+xml",
      "purpose": "any maskable"
    }
  ]
}
```

- [ ] **Step 4: Create `app/static/icons/icon.svg`** (palette disc glyph, maskable-safe padding):

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <rect width="512" height="512" fill="#0c0e13"/>
  <circle cx="256" cy="256" r="150" fill="none" stroke="#4b73b8" stroke-width="28"/>
  <circle cx="256" cy="256" r="44" fill="#0c0e13" stroke="#4b73b8" stroke-width="20"/>
  <circle cx="256" cy="256" r="6" fill="#e1e5ed"/>
</svg>
```

- [ ] **Step 5: Create `app/static/sw.js`:**

```javascript
// Service worker: Web Push display + click handling. No offline caching.
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { /* non-JSON */ }
  const title = data.title || 'DVD Rip';
  const options = {
    body: data.body || '',
    icon: '/static/icons/icon.svg',
    tag: data.tag || 'dvd-rip',
    data: { url: data.url || '/' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      for (const w of wins) { if ('focus' in w) return w.focus(); }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
```

- [ ] **Step 6: Add the `/sw.js` route in `app/main.py`.** Add `from fastapi.responses import FileResponse` to the responses import line, then:

```python
@app.get("/sw.js")
async def service_worker() -> FileResponse:
    # Served from the root so its scope is "/" (covers the whole app), not "/static/".
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")
```

- [ ] **Step 7: Add PWA head tags** to BOTH `app/templates/index.html` and `app/templates/library.html`, right after the existing `<link rel="stylesheet" ...>` line in `<head>`:

```html
  <link rel="manifest" href="/static/manifest.webmanifest" />
  <meta name="theme-color" content="#0c0e13" />
  <link rel="apple-touch-icon" href="/static/icons/icon.svg" />
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_pwa.py -v`
Expected: PASS (3 tests).

- [ ] **Step 9: Commit**

```bash
git add app/static/manifest.webmanifest app/static/icons/icon.svg app/static/sw.js app/main.py app/templates/index.html app/templates/library.html tests/test_pwa.py
git commit -m "feat(pwa): installable manifest, icon, and root-scoped service worker"
```

---

### Task 6: Notify-toggle UI (subscribe/unsubscribe)

**Files:**
- Modify: `app/templates/index.html` (button in Jobs section header)
- Modify: `app/static/app.js` (subscribe flow)
- Modify: `app/static/style.css` (active state)
- Test: `tests/test_pwa.py` (served-content assertions)

**Interfaces:**
- Consumes: `GET /api/push/key`, `POST /api/push/subscribe`, `POST /api/push/unsubscribe`, `/sw.js` (Tasks 4–5).
- Produces: a `#btn-notify` toggle in the Jobs header that registers the SW and (un)subscribes the browser.

- [ ] **Step 1: Write the failing test** — append to `tests/test_pwa.py`:

```python
def test_notify_button_present(client):
    assert 'id="btn-notify"' in client.get("/").text


def test_app_js_wires_push(client):
    js = client.get("/static/app.js").text
    assert "/api/push/key" in js
    assert "pushManager.subscribe" in js
    assert "/sw.js" in js
```

Run to confirm fail: `.venv/bin/python -m pytest tests/test_pwa.py -k notify -v` → FAIL.

- [ ] **Step 2: Add the button** to `app/templates/index.html` — change the Jobs `section-head` so it reads:

```html
      <div class="section-head">
        <h2>Jobs</h2>
        <button id="btn-notify" type="button" class="ghost" hidden>Enable notifications</button>
      </div>
```

- [ ] **Step 3: Add the subscribe flow** to the end of `app/static/app.js`:

```javascript
// ---- Web Push notify toggle ------------------------------------------------
function urlB64ToUint8Array(b64) {
  const pad = '='.repeat((4 - (b64.length % 4)) % 4);
  const base = (b64 + pad).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function initPushUI() {
  const btn = document.getElementById('btn-notify');
  if (!btn) return;
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return; // unsupported: stay hidden

  let key = '';
  try {
    const info = await (await fetch('/api/push/key')).json();
    if (!info.enabled) return; // push disabled server-side: stay hidden
    key = info.key;
  } catch (e) { return; }

  btn.hidden = false;
  if (!window.isSecureContext) {
    btn.textContent = 'Open via HTTPS to enable';
    btn.disabled = true;
    return;
  }

  const reg = await navigator.serviceWorker.register('/sw.js');
  const render = (sub) => {
    btn.classList.toggle('active', !!sub);
    btn.textContent = sub ? 'Notifications on' : 'Enable notifications';
  };
  render(await reg.pushManager.getSubscription());

  btn.addEventListener('click', async () => {
    const existing = await reg.pushManager.getSubscription();
    if (existing) {
      await existing.unsubscribe();
      await fetch('/api/push/unsubscribe', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: existing.endpoint }),
      });
      render(null);
      return;
    }
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') { btn.textContent = 'Notifications blocked'; return; }
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlB64ToUint8Array(key),
    });
    await fetch('/api/push/subscribe', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sub),
    });
    render(sub);
  });
}

initPushUI();
```

- [ ] **Step 4: Add the active state** to `app/static/style.css` (near the other `.ghost` rules; if none, append):

```css
#btn-notify.active {
  color: var(--accent-bright, #6f97d8);
  border-color: var(--accent, #4b73b8);
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_pwa.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/templates/index.html app/static/app.js app/static/style.css tests/test_pwa.py
git commit -m "feat(ui): notify toggle that subscribes the browser to Web Push"
```

---

### Task 7: Shared-password session auth

**Files:**
- Modify: `app/main.py` (SessionMiddleware, auth middleware, `/login` routes)
- Create: `app/templates/login.html`
- Modify: `requirements.txt` (add `itsdangerous`)
- Test: `tests/test_auth.py`

**Interfaces:**
- Produces: when `APP_PASSWORD` is set, all routes except `/login`, `/sw.js`, and `/static/*` require a valid session; `/login` (GET form, POST sets `session["authed"]`). When `APP_PASSWORD` is unset, auth is fully disabled.

- [ ] **Step 1: Add the dependency.** Append to `requirements.txt`:

```
itsdangerous==2.2.0
```

Install: `.venv/bin/pip install itsdangerous==2.2.0`.

- [ ] **Step 2: Write the failing test** — create `tests/test_auth.py`:

```python
import pytest
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch, password):
    monkeypatch.setenv("OUTPUT_ROOT", str(tmp_path / "output"))
    monkeypatch.setenv("DVD_DEVICE", "/dev/null")
    monkeypatch.setenv("TMDB_API_KEY", "")
    monkeypatch.setenv("LIBRARY_DB", str(tmp_path / "library.db"))
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "0")  # TestClient uses http
    if password is None:
        monkeypatch.delenv("APP_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("APP_PASSWORD", password)
    from importlib import reload
    from app import main as main_module
    reload(main_module)
    return TestClient(main_module.app, follow_redirects=False)


def test_auth_disabled_when_no_password(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, None) as c:
        assert c.get("/").status_code == 200


def test_protected_routes_require_login(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, "hunter2") as c:
        assert c.get("/").status_code == 303          # redirect to /login
        assert c.get("/api/jobs").status_code == 401   # API: 401, not redirect
        assert c.get("/login").status_code == 200      # login page open
        assert c.get("/static/style.css").status_code == 200  # static exempt
        assert c.get("/sw.js").status_code == 200      # sw exempt


def test_login_flow(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, "hunter2") as c:
        assert c.post("/login", data={"password": "wrong"}).status_code == 401
        ok = c.post("/login", data={"password": "hunter2"})
        assert ok.status_code == 303
        # session cookie now lets us through
        assert c.get("/").status_code == 200
        assert c.get("/api/jobs").status_code == 200
```

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_auth.py -v`
Expected: FAIL (`/` returns 200 even with password set; no `/login`).

- [ ] **Step 4: Implement in `app/main.py`.** Add imports:

```python
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
```

(Merge with the existing `from fastapi.responses import HTMLResponse` line — do not duplicate.) After `ASSET_VERSION = ...`, add config:

```python
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
AUTH_ENABLED = bool(APP_PASSWORD)
SESSION_SECRET = os.getenv("SESSION_SECRET", "") or secrets.token_hex(32)
COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "1") != "0"
AUTH_EXEMPT = {"/login", "/sw.js"}
```

After `app = FastAPI(...)` and the `app.mount("/static", ...)` line, register the auth middleware FIRST and the session middleware LAST so SessionMiddleware is outermost (runs first, populating `request.session` before the auth check reads it):

```python
@app.middleware("http")
async def require_auth(request: Request, call_next):
    if not AUTH_ENABLED:
        return await call_next(request)
    path = request.url.path
    if path in AUTH_EXEMPT or path.startswith("/static/"):
        return await call_next(request)
    if request.session.get("authed"):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=COOKIE_SECURE,
    same_site="lax",
    max_age=60 * 60 * 24 * 60,  # 60 days
)
```

Add the login routes (near `index`):

```python
@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "login.html", {"asset_version": ASSET_VERSION, "error": None}
    )


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    password = str(form.get("password", ""))
    if AUTH_ENABLED and secrets.compare_digest(password, APP_PASSWORD):
        request.session["authed"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"asset_version": ASSET_VERSION, "error": "Incorrect password"},
        status_code=401,
    )
```

- [ ] **Step 5: Create `app/templates/login.html`:**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>DVD Rip — Sign in</title>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/static/style.css?v={{ asset_version }}" />
</head>
<body>
  <main style="max-width:340px;margin:18vh auto;padding:0 20px;">
    <h1 class="brand-title">DVD Rip</h1>
    <form method="post" action="/login" class="panel" style="display:flex;flex-direction:column;gap:12px;">
      <label>Password
        <input type="password" name="password" autofocus autocomplete="current-password" />
      </label>
      {% if error %}<p style="color:var(--warning,#d99c3a);font-size:13px;">{{ error }}</p>{% endif %}
      <button type="submit">Sign in</button>
    </form>
  </main>
</body>
</html>
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_auth.py tests/test_api.py -v`
Expected: PASS (auth tests pass; existing `test_api.py` still passes because no `APP_PASSWORD` is set there).

- [ ] **Step 7: Commit**

```bash
git add app/main.py app/templates/login.html requirements.txt tests/test_auth.py
git commit -m "feat(auth): shared-password session login, gated on APP_PASSWORD"
```

---

### Task 8: Mobile UI polish (responsive Titles + tap targets)

**Files:**
- Modify: `app/static/style.css` (extend the `@media (max-width: 820px)` block)
- Test: `tests/test_pwa.py` (assert the responsive rules are served)

**Interfaces:** none new — CSS-only reflow so the Titles table and controls are usable at ~390px.

- [ ] **Step 1: Write the failing test** — append to `tests/test_pwa.py`:

```python
def test_mobile_titles_reflow_present(client):
    css = client.get("/static/style.css").text
    assert "max-width: 480px" in css   # phone breakpoint added
    assert "#titles thead" in css      # table-to-cards reflow
    assert "col-duration::before" in css  # CSS-supplied row labels
```

Run to confirm fail: `.venv/bin/python -m pytest tests/test_pwa.py -k mobile -v` → FAIL.

- [ ] **Step 2: Add the phone breakpoint** to `app/static/style.css` (after the existing `@media (max-width: 820px) { ... }` block). This reflows the Titles `<table>` into stacked cards. The row labels are supplied by CSS `::before` content keyed off the per-column classes the template already emits (`col-duration`, `col-chapters`, `col-frame`), so no JS/template change is needed:

```css
@media (max-width: 480px) {
  /* Stack the titles table into per-row cards. */
  #titles thead { display: none; }
  #titles, #titles tbody, #titles tr, #titles td { display: block; width: 100%; }
  #titles tr.title-row {
    border: 1px solid var(--border, #232733);
    border-radius: var(--radius, 8px);
    padding: 10px 12px;
    margin-bottom: 10px;
  }
  #titles td {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 4px 0;
    border: none;
  }
  #titles td.col-duration::before { content: "Duration"; color: var(--text-muted); }
  #titles td.col-chapters::before { content: "Chapters"; color: var(--text-muted); }
  #titles td.col-frame::before    { content: "Frame";    color: var(--text-muted); }

  /* Comfortable tap targets. */
  input[type="checkbox"] { width: 22px; height: 22px; }
  button, .ghost { min-height: 40px; }

  /* Respect the notch / home indicator when installed. */
  main { padding-bottom: calc(56px + env(safe-area-inset-bottom)); }
}
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_pwa.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add app/static/style.css tests/test_pwa.py
git commit -m "feat(ui): responsive Titles cards and tap targets for phones"
```

---

### Task 9: Ops — `make secrets`, `.env.example`, Tailscale docs

**Files:**
- Modify: `Makefile` (`secrets` target)
- Modify: `.env.example`
- Modify: `README.md`

**Interfaces:** `make secrets` prints `.env`-ready lines for `SESSION_SECRET`, `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT`.

- [ ] **Step 1: Add the `secrets` target** to `Makefile` (after the `dev` target). It runs in the venv (so `pywebpush`/`cryptography` are available):

```makefile
.PHONY: secrets
secrets: venv ## Print fresh SESSION_SECRET + VAPID keys for .env (phone push/auth)
	@.venv/bin/python -c "import secrets, base64; \
from py_vapid import Vapid01; \
from cryptography.hazmat.primitives import serialization as s; \
v=Vapid01(); v.generate_keys(); \
priv=v.private_key.private_numbers().private_value.to_bytes(32,'big'); \
pub=v.public_key.public_bytes(s.Encoding.X962, s.PublicFormat.UncompressedPoint); \
b=lambda x: base64.urlsafe_b64encode(x).rstrip(b'=').decode(); \
print('SESSION_SECRET='+secrets.token_hex(32)); \
print('VAPID_PRIVATE_KEY='+b(priv)); \
print('VAPID_PUBLIC_KEY='+b(pub)); \
print('VAPID_SUBJECT=mailto:you@example.com')"
```

- [ ] **Step 2: Verify the target runs**

Run: `make secrets`
Expected: four `KEY=value` lines printed; `VAPID_PUBLIC_KEY` is ~87 chars of base64url. (`py_vapid` ships as a `pywebpush` dependency from Task 2.)

- [ ] **Step 3: Extend `.env.example`** — append:

```bash
# --- Phone access (PWA Web Push + login) -------------------------------------
# Shared password gating the whole app. Unset = no auth (fine for localhost dev;
# set it before exposing the app over Tailscale). Generate the rest with `make secrets`.
APP_PASSWORD=
SESSION_SECRET=
# VAPID keypair for self-hosted Web Push (run `make secrets` to generate). Unset
# = push disabled (the notify button stays hidden).
VAPID_PUBLIC_KEY=
VAPID_PRIVATE_KEY=
VAPID_SUBJECT=mailto:you@example.com
```

- [ ] **Step 4: Add a README section** documenting the phone setup. Append under a new `## Phone access` heading:

```markdown
## Phone access (track rips from your phone)

The app is already LAN-reachable at `http://<host-ip>:8000`. To get a stable
HTTPS address (required for installable PWA + Web Push) and optional off-LAN
access, front it with Tailscale:

1. Install Tailscale on the host and your phone; sign both into the same tailnet.
   Enable MagicDNS + HTTPS certs in the Tailscale admin console.
2. On the host, publish the app over HTTPS:
   ```bash
   tailscale serve --bg 8000
   ```
   It is now at `https://<host>.<tailnet>.ts.net` with a trusted cert. The raw
   `:8000` port is never forwarded to the public internet.
3. Generate secrets and put them in `.env`, then restart the container:
   ```bash
   make secrets   # paste the printed lines into .env
   # also set APP_PASSWORD=<your password> in .env
   ```
4. On the phone, open the `https://…ts.net` URL, sign in, and tap **Enable
   notifications** in the Jobs panel (optionally "Add to Home Screen" for an app
   icon). You'll get a push when each rip finishes or fails.

Security: access is gated by your tailnet **and** the shared password. Tighten
further with Tailscale ACLs if you share your tailnet.
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: PASS (all tests, old and new).

- [ ] **Step 6: Commit**

```bash
git add Makefile .env.example README.md
git commit -m "docs(ops): make secrets helper and Tailscale phone-access setup"
```

---

## Final Verification

- [ ] `.venv/bin/python -m pytest tests/ -v` — full suite green.
- [ ] `make secrets` prints valid keys.
- [ ] Manual (Android, over the `ts.net` URL): set `APP_PASSWORD` + VAPID keys in `.env`, rebuild/restart the container (only when no job is running — check `curl localhost:8000/api/jobs`), `tailscale serve --bg 8000`, sign in, Enable notifications, `POST /api/push/test` (or the button), then run a short real rip and confirm a notification arrives with the phone locked.
- [ ] On landing, update `dvd-rip-sfo` (Tailscale origin satisfies its off-LAN goal) and close the beads epic + children.
