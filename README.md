# ZSTM — Storage-Location Stock Move (transfer posting 311)

A SAP Fiori (SAPUI5 freestyle) shop-floor app + small Python Flask backend that
moves stock **from one storage location to another within the same plant** by
scanning a QR code.

**Flow:** the operator enters the **destination storage location** once → scans a
QR (handheld or iPad camera) → the app **immediately posts** the transfer to SAP
and shows the resulting material document number. Scan the next label to move
another pallet to the same destination.

The QR carries: **Plant, Storage Location (from), Material, Batch, Quantity.**

## How the move is posted (important)

Moving standard MM stock is **not** a direct table write. This app posts a real
goods movement through SAP:

```
BAPI_GOODSMVT_CREATE   (GM_CODE '04' = MB1B transfer posting, movement type 311)
BAPI_TRANSACTION_COMMIT (WAIT = 'X')     # or ..._ROLLBACK on a BAPI error
```

called over RFC via **PyRFC**. Never write `MARD` / `MCHB` / `MKPF` / `MSEG`
directly — that bypasses stock checks, the material document, number ranges and
update logs, and corrupts inventory. The QR has no unit of measure, so the base
unit is looked up from `SAPHANADB.MARA` over HANA when configured, else
`DEFAULT_UOM` is used.

## Run / develop

No build step, no Node toolchain.

```bash
pip install -r server/requirements.txt      # flask (+ pyrfc, hdbcli)
cp server/.env.example server/.env           # then fill in real values
python server/app.py                         # serves UI + API at http://localhost:8000
```

- `PORT` changes the port (default `8000`).
- Flask runs with `debug=True`; when restarting, make sure the port is free.

### Try it without a live SAP system

Set `SAP_RFC_MOCK=true` in `server/.env` (the default in `.env.example`). The
backend then returns a fake material document number so you can exercise the full
scan → post → result flow. `pyrfc` is not needed in mock mode.

### PyRFC prerequisites (real posting)

`pyrfc` is **not on PyPI** — `pip install pyrfc` always fails ("No matching
distribution"). It ships as prebuilt wheels on SAP's GitHub, built against the
**SAP NW RFC SDK**. To enable real posting:

1. Download the **SAP NW RFC SDK** (SAP ONE Support, S-user required), unzip it,
   and set `SAPNWRFC_HOME` to its folder; put its `lib` directory on the path
   (Windows: add `...\nwrfcsdk\lib` to `PATH`).
2. Install a PyRFC wheel matching your Python (here CPython **3.12**) from
   <https://github.com/SAP/PyRFC/releases>, e.g.:
   ```bash
   pip install https://github.com/SAP/PyRFC/releases/download/<version>/pyrfc-<version>-cp312-cp312-win_amd64.whl
   ```
   (No matching wheel? Build from source: install the SDK as above, then
   `pip install cython` and `pip install .` from a PyRFC source checkout.)
3. Set `SAP_RFC_MOCK=false` and the `SAP_ASHOST` / `SAP_SYSNR` / `SAP_CLIENT` /
   `SAP_USER` / `SAP_PASSWD` vars (or a `SAP_DEST` from `sapnwrfc.ini`).

Until then, keep `SAP_RFC_MOCK=true` — the app runs the full scan → post → result
flow with a fake material document and needs none of the above.

## API

- `GET  /api/health` — is SAP reachable (RFC ping); reports mock mode.
- `POST /api/stock/parse` — `{ "qr": "<raw>" }` → parsed `{plant,sloc,material,batch,qty,uom}` (no posting). Handy for testing QR formats.
- `POST /api/stock/move` — `{ "qr": "<raw>", "toSloc": "0002", "user": "OP1" }` →
  posts the 311 transfer, returns `{ matdoc, year, toSloc, moved:{...} }`.
  `409` with `error` for a BAPI/business failure (e.g. stock deficit); the move
  is rolled back.

## QR format

Two shapes are accepted (`server/app.py` `parse_qr`):

- **Labeled** — `PLANT:1000;LGORT:0001;MATNR:ABC123;CHARG:B77;QTY:5;UOM:PC`
  (separators `;` `|` `,` newline/tab; `KEY:VALUE` or `KEY=VALUE`; field aliases
  cover the common English/SAP names).
- **Positional** — the same values in the order set by `QR_FIELD_ORDER`
  (default `plant,sloc,material,batch,qty,uom`; `uom` optional).

Adjust `QR_FIELD_ORDER` in `.env` if your labels differ. Test any label quickly
with `POST /api/stock/parse` before wiring up posting.

## HTTPS (required for the iPad camera)

Safari blocks `getUserMedia` over plain `http://` on a LAN IP, so the camera scan
needs HTTPS. Run with `USE_HTTPS=true`; the server uses `server/cert.pem` +
`server/key.pem` if present, else a throwaway adhoc cert. A stable cert:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout server/key.pem -out server/cert.pem -subj "/CN=zstm-stockmove" \
  -addext "subjectAltName=IP:<YOUR_LAN_IP>,IP:127.0.0.1,DNS:localhost"
```

Handheld scanners (keyboard-wedge) work fine over plain HTTP — the camera is the
only part that needs HTTPS.

## Offline / LAN UI5 runtime (optional)

`webapp/index.html` bootstraps UI5 from the public OpenUI5 **CDN** by default, so
the repo runs with nothing vendored. For an offline shop-floor device, download
the 1.120.30 runtime into `webapp/resources/` (gitignored) and point the
bootstrap `src` at `resources/sap-ui-core.js`:

```bash
curl -sL -o /tmp/ui5.zip https://github.com/SAP/openui5/releases/download/1.120.30/openui5-runtime-1.120.30.zip
python -c "import zipfile; z=zipfile.ZipFile('/tmp/ui5.zip'); z.extractall('webapp', [n for n in z.namelist() if n.startswith('resources/') and not n.endswith('/')])"
```

## Notes & gotchas

- **Single origin, no CORS.** Flask serves the UI at `/` and the API at `/api/*`;
  the front end uses relative `fetch` paths — never hardcode a host/port.
- **Auto-post on scan.** A scan posts a real movement. The destination field is a
  deliberate gate (scanning is disabled until it is filled); a post in flight
  locks the UI to prevent double-posting; the scan field clears on success and is
  kept on error for inspection.
- **UI5 caches XML views.** After editing a `.view.xml`, hard-refresh (Ctrl+F5).
  XML comments must not contain `--` (double hyphen) or the view fails to parse.
- **Secrets:** only `server/.env` (gitignored) holds real credentials; keep
  `server/.env.example` as placeholders.
- **Logo** is a placeholder (`webapp/img/logo.svg`); replace with your asset.
