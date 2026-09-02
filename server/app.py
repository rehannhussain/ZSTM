"""
Flask backend for ZSTM - Storage-location Stock Move (transfer posting 311).

Serves the SAPUI5 (Fiori) app from ../webapp at / and exposes a small JSON API:

  GET  /api/health                 -> is SAP (and optional HANA) reachable
  POST /api/stock/parse            -> parse a plain pipe QR into fields (debug)
  POST /api/stock/resolve          -> resolve a beam/doff QR into move line(s)
  POST /api/stock/move             -> post the doff list as one 311 material doc

The move is posted to SAP via RFC using BAPI_GOODSMVT_CREATE (GM_CODE '04' =
MB1B transfer posting, movement type 311) followed by BAPI_TRANSACTION_COMMIT.
This is the *only* correct way to move standard MM stock - never write MARD /
MCHB / MSEG directly.

The QR label carries: Plant, Storage Location (from), Material, Batch, Quantity.
It has no unit of measure, so the base unit is looked up from SAPHANADB.MARA
(via hdbcli) when available, else a configurable default is used.

Configuration comes from environment variables (optionally a .env file next to
this module). See .env.example. No secrets live in this file.

Run:  python server/app.py   ->   http://localhost:8000

Dev without a live SAP system: set SAP_RFC_MOCK=true to return a fake material
document number so the UI can be exercised end to end.
"""

import os
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import Flask, request, jsonify, send_from_directory

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_dotenv():
    """Minimal .env loader (KEY=VALUE per line) so python-dotenv isn't required."""
    path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()


def _envbool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


PORT = int(os.environ.get("PORT", "8000"))
MANDT = os.environ.get("SAP_CLIENT", "900")

# --- SAP RFC (PyRFC / BAPI_GOODSMVT_CREATE) --------------------------------
SAP_RFC_MOCK = _envbool("SAP_RFC_MOCK", False)
# Connection params passed straight to pyrfc.Connection(**RFC_PARAMS). Either
# use a saprfc destination (SAP_DEST) or the classic ashost/sysnr set.
_rfc = {
    "user": os.environ.get("SAP_USER", ""),
    "passwd": os.environ.get("SAP_PASSWD", ""),
    "ashost": os.environ.get("SAP_ASHOST", ""),
    "sysnr": os.environ.get("SAP_SYSNR", ""),
    "client": os.environ.get("SAP_CLIENT", MANDT),
    "lang": os.environ.get("SAP_LANG", "EN"),
    "dest": os.environ.get("SAP_DEST", ""),
}
RFC_PARAMS = {k: v for k, v in _rfc.items() if v}

# Goods-movement details. GM_CODE 04 = MB1B (transfer posting); 311 = transfer
# stock-to-stock, storage location to storage location, within a plant.
GM_CODE = os.environ.get("SAP_GM_CODE", "04")
MOVE_TYPE = os.environ.get("SAP_MOVE_TYPE", "311")
# S/4HANA uses the 40-char MATERIAL_LONG field; classic ECC uses MATERIAL (18).
USE_MATERIAL_LONG = _envbool("SAP_USE_MATERIAL_LONG", True)
HEADER_TXT = os.environ.get("SAP_HEADER_TXT", "ZSTM")[:25]

# --- HANA (optional, only for base-unit lookup from MARA) ------------------
HANA = {
    "address": os.environ.get("HANA_HOST", ""),
    "port": int(os.environ.get("HANA_PORT", "0") or 0),
    "user": os.environ.get("HANA_USER", ""),
    "password": os.environ.get("HANA_PASSWORD", ""),
    "encrypt": os.environ.get("HANA_ENCRYPT", "true").lower() == "true",
}
HANA_TIMEOUT_MS = int(os.environ.get("HANA_TIMEOUT_MS", "5000"))
DEFAULT_UOM = os.environ.get("DEFAULT_UOM", "").strip().upper()

# --- Doff QR resolve (ZWV_DOF_D / ZWV_DOF_DD2) -----------------------------
# The scanned QR is a beam/doff label like '261042-528-1446-01 TRIAL':
#   part 0 = Lot no  (LOT_NO)         part 1 = Loom no (RIGHT(LOOM_NO,3))
#   part 2 = Beam no (LEGACY_NO)      part 3 = doff seq (not filtered)
# The lookup returns the source storage location, batch, output material, doff
# length and unit for every matching doff. Plant is fixed (DOFF_PLANT).
DOFF_PLANT = os.environ.get("DOFF_PLANT", "3000")

# --- QR parsing ------------------------------------------------------------
# Positional field order used when the QR is plain delimited text. Override via
# QR_FIELD_ORDER (comma-separated) if your labels use a different sequence.
QR_FIELD_ORDER = [
    f.strip().lower()
    for f in os.environ.get("QR_FIELD_ORDER", "plant,sloc,material,batch,qty,uom").split(",")
    if f.strip()
]

WEBAPP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "webapp"))


# ---------------------------------------------------------------------------
# QR parsing
# ---------------------------------------------------------------------------

# Accept several friendly aliases per logical field so labels can be labeled
# (KEY:VALUE) or positional. Keys are matched case-insensitively.
_QR_ALIASES = {
    "plant": ("plant", "werks", "werk", "pl"),
    "sloc": ("sloc", "stge_loc", "lgort", "storageloc", "storagelocation", "sl", "loc", "location"),
    "material": ("material", "matnr", "mat", "materialcode", "article", "sku"),
    "batch": ("batch", "charg", "batchno", "batch_no", "lot"),
    "qty": ("qty", "quantity", "menge", "erfmg", "qnt"),
    "uom": ("uom", "unit", "meins", "erfme", "um"),
}
_ALIAS_LOOKUP = {alias: canon for canon, al in _QR_ALIASES.items() for alias in al}


def parse_qr(raw):
    """
    Turn a raw QR string into {plant, sloc, material, batch, qty, uom}.

    Two shapes are supported:
      - Labeled: 'PLANT:1000;LGORT:0001;MATNR:ABC;CHARG:B1;QTY:5;UOM:PC'
        (separators ; | , newline or tab; KEY:VALUE or KEY=VALUE).
      - Positional: the same values in QR_FIELD_ORDER, delimited the same way.
    Missing fields come back as "". Values are stripped; codes are upper-cased,
    the material is left as-is (case can be significant).
    """
    s = (raw or "").strip()
    out = {k: "" for k in ("plant", "sloc", "material", "batch", "qty", "uom")}
    if not s:
        return out

    # Split on the common label separators (keep material spaces intact by only
    # splitting on the structural delimiters, not internal whitespace).
    tokens = [t.strip() for t in re.split(r"[;|,\r\n\t]+", s) if t.strip()]
    if not tokens:
        return out

    labeled = [t for t in tokens if re.match(r"^[A-Za-z_]{1,20}\s*[:=]", t)]
    if labeled and len(labeled) >= max(2, len(tokens) - 1):
        # Treat as labeled KEY:VALUE pairs.
        for t in tokens:
            m = re.match(r"^([A-Za-z_]{1,20})\s*[:=]\s*(.*)$", t)
            if not m:
                continue
            canon = _ALIAS_LOOKUP.get(m.group(1).strip().lower())
            if canon and not out[canon]:
                out[canon] = m.group(2).strip()
    else:
        # Positional mapping against QR_FIELD_ORDER.
        for i, val in enumerate(tokens):
            if i >= len(QR_FIELD_ORDER):
                break
            field = QR_FIELD_ORDER[i]
            if field in out:
                out[field] = val.strip()

    for f in ("plant", "sloc", "batch", "uom"):
        out[f] = out[f].upper()
    out["material"] = out["material"].strip()
    return out


def parse_doff_qr(raw):
    """Parse a beam/doff QR like '261042-528-1446-01 TRIAL' into its parts.

    The first whitespace-delimited token is used (this drops a trailing label
    such as 'TRIAL'); it is then split on '-':
        261042 - 528 - 1446 - 01
        lot      loom  beam   seq
    Returns {'lot','loom','beam','seq','raw'} (missing parts come back '').
    """
    s = (raw or "").strip()
    if not s:
        return {"lot": "", "loom": "", "beam": "", "seq": "", "raw": ""}
    token = s.split()[0]
    parts = [p.strip() for p in token.split("-") if p.strip()]

    def at(i):
        return parts[i] if i < len(parts) else ""

    return {"lot": at(0), "loom": at(1), "beam": at(2), "seq": at(3), "raw": token}


def _qty_str(value):
    """Format a HANA DECIMAL quantity as a plain string with no trailing zeros
    ('1894.000' -> '1894', '12.500' -> '12.5'). '' if not a number."""
    try:
        q = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return ""
    q = q.normalize()
    if q == q.to_integral_value():
        q = q.quantize(Decimal(1))
    return format(q, "f")


def _dec(value):
    """Parse a quantity into a Decimal; returns None if not a positive number."""
    try:
        d = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None
    return d if d > 0 else None


# ---------------------------------------------------------------------------
# HANA base-unit lookup (optional convenience)
# ---------------------------------------------------------------------------

def _hana_conn():
    if not HANA["address"] or not HANA["port"]:
        return None
    from hdbcli import dbapi
    return dbapi.connect(
        address=HANA["address"], port=HANA["port"], user=HANA["user"],
        password=HANA["password"], encrypt=HANA["encrypt"],
        sslValidateCertificate=False,
        connectTimeout=HANA_TIMEOUT_MS, communicationTimeout=HANA_TIMEOUT_MS,
    )


def lookup_uom(material):
    """Best-effort base unit of measure (MARA.MEINS) for a material. '' on miss."""
    if not material:
        return ""
    conn = None
    try:
        conn = _hana_conn()
        if conn is None:
            return ""
        cur = conn.cursor()
        # Match the material as given and, for numeric materials, its 18-char
        # zero-padded (ALPHA) form as stored in MARA.
        padded = material.zfill(18) if material.isdigit() else material
        cur.execute(
            "SELECT MEINS FROM SAPHANADB.MARA WHERE MANDT = ? AND MATNR IN (?, ?) LIMIT 1",
            [MANDT, material, padded],
        )
        row = cur.fetchone()
        cur.close()
        return (row[0] or "").strip() if row else ""
    except Exception:
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


_DOFF_SQL = (
    "SELECT A.STORAGE_LOCATION, B.BATCH_NO, A.OUT_MATNR, B.DOFF_LENGTH, "
    "A.OUT_UOM, B.DOFF_BATCHNO, A.ARTICLE "
    "FROM SAPHANADB.ZWV_DOF_D A "
    "INNER JOIN SAPHANADB.ZWV_DOF_DD2 B ON A.DOCID = B.DOCID "
    "WHERE A.MANDT = ? AND A.LOT_NO = ? AND A.LEGACY_NO = ? "
    "AND RIGHT(A.LOOM_NO, 3) = ? "
    "ORDER BY B.BATCH_NO"
)

# Current unrestricted-use stock for a batch at a storage location. On S/4HANA
# the MARD/MCHB aggregates are not maintained, so stock is summed from MATDOC
# (INSMK='' unrestricted, SOBKZ='' own stock; SHKZG S=receipt +, H=issue -).
_STOCK_SQL = (
    "SELECT SUM(CASE WHEN SHKZG = 'S' THEN TO_DECIMAL(MENGE) ELSE -TO_DECIMAL(MENGE) END) "
    "FROM SAPHANADB.MATDOC "
    "WHERE MANDT = ? AND MATNR = ? AND WERKS = ? AND LGORT = ? AND CHARG = ? "
    "AND INSMK = '' AND SOBKZ = ''"
)


def _unrestricted(cur, matnr_padded, plant, sloc, batch):
    """Net unrestricted-use quantity for a batch at a storage location (Decimal)."""
    cur.execute(_STOCK_SQL, [MANDT, matnr_padded, plant, sloc, batch])
    row = cur.fetchone()
    val = row[0] if row else None
    try:
        return Decimal(str(val)) if val is not None else Decimal(0)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def resolve_doff(lot, loom, beam):
    """Resolve a scanned beam/doff into its stock-move line(s) via HANA.

    Returns a list of dicts, one per doff:
      {plant, sloc, material, batch, uom, doffLength, doffBatchNo, article}
    'material' is the display form (leading zeros stripped for numeric numbers);
    it is re-padded (ALPHA) at posting time. Raises RuntimeError if HANA is not
    configured.
    """
    conn = _hana_conn()
    if conn is None:
        raise RuntimeError(
            "HANA is not configured for the doff lookup. Set HANA_HOST / "
            "HANA_PORT / HANA_USER / HANA_PASSWORD in server/.env."
        )
    lines = []
    try:
        cur = conn.cursor()
        cur.execute(_DOFF_SQL, [MANDT, lot, beam, loom])
        rows = cur.fetchall()
        for sloc, batch, matnr, length, uom, doffbn, article in rows:
            matnr_raw = (matnr or "").strip()          # ALPHA-padded, as stored
            mat = str(int(matnr_raw)) if matnr_raw.isdigit() else matnr_raw
            sloc = (sloc or "").strip()
            batch = (batch or "").strip()
            avail = _unrestricted(cur, matnr_raw, DOFF_PLANT, sloc, batch)
            lines.append({
                "plant": DOFF_PLANT,
                "sloc": sloc,
                "material": mat,                         # display; re-padded on post
                "batch": batch,
                "uom": (uom or "").strip() or DEFAULT_UOM or "M",
                "doffLength": _qty_str(length),
                "available": _qty_str(avail),
                "doffBatchNo": (doffbn or "").strip(),
                "article": (article or "").strip(),
            })
        cur.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return lines


# ---------------------------------------------------------------------------
# SAP RFC posting (BAPI_GOODSMVT_CREATE, movement type 311)
# ---------------------------------------------------------------------------

def _rfc_conn():
    """Open a PyRFC connection. Raises RuntimeError if pyrfc/params are missing."""
    if not RFC_PARAMS:
        raise RuntimeError(
            "SAP RFC is not configured. Set SAP_ASHOST/SAP_SYSNR/SAP_CLIENT/"
            "SAP_USER/SAP_PASSWD (or SAP_DEST) in server/.env, or SAP_RFC_MOCK=true."
        )
    try:
        from pyrfc import Connection
    except ImportError as exc:
        raise RuntimeError(
            "pyrfc is not installed (needs the SAP NW RFC SDK). Install it, or "
            "run with SAP_RFC_MOCK=true for UI development."
        ) from exc
    return Connection(**RFC_PARAMS)


def _alpha18(m):
    """ALPHA input conversion for classic 18-char material numbers: SAP stores a
    numeric material right-justified, zero-padded to 18 (e.g. '3100012685' ->
    '000000003100012685'). Non-numeric or longer numbers are passed unchanged."""
    m = (m or "").strip()
    return m.zfill(18) if (m.isdigit() and len(m) <= 18) else m


def _bapi_errors(return_rows):
    """Collect E/A messages from a BAPI RETURN table into a list of strings."""
    msgs = []
    for r in return_rows or []:
        if str(r.get("TYPE", "")).upper() in ("E", "A"):
            msgs.append(r.get("MESSAGE", "").strip() or
                        f"{r.get('ID','')} {r.get('NUMBER','')}")
    return msgs


def post_moves(items, to_sloc, user):
    """
    Post a batch of same-plant storage-location transfers (movement type 311)
    as ONE atomic material document.

    items: list of {plant, sloc, material, batch, uom, qty(Decimal)}.
    Returns {"matdoc": ..., "year": ..., "mock": bool} or raises ValueError with
    a user-facing message on a BAPI/validation error (nothing is committed).
    """
    if SAP_RFC_MOCK:
        return {
            "matdoc": "49" + datetime.now().strftime("%H%M%S%f")[:8],
            "year": str(date.today().year),
            "mock": True,
        }

    conn = _rfc_conn()
    try:
        gm_items = []
        for f in items:
            item = {
                "PLANT": f["plant"],
                "STGE_LOC": f["sloc"],      # issuing storage location (from)
                "MOVE_TYPE": MOVE_TYPE,     # 311
                "MOVE_STLOC": to_sloc,      # receiving storage location (to)
                "ENTRY_QNT": f["qty"],
                "ENTRY_UOM": f["uom"],
            }
            if f.get("batch"):
                item["BATCH"] = f["batch"]
            # Classic numeric materials (<=18 chars) must be ALPHA-padded and go
            # in MATERIAL; MATERIAL_LONG is for genuinely long (>18) numbers.
            material = f["material"]
            if USE_MATERIAL_LONG and len(material) > 18:
                item["MATERIAL_LONG"] = material
            else:
                item["MATERIAL"] = _alpha18(material)
            gm_items.append(item)

        # Record the operator on the material document. USNAM is always the RFC
        # logon user on this system, so the entered operator is stamped into the
        # document header text (BKTXT), e.g. "ZSTM KT_REHAN" (max 25 chars).
        header_txt = (HEADER_TXT + " " + (user or "")).strip()[:25]
        result = conn.call(
            "BAPI_GOODSMVT_CREATE",
            GOODSMVT_HEADER={
                "PSTNG_DATE": date.today(),
                "DOC_DATE": date.today(),
                "HEADER_TXT": header_txt,
                "PR_UNAME": (user or "")[:12],
            },
            GOODSMVT_CODE={"GM_CODE": GM_CODE},
            GOODSMVT_ITEM=gm_items,
        )

        errors = _bapi_errors(result.get("RETURN"))
        if errors:
            conn.call("BAPI_TRANSACTION_ROLLBACK")
            raise ValueError("; ".join(errors))

        headret = result.get("GOODSMVT_HEADRET") or {}
        matdoc = headret.get("MAT_DOC") or result.get("MATERIALDOCUMENT") or ""
        year = headret.get("DOC_YEAR") or result.get("MATDOCUMENTYEAR") or ""
        if not matdoc:
            conn.call("BAPI_TRANSACTION_ROLLBACK")
            raise ValueError("SAP did not return a material document number.")

        conn.call("BAPI_TRANSACTION_COMMIT", WAIT="X")
        return {"matdoc": str(matdoc), "year": str(year), "mock": False}
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# App + static SAPUI5 serving (same origin as the API, so no CORS needed)
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder=None)


@app.route("/")
def index():
    return send_from_directory(WEBAPP_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(WEBAPP_DIR, path)


@app.get("/api/health")
def api_health():
    """Report whether SAP RFC is reachable (RFC_PING). Mock mode always ok."""
    if SAP_RFC_MOCK:
        return jsonify({"ok": True, "mock": True})
    try:
        conn = _rfc_conn()
        try:
            conn.ping()
        finally:
            conn.close()
        return jsonify({"ok": True, "mock": False})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


def _resolve_fields(raw_qr):
    """Parse + validate a QR into a ready-to-post fields dict, or (None, error)."""
    f = parse_qr(raw_qr)
    missing = [k for k in ("plant", "sloc", "material", "batch") if not f[k]]
    if missing:
        return None, "QR is missing: " + ", ".join(missing)
    qty = _dec(f["qty"])
    if qty is None:
        return None, "QR quantity is missing or not a positive number."
    f["qty"] = qty
    if not f["uom"]:
        f["uom"] = lookup_uom(f["material"]) or DEFAULT_UOM
    if not f["uom"]:
        return None, ("No unit of measure: it is not in the QR and could not be "
                      "looked up. Set DEFAULT_UOM or add it to the QR.")
    return f, None


def _display(f):
    """JSON-safe echo of parsed fields (Decimal -> str)."""
    return {
        "plant": f["plant"], "sloc": f["sloc"], "material": f["material"],
        "batch": f["batch"], "qty": str(f["qty"]), "uom": f["uom"],
    }


@app.post("/api/stock/parse")
def api_stock_parse():
    """Parse-only: show what a QR resolves to without posting anything."""
    data = request.get_json(silent=True) or {}
    fields, err = _resolve_fields(data.get("qr"))
    if err:
        return jsonify({"error": err}), 400
    return jsonify(_display(fields))


@app.post("/api/stock/resolve")
def api_stock_resolve():
    """Resolve a scanned beam/doff QR into stock-move line(s). No posting."""
    data = request.get_json(silent=True) or {}
    parsed = parse_doff_qr(data.get("qr"))
    if not (parsed["lot"] and parsed["loom"] and parsed["beam"]):
        return jsonify({"error": "Could not read Lot / Loom / Beam from the QR.",
                        "parsed": parsed}), 400
    try:
        lines = resolve_doff(parsed["lot"], parsed["loom"], parsed["beam"])
    except RuntimeError as exc:           # HANA not configured
        return jsonify({"error": str(exc)}), 500
    except Exception as exc:              # query/communication failure
        return jsonify({"error": "Doff lookup failed: " + str(exc)}), 502
    if not lines:
        return jsonify({
            "error": "No doff found for Lot %s / Loom %s / Beam %s." % (
                parsed["lot"], parsed["loom"], parsed["beam"]),
            "parsed": parsed}), 404

    # Only offer doffs that have enough unrestricted stock to move; the rest come
    # back as SAP-style deficit messages so the operator knows why they were skipped.
    movable, deficits = [], []
    for ln in lines:
        need = _dec(ln["doffLength"]) or Decimal(0)
        try:
            avail = Decimal(ln.get("available") or "0")
        except (InvalidOperation, ValueError):
            avail = Decimal(0)
        if avail < need:
            short = need - (avail if avail > 0 else Decimal(0))
            deficits.append(
                "Deficit of BA Unrestricted-use %s %s : %s %s / %s batch %s" % (
                    _qty_str(short), ln["uom"], ln["material"], ln["plant"],
                    ln["sloc"], ln["batch"]))
        else:
            movable.append(ln)
    return jsonify({"parsed": parsed, "lines": movable, "deficits": deficits})


def _validate_items(raw_items, to_sloc):
    """Validate cart line(s) for posting -> (items, error).

    Each line needs plant/sloc/material/batch and a unit, a positive length that
    does not exceed its doff length, and a source SLoc different from the
    destination."""
    if not raw_items:
        return None, "Scan at least one doff before posting."
    items = []
    for idx, r in enumerate(raw_items, 1):
        plant = str(r.get("plant", "")).strip()
        sloc = str(r.get("sloc", "")).strip().upper()
        material = str(r.get("material", "")).strip()
        batch = str(r.get("batch", "")).strip()
        uom = str(r.get("uom", "")).strip().upper()
        label = batch or ("line %d" % idx)
        if not (plant and sloc and material and batch):
            return None, "Line %s is missing plant / storage location / material / batch." % label
        if not uom:
            return None, "Line %s has no unit of measure." % label
        qty = _dec(r.get("qty"))
        if qty is None:
            return None, "Enter a length greater than 0 for batch %s." % label
        doff = _dec(r.get("doffLength"))
        if doff is not None and qty > doff:
            return None, ("Length %s for batch %s is more than its doff length %s."
                          % (_qty_str(qty), label, _qty_str(doff)))
        if sloc == to_sloc:
            return None, ("Batch %s is already in %s - source and destination are the same."
                          % (label, to_sloc))
        items.append({"plant": plant, "sloc": sloc, "material": material,
                      "batch": batch, "uom": uom, "qty": qty})
    return items, None


@app.post("/api/stock/move")
def api_stock_move():
    """Post the scanned doff list as one same-plant 311 material document."""
    data = request.get_json(silent=True) or {}
    to_sloc = str(data.get("toSloc", "")).strip().upper()
    user = str(data.get("user", "")).strip()

    if not to_sloc:
        return jsonify({"error": "Enter the destination storage location first."}), 400

    items, err = _validate_items(data.get("items"), to_sloc)
    if err:
        return jsonify({"error": err}), 400

    try:
        res = post_moves(items, to_sloc, user)
    except RuntimeError as exc:          # not configured / pyrfc missing
        return jsonify({"error": str(exc)}), 500
    except ValueError as exc:            # BAPI/business error (surfaced to operator)
        return jsonify({"error": str(exc), "toSloc": to_sloc}), 409
    except Exception as exc:             # RFC/communication failure
        return jsonify({"error": "SAP posting failed: " + str(exc)}), 502

    moved = [{"plant": it["plant"], "sloc": it["sloc"], "material": it["material"],
              "batch": it["batch"], "qty": _qty_str(it["qty"]), "uom": it["uom"]}
             for it in items]
    body = {"matdoc": res["matdoc"], "year": res["year"], "mock": res["mock"],
            "toSloc": to_sloc, "moved": moved}
    return jsonify(body), 201


def _ssl_context():
    """
    HTTPS is required for the iPad camera scan (Safari blocks getUserMedia over
    plain http:// on a LAN address). Enable with USE_HTTPS=true; uses
    server/cert.pem + server/key.pem when present, else a throwaway adhoc cert.
    """
    if not _envbool("USE_HTTPS", False):
        return None
    here = os.path.dirname(__file__)
    cert = os.path.join(here, "cert.pem")
    key = os.path.join(here, "key.pem")
    if os.path.exists(cert) and os.path.exists(key):
        return (cert, key)
    return "adhoc"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True, ssl_context=_ssl_context())
