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
