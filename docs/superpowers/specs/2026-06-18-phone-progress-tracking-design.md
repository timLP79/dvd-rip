# Phone Progress Tracking — Design

**Date:** 2026-06-18
**Status:** Approved (brainstorming) — pending implementation plan
**Related:** `dvd-rip-sfo` (Secure remote access) — largely satisfied by the Tailscale choice here.

## Context & Goal

The user wants to monitor (and incidentally control) DVD/Blu-ray rips from an
**Android phone**, both on the home LAN and beyond it, and receive a **push
notification when a rip finishes or fails** — including when the phone is locked
and the page is closed.

The app already binds `0.0.0.0:8000` and Docker publishes `8000:8000`, so it is
*already* LAN-reachable at `http://<host-ip>:8000`. The gap is not raw
reachability; it is: (1) a usable mobile UI, (2) a stable/easy way to reach it,
and (3) push notifications. Web Push is the chosen mechanism, which forces an
HTTPS secure context — solved with Tailscale.

### In scope
- HTTPS secure origin via Tailscale `serve` (ops/docs; no container change).
- Installable PWA shell (manifest + service worker).
- Self-hosted Web Push (VAPID) firing on terminal job states.
- App-level shared-password auth (session cookie) layered on top of the tailnet.
- Mobile UI polish (CSS-only responsive reflow of the Titles table, tap targets).

### Out of scope (YAGNI)
- Offline caching / full PWA offline support (service worker handles push only).
- Progress-milestone notifications (terminal states only).
- iOS-specific handling (user is Android-only; design avoids iOS-only hacks but
  the standard path also happens to work on iOS 16.4+ if ever needed).
- Multi-user accounts / per-user auth (single shared password).
- Inserting/ejecting media remotely (physical constraint — a disc must be loaded
  at home).

## Decisions (resolved during brainstorming)

| Question | Decision |
|---|---|
| Notification mechanism | PWA + self-hosted VAPID **Web Push** (works when locked/closed) |
| HTTPS secure origin | **Tailscale `serve`** (public Let's Encrypt cert, stable `*.ts.net` name, off-LAN too) |
| Phone OS | **Android** (Web Push works straight from Chrome over HTTPS) |
| Auth | **Shared-password login + signed session cookie** (defense-in-depth) |
| Push triggers | **Terminal states only**: done / failed / cancelled |
| Notify control placement | **Jobs section header** (single restrained bell toggle) |

## Architecture

Two layers: an **ops/setup layer** (Tailscale, no app code) and an **app-code layer**.

```
Android phone (Chrome / installed PWA)
        │  https://<host>.<tailnet>.ts.net   (Tailscale serve, public cert)
        ▼
Tailscale on host ──proxy──▶ 127.0.0.1:8000  (existing published container port)
        ▼
FastAPI ── SessionMiddleware (auth) ── routes ── ripper ──▶ HandBrake / makemkvcon
        │                                          │
        │  /api/push/*  (subscribe/key/test)       │ on terminal state:
        ▼                                          ▼
   library.db: push_subscriptions          app/push.py ──pywebpush──▶ Google/Apple
                                                                       push service
                                                                          │
                                                                          ▼
                                                              phone SW `push` event
                                                              → showNotification(...)
```

The raw `:8000` port stays LAN-only and is **never** port-forwarded to the public
internet. Access from outside the LAN is exclusively through the tailnet.

## Components

### 1. Tailscale HTTPS origin — *ops/docs only, no container change*
- Install Tailscale on host and phone (one tailnet). Enable MagicDNS + HTTPS certs.
- `tailscale serve --bg 8000` → app served at `https://<host>.<tailnet>.ts.net`
  with an auto-renewing publicly-trusted cert (the Web Push secure context).
- Documented in `README` and `.env.example`. No Dockerfile/compose change — the
  container keeps publishing `8000` on the host loopback; Tailscale proxies to it.
- **Dependency note:** Tailscale's coordination plane is third-party; the data
  plane is peer-to-peer WireGuard. Headscale is a self-hostable control-plane
  alternative if ever desired (not implemented now).

### 2. App-level auth — *new middleware + login route in `main.py`*
- **Responsibility:** require a shared password before any sensitive route.
- **Mechanism:** Starlette `SessionMiddleware` (signed cookie) + a `/login`
  (GET form, POST handler) that compares the submitted password against
  `APP_PASSWORD` and sets `request.session["authed"] = True`.
- **Cookie:** `HttpOnly`, `SameSite=Lax`, `https_only=True`, long `max_age`
  (e.g. 60 days) so the phone stays logged in.
- **Enforcement:** a dependency / middleware that 401s `/api/*` and redirects
  HTML routes to `/login` when unauthenticated.
- **Exempt paths:** `/static/*` (CSS/JS/manifest/sw/icons — not secret; lets the
  PWA shell + service worker load pre-login) and `/login`.
- **Feature-gate:** if `APP_PASSWORD` is unset, auth is disabled entirely (keeps
  `make dev` on localhost frictionless; mirrors the TMDB/VAPID gating pattern).
- **Secret:** `SESSION_SECRET` in `.env` (signs the cookie).

### 3. PWA shell — *new static files + template tags*
- `app/static/manifest.webmanifest`: `display: standalone`, `start_url: /`,
  `theme_color`/`background_color` from the blue-gray palette (`#0c0e13` /
  accent `#4b73b8`), `name`/`short_name`, icons.
- Icons: 192×192, 512×512, plus a 512 maskable — a restrained disc glyph in
  Prussian Ink (on-brand, not decorated). Stored under `app/static/icons/`.
- Template `<head>` (both `index.html`, `library.html`): `<link rel="manifest">`,
  `<meta name="theme-color">`, `apple-touch-icon` (harmless on Android).
- `app/static/sw.js` — minimal service worker:
  - `push` event → `self.registration.showNotification(title, {body, icon, tag, data})`.
  - `notificationclick` → focus an existing client or open `/` (Jobs).
  - **No fetch/caching handler** (out of scope).

### 4. Web Push backend — *new `app/push.py`, `models.py`, `library.py`, `main.py`*
- **Dependency:** add `pywebpush` (pulls `py-vapid`, `cryptography`, `http-ecdsa`)
  to `requirements.txt`.
- **VAPID keys:** generated once; stored in `.env` as `VAPID_PUBLIC_KEY`,
  `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` (`mailto:`). **Feature-gated:** absent
  keys ⇒ push disabled, `/api/push/key` reports disabled, UI hides the bell.
- **Subscription storage (`library.py`** — keeps it the only SQL module):
  - New table `push_subscriptions(endpoint TEXT PRIMARY KEY, p256dh TEXT,
    auth TEXT, created_at TEXT)`.
  - CRUD: `add_subscription`, `remove_subscription(endpoint)`,
    `list_subscriptions()`. Idempotent insert (upsert on endpoint).
- **Send logic (`app/push.py`** — new module, keeps `ripper.py` thin and
  `library.py` SQL-only):
  - `notify_job(job)` — build payload `{title, body, url}` from job title +
    terminal status, then send to all stored subscriptions.
  - **Runs `pywebpush` (synchronous) in a thread executor** so it never blocks
    the async rip worker.
  - **Prune dead subscriptions:** on `WebPushException` with 404/410, delete that
    endpoint via `library.remove_subscription`.
  - All exceptions caught/logged — a push failure never affects job outcome.
- **Endpoints (`main.py`):**
  - `GET  /api/push/key` → `{enabled: bool, key: <vapid public key>}`.
  - `POST /api/push/subscribe` → body = browser `PushSubscription` JSON → store.
  - `POST /api/push/unsubscribe` → body = `{endpoint}` → remove.
  - `POST /api/push/test` → send a test notification to all subs (wiring check).
- **Model (`models.py`):** `PushSubscription` (endpoint, keys.p256dh, keys.auth).

### 5. Trigger — *one hook in `ripper.py`*
- `Ripper._update()` is the single choke point for status transitions and already
  records terminal jobs to the library. After the existing terminal-state library
  record (status ∈ DONE/FAILED/CANCELLED), call `push.notify_job(job)`.
- Guarded so a push error is logged and swallowed (never propagates into the
  worker / job state).

### 6. Notify UI — *`index.html` + `app.js` + `style.css`*
- A single restrained **bell toggle in the Jobs section header**.
- States: unsupported → hidden; `default` → "Enable notifications"; granted +
  subscribed → "On" (click to unsubscribe); `denied` → hint; insecure origin
  (loaded over plain `http`) → "Open via the HTTPS address to enable."
- Enable flow: fetch `/api/push/key` → register `/static/sw.js` →
  `Notification.requestPermission()` → `registration.pushManager.subscribe({
  userVisibleOnly: true, applicationServerKey })` → `POST /api/push/subscribe`.
- Accent color only on the active state (matches recorded UI prefs — clean/utility).

### 7. Mobile UI polish — *CSS only (`style.css`)*
- Under the existing `@media (max-width: 820px)` breakpoint:
  - Reflow the 6-column Titles `<table>` into **stacked cards** using CSS
    `data-label` pseudo-elements (no JS/template change) so it is usable at ~390px.
  - Larger tap targets for checkboxes/buttons; comfortable row spacing.
  - Respect `env(safe-area-inset-*)` and standalone display chrome.
- Jobs/progress view (the primary phone path) is already close — verify the
  progress bar and Cancel button are thumb-friendly.

## Data Flow (push happy path)

1. Phone loads PWA over `https://<host>.<tailnet>.ts.net`, logs in (password →
   session cookie).
2. User taps the bell → SW registers → permission granted → `pushManager.subscribe`
   with the VAPID public key → subscription POSTed → row in `push_subscriptions`.
3. A rip runs (started from desktop or phone) → `Ripper._update` transitions the
   job to DONE/FAILED/CANCELLED → records to library → `push.notify_job`.
4. `push.py` sends (in an executor) to every stored subscription via `pywebpush`;
   Google/Apple push service delivers to the phone.
5. The SW `push` handler shows `"Inception (2010) — done"`. Tap → opens Jobs.
6. Any 404/410 responses prune stale subscriptions.

## Configuration & Secrets (`.env`)

| Var | Purpose | Gate |
|---|---|---|
| `APP_PASSWORD` | Shared login password | unset ⇒ auth disabled |
| `SESSION_SECRET` | Signs the session cookie | required if `APP_PASSWORD` set |
| `VAPID_PUBLIC_KEY` | Client subscribe key | unset ⇒ push disabled |
| `VAPID_PRIVATE_KEY` | Server signing key | unset ⇒ push disabled |
| `VAPID_SUBJECT` | `mailto:` contact for push services | with VAPID keys |

- A **`make secrets`** helper generates `SESSION_SECRET` (random) + a VAPID
  keypair and prints the `.env` lines to paste. `.env.example` documents all five.

## Security Model

- **Layer 1 — network:** only tailnet devices can reach the `ts.net` origin;
  raw `:8000` never leaves the LAN. Tightenable with Tailscale ACLs.
- **Layer 2 — app:** shared-password session required for all sensitive routes.
- Subprocess calls remain argv-based (no shell injection) — unchanged.
- Cookie is `HttpOnly`/`Secure`/`SameSite=Lax`; secret in `.env` (gitignored).
- VAPID private key in `.env`; never sent to the client (only the public key is).

## Error Handling

- **Push disabled** (no VAPID keys): `/api/push/*` report disabled; UI hides bell.
- **Auth disabled** (no `APP_PASSWORD`): all routes open (dev/localhost only).
- **Dead subscription** (404/410): pruned on send.
- **Push send failure:** caught + logged in `push.py`; job outcome unaffected.
- **Insecure origin** (plain `http`): SW registration fails; UI shows the
  "open via HTTPS" hint instead of a broken subscribe.
- **Synchronous `pywebpush`:** offloaded to a thread executor so the async worker
  never blocks.

## Testing

- **Unit (pytest, no real push service / no real drive):**
  - `push.py` payload construction + prune-on-410 (mock `pywebpush`).
  - `library.py` `push_subscriptions` CRUD (upsert idempotency, remove).
  - `/api/push/subscribe|unsubscribe|key|test` via `TestClient`.
  - Auth: protected route 401/redirect without session; passes with valid login;
    fully open when `APP_PASSWORD` unset; `/static/*` reachable pre-login.
  - Feature-gate-off behavior for both auth and push.
- **Hardware/manual (Android over `ts.net`):** install PWA → log in → enable
  notifications → `/api/push/test` → run a short real rip → confirm the
  notification arrives with the phone locked and the tab closed.

## Beads Breakdown (proposed)

Epic: *Phone progress tracking (PWA + Web Push)*. Children:
1. Tailscale HTTPS serve — docs + `.env.example` + `make secrets`.
2. App-level auth (SessionMiddleware + `/login` + gating).
3. PWA shell (manifest + icons + service worker + template tags).
4. Web Push backend (`push.py`, `push_subscriptions`, endpoints, `pywebpush`).
5. Push trigger hook in `ripper._update`.
6. Notify UI (bell toggle + subscribe flow).
7. Mobile UI polish (responsive Titles cards + tap targets).

On landing, annotate `dvd-rip-sfo` — the Tailscale origin satisfies its
"control rips from outside the LAN" goal; close or relabel accordingly.
