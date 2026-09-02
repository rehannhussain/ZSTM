# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

**ZSTM** — a SAP Fiori (SAPUI5 freestyle) shop-floor app + Python Flask backend
that moves stock **between storage locations within the same plant** (SAP
**transfer posting, movement type 311**) by scanning a QR code.

Flow: operator enters the **destination storage location** → scans a QR
(`Plant, SLoc-from, Material, Batch, Qty`) → the app **posts the move
immediately** to SAP and shows the material document number.

Scaffolded from the sibling `ZWFN` finishing app, but the backend is
fundamentally different: it posts a **real SAP goods movement**, it does not
insert into a custom Z-table.

## Run / develop

No build step, no Node toolchain.

```bash
pip install -r server/requirements.txt
cp server/.env.example server/.env
python server/app.py          # http://localhost:8000
```

- `SAP_RFC_MOCK=true` (default in `.env.example`) runs the whole flow with a fake
  material document number and **no** SAP/pyrfc needed — use it for UI work.
- Real posting needs `pyrfc` + the SAP NW RFC SDK and the `SAP_*` connection vars.

## Architecture

**Single origin, no CORS.** `server/app.py` serves `../webapp` at `/` and the API
under `/api/*`. Front end uses relative `fetch` paths — never hardcode an origin.

**Back end** (`server/app.py`, Flask):
- `GET /api/health` — RFC ping (or mock).
- `POST /api/stock/parse` — `{qr}` → parsed fields, no posting (QR-format testing).
- `POST /api/stock/move` — `{qr, toSloc, user}` → posts via
  **`BAPI_GOODSMVT_CREATE`** (GM_CODE `04`, MOVE_TYPE `311`) + `BAPI_TRANSACTION_COMMIT`
  over PyRFC; rolls back and returns `409` on a BAPI error. Base unit is looked up
  from `SAPHANADB.MARA` over `hdbcli` when HANA is configured, else `DEFAULT_UOM`.
- `parse_qr` accepts labeled `KEY:VALUE` QRs (with SAP/English aliases) or
  positional text ordered by `QR_FIELD_ORDER`.

**NEVER** post stock by writing `MARD`/`MCHB`/`MKPF`/`MSEG` directly — always the
BAPI. Direct writes skip stock checks, the material document, number ranges and
update logs, corrupting inventory.

**Front end** (`webapp/`, namespace `stock.transfer`) — plain `fetch`, no OData:
`index.html` → `Component.js` → `manifest.json` (`rootView` = `view/StockMove`).
Camera QR scan uses vendored **jsQR** (`webapp/lib/jsQR.js`); the destination
field gates scanning; a post in flight locks the UI against double-posting.

UI5 bootstraps from the **CDN** by default; see README for the offline/local
`webapp/resources/` runtime option.

## Conventions & gotchas

- **UI5 caches XML views** — hard-refresh (Ctrl+F5) after editing a `.view.xml`;
  XML comments must not contain `--`.
- **Secrets:** only `server/.env` (gitignored) holds real credentials.
- **Logo** `webapp/img/logo.svg` is a placeholder.
- `Component-preload.js` 404 in console is expected (no optimized UI5 build).
