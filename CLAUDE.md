# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

**ZSTM** — a SAP Fiori (SAPUI5 freestyle) shop-floor app + Python Flask backend
that moves stock **between storage locations within the same plant** (SAP
**transfer posting, movement type 311**) by scanning a QR code.

Flow: the operator **signs in** by scanning a login **badge** (which alone signs
them in), or by typing their **PIN** → enters/scans the destination context →
scans a doff/beam QR → the
app resolves it against HANA and either **posts the 311 move** to SAP or saves a
**"Doff in Transit"** row, showing the material document number.

Scaffolded from the sibling `ZWFN` finishing app, but the backend is
fundamentally different: it posts a **real SAP goods movement**, it does not
insert into a custom Z-table (the transit log is a secondary path, below).

## Run / develop

No build step, no Node toolchain.

```bash
pip install -r server/requirements.txt
cp server/.env.example server/.env
python server/app.py          # http://localhost:8000 (PORT default 8010 in .env.example)
```

- `SAP_RFC_MOCK=true` (default in `.env.example`) runs the whole flow — including
  **login** and **badge lookup** — with fakes and **no** SAP/pyrfc/HANA needed:
  any non-empty user/password signs in, and badges live in an in-memory list
  (`_MOCK_BADGES`, reset each restart). Use it for UI work.
- Real posting needs `pyrfc` + the SAP NW RFC SDK and the `SAP_*` connection vars.
- HANA (`HANA_*` vars) is used for the MARA base-unit lookup, the doff resolve,
  and the persistent login badges.

## Architecture

**Single origin, no CORS.** `server/app.py` serves `../webapp` at `/` and the API
under `/api/*`. Front end uses relative `fetch` paths — never hardcode an origin.

### Authentication & sessions

There is **no SAP user/password login**. The operator signs in with **either**
credential of their badge — whichever is convenient — and each on its own is
enough (single-factor, by design, for a fast shop-floor tap):

- `POST /api/auth/login-qr` — body `{qr}`. **Scanning the badge alone signs in.**
  The token is looked up in `ZSTM_LOGIN_QR`; refused unless the row exists,
  `ACTIVE='X'`, and `VALID_TO` (if set) is not past.
- `POST /api/auth/login-pin` — body `{pin}`. **The PIN alone signs in** (fallback
  when the card can't be scanned). Because PINs are salted hashes, the server
  scans the (small) active-badge set with `check_password_hash`; the **PIN is kept
  unique** (see admin, below) so it identifies exactly one operator.
- `POST /api/auth/logout` clears the session; `GET /api/auth/me` returns the
  current `{user, fullName, isAdmin}` or 401.

Both funnel through `_sign_in()` (session + audit). The QR token is stored
plaintext (it's the value on the card); the PIN is stored only as a salted
**werkzeug** hash in `PIN_HASH`. This is 1FA-either, not 2FA — a copied card **or**
a known PIN logs in, so treat both as credentials and revoke lost cards.

Every **successful login** writes one append-only audit row to **`ZSTM_LOGIN_LOG`**
(`db/ZSTM_LOGIN_LOG.sql`) via `_log_login`: SAP user, badge `QR_ID`, `METHOD`
(`'QR'` or `'PIN'`), timestamp, client IP (`X-Forwarded-For` first hop or
`remote_addr`), and user-agent. **Best-effort** — a HANA/table/grant failure is
swallowed so it never blocks a login — and only on success, so failed attempts are
not logged. Needs `GRANT SELECT, INSERT ON SAPHANADB.ZSTM_LOGIN_LOG TO ZMSQL;`.

The login response includes `isAdmin`. The session is `permanent` with
`PERMANENT_SESSION_LIFETIME = SESSION_HOURS` (default **8h**), `HttpOnly`,
`SameSite=Lax`, and `Secure` when `SESSION_COOKIE_SECURE=true` (set it in prod).
**`SESSION_SECRET` must be a long, stable random string** — if empty, the app
falls back to a random per-process key and warns, so every restart logs everyone
out. Endpoints that read/act on operator data are guarded by **`@require_login`**
(`/api/stock/resolve`, `/api/stock/transit`; 401 when no session).

### Badge administration (admin-only)

Users listed in the **`ADMIN_USERS`** env var (comma-separated SAP users) see a
**"Login badges"** screen. Its endpoints are guarded by **`@require_admin`**
(401 if not signed in, **403** if signed in but not an admin); leave `ADMIN_USERS`
empty to disable the screen entirely.

- `GET  /api/admin/qr` — list badges (tokens included for reprint; no PIN).
- `POST /api/admin/qr` — create a badge; body `{sapUser, fullName, pin}` (PIN must
  be 4–8 digits **and unique** among active badges — else **409**). The server mints
  a **20-char opaque token** (`secrets.token_hex(10)`), hashes the PIN, inserts a row
  (`QR_ID` = `MAX(TO_BIGINT(QR_ID))+1` padded to 10, retried once on a clash), and
  returns the token so the UI can print a QR card.
- `POST /api/admin/qr/pin` — reset a badge's PIN; body `{qrId, pin}` (same 4–8 digit
  + uniqueness rule, **409** on a clash).
- `POST /api/admin/qr/toggle` — revoke/enable by flipping `ACTIVE` (`'X'` / `''`).

**Bootstrap (important):** because there's no SAP login, the **first** admin badge
can't be made from the in-app screen. Seed it from the CLI:

```bash
python server/app.py --create-badge SAP_USER PIN "Full Name"   # prints the QR token
```

Then add that user to `ADMIN_USERS`. In **mock mode** the server auto-seeds a badge
for every `ADMIN_USERS` entry at startup (token = username lower-cased, PIN =
`MOCK_BADGE_PIN`, default `1234`) so dev needs no bootstrap.

**`ZSTM_LOGIN_QR`** (`db/ZSTM_LOGIN_QR.sql`) is the badge table; a badge is still a
bearer-ish credential, so keep read access tight, control printed cards, and revoke
lost ones (`ACTIVE=''`, never delete). The app needs
`GRANT SELECT, INSERT, UPDATE ON SAPHANADB.ZSTM_LOGIN_QR TO ZMSQL;`. Existing
installs add the PIN column with the `ALTER` at the bottom of that SQL file.

### Stock move / doff resolve

- `GET  /api/health` — RFC ping (or mock).
- `POST /api/stock/parse` — `{qr}` → parsed fields, no posting (QR-format testing).
- `POST /api/stock/resolve` *(login)* — resolve a scanned **doff/beam** QR
  (Lot/Loom/Beam) into candidate stock-move line(s) via HANA; only doffs with
  enough unrestricted stock are offered, the rest come back as SAP-style deficit
  messages. No posting.
- `POST /api/stock/move` — posts via **`BAPI_GOODSMVT_CREATE`** (GM_CODE `04`,
  MOVE_TYPE `311`) + `BAPI_TRANSACTION_COMMIT` over PyRFC; rolls back and returns
  `409` on a BAPI error. Base unit is looked up from `SAPHANADB.MARA` over
  `hdbcli` when HANA is configured, else `DEFAULT_UOM`.
- `POST /api/stock/transit` *(login)* — instead of posting a 311, insert each
  scanned doff as a **"Doff in Transit"** row into `ZSTM_TRANSIT_D`
  (`db/ZSTM_TRANSIT_D.sql`; `DOCID` = `MAX(TO_BIGINT(DOCID))+1`).

`parse_qr` accepts labeled `KEY:VALUE` QRs (with SAP/English aliases) or
positional text ordered by `QR_FIELD_ORDER`.

**NEVER** post stock by writing `MARD`/`MCHB`/`MKPF`/`MSEG` directly — always the
BAPI. Direct writes skip stock checks, the material document, number ranges and
update logs, corrupting inventory.

### Front end

`webapp/`, namespace `stock.transfer` — plain `fetch`, no OData:
`index.html` → `Component.js` → `manifest.json` (`rootView` = `view/StockMove`).

- `view/Login.fragment.xml` — the sign-in gate: a **badge** field (camera *Scan
  badge* or handheld scanner) that **signs in on scan/Enter**, *or* a **PIN** field
  with a *Sign in with PIN* button. Either alone works; no SAP user/password.
- `view/Admin.fragment.xml` — the "Login badges" screen; its header button is
  shown **only when `isAdmin`**. Badge cards are printed as QR codes using the
  vendored `qrcode-generator` lib.
- Camera QR scan uses vendored **jsQR** (`webapp/lib/jsQR.js`); the destination
  field gates scanning; a post in flight locks the UI against double-posting.

UI5 bootstraps from the **CDN** by default; see README for the offline/local
`webapp/resources/` runtime option.

## Conventions & gotchas

- **UI5 caches XML views** — hard-refresh (Ctrl+F5) after editing a `.view.xml`
  or `.fragment.xml`; XML comments must not contain `--`.
- **Secrets:** only `server/.env` (gitignored) holds real credentials. Set
  `SESSION_SECRET` (e.g. `python -c "import secrets; print(secrets.token_hex(32))"`)
  and `ADMIN_USERS` there.
- **Badge printing opens a new window** — allow pop-ups on the admin's device.
- **Logo** `webapp/img/logo.svg` is a placeholder.
- `Component-preload.js` 404 in console is expected (no optimized UI5 build).

## Deploy checklist (login)

1. DBA runs `db/ZSTM_LOGIN_QR.sql`, then
   `GRANT SELECT, INSERT, UPDATE ON SAPHANADB.ZSTM_LOGIN_QR TO ZMSQL;`
   and `db/ZSTM_LOGIN_LOG.sql`, then
   `GRANT SELECT, INSERT ON SAPHANADB.ZSTM_LOGIN_LOG TO ZMSQL;`
   (and `db/ZSTM_TRANSIT_D.sql` + its grant if the transit path is used).
2. Set `SESSION_SECRET`, `SESSION_COOKIE_SECURE=true` (HTTPS), and
   `ADMIN_USERS=<sap users>` in `server/.env`.
3. Seed the first admin badge:
   `python server/app.py --create-badge <ADMIN_USER> <PIN> "<Full Name>"`
   (encode the printed token on a card).
4. `git pull` + restart the service, hard-refresh the iPad.
5. The admin scans that badge (or types its PIN) → **Login badges** → adds users,
   each with a **unique** PIN → prints badges. Operators **scan the badge** (instant)
   or **type their PIN + Sign in**; a forgotten PIN is reset from the badge row
   (**key** icon).
