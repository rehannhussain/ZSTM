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
import sys
import secrets
import mimetypes
from functools import wraps
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import Flask, request, jsonify, send_from_directory, session
from werkzeug.security import generate_password_hash, check_password_hash

# Serve the PWA manifest with the correct content type (some Python installs
# don't know .webmanifest, and browsers ignore a manifest served as octet-stream).
mimetypes.add_type("application/manifest+json", ".webmanifest")

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


def _register_nwrfc_sdk():
    """Make the SAP NW RFC SDK's DLLs (sapnwrfc.dll + icu*57.dll) loadable on
    Windows without the user editing PATH. PyRFC dlopen's sapnwrfc.dll, which in
    turn needs the ICU 57 DLLs beside it; if that folder isn't on the DLL search
    path the loader fails with 'Could not open the ICU common library'
    (icuuc57/icudt57/icuin57). We add <SAPNWRFC_HOME>\\lib to the search path.
    No-op off Windows or when the SDK isn't found."""
    if os.name != "nt":
        return
    home = os.environ.get("SAPNWRFC_HOME", r"C:\SAP\nwrfcsdk")
    libdir = os.path.join(home, "lib")
    if not os.path.isdir(libdir):
        return
    # Python 3.8+: PATH alone no longer affects DLL resolution for extensions.
    add = getattr(os, "add_dll_directory", None)
    if add:
        try:
            add(libdir)
        except OSError:
            pass
    # Keep PATH in sync too (helps the SDK's own internal lookups / older Pythons).
    if libdir.lower() not in os.environ.get("PATH", "").lower():
        os.environ["PATH"] = libdir + os.pathsep + os.environ.get("PATH", "")


_register_nwrfc_sdk()


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

# --- Session / operator login ---------------------------------------------
# Operators sign in ONLY by scanning their login badge (QR token) and entering
# their PIN -- see ZSTM_LOGIN_QR. There is no SAP user/password login. The
# session then carries the operator id for 8 hours. SESSION_SECRET must be a
# stable value in .env for logins to survive a restart.
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "8"))
SESSION_COOKIE_SECURE = _envbool("SESSION_COOKIE_SECURE", False)

# Custom "Doff in Transit" log table (created by db/ZSTM_TRANSIT_D.sql).
TRANSIT_TABLE = os.environ.get("ZSTM_TRANSIT_TABLE", "SAPHANADB.ZSTM_TRANSIT_D")

# QR-badge login table (created by db/ZSTM_LOGIN_QR.sql) + who may manage badges.
LOGIN_QR_TABLE = os.environ.get("ZSTM_LOGIN_QR_TABLE", "SAPHANADB.ZSTM_LOGIN_QR")
ADMIN_USERS = set(u.strip().upper() for u in os.environ.get("ADMIN_USERS", "").split(",") if u.strip())
# Login audit trail (created by db/ZSTM_LOGIN_LOG.sql): one row per sign-in.
LOGIN_LOG_TABLE = os.environ.get("ZSTM_LOGIN_LOG_TABLE", "SAPHANADB.ZSTM_LOGIN_LOG")
# In-memory stores used only in SAP_RFC_MOCK mode so the admin + QR-login flow
# can be exercised without HANA. Never used against a real system.
_MOCK_BADGES = []
_MOCK_LOGINS = []

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

# The doff master's OUT_MATNR is an unreliable placeholder on some beams, so the
# real material for the move is derived from the batch itself. Stock (and thus the
# stocked material) is read from MATDOC on S/4HANA - the MARD/MCHB aggregates are
# not maintained. INSMK='' unrestricted, SOBKZ='' own stock; SHKZG S=receipt(+)/H=issue(-).
_MATDOC_MAT_SQL = (
    "SELECT MATNR, SUM(CASE WHEN SHKZG = 'S' THEN TO_DECIMAL(MENGE) ELSE -TO_DECIMAL(MENGE) END) AS NET "
    "FROM SAPHANADB.MATDOC "
    "WHERE MANDT = ? AND WERKS = ? AND LGORT = ? AND CHARG = ? AND INSMK = '' AND SOBKZ = '' "
    "GROUP BY MATNR ORDER BY NET DESC"
)
# Authoritative material<->batch link (batch master), used when the batch has no
# stock at the source location so a deficit message still names the right material.
_MCH1_SQL = "SELECT MATNR FROM SAPHANADB.MCH1 WHERE MANDT = ? AND CHARG = ? LIMIT 1"


def _batch_stock(cur, plant, sloc, batch):
    """Resolve (material, unrestricted_qty) for a batch at a storage location.

    The material is the one the batch is actually stocked under at that location
    (from MATDOC); if there is no stock there, the material falls back to the batch
    master (MCH1) and the quantity to 0. Returns (matnr_padded, Decimal)."""
    cur.execute(_MATDOC_MAT_SQL, [MANDT, plant, sloc, batch])
    for matnr, net in cur.fetchall():
        try:
            qty = Decimal(str(net)) if net is not None else Decimal(0)
        except (InvalidOperation, ValueError, TypeError):
            qty = Decimal(0)
        if qty > 0:
            return (matnr or "").strip(), qty
    cur.execute(_MCH1_SQL, [MANDT, batch])
    row = cur.fetchone()
    return ((row[0] or "").strip() if row else ""), Decimal(0)


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
        for sloc, batch, out_matnr, length, uom, doffbn, article in rows:
            sloc = (sloc or "").strip()
            batch = (batch or "").strip()
            # Material comes from the batch's actual stock, not the doff OUT_MATNR
            # (which is an unreliable placeholder); OUT_MATNR is only a last resort.
            matnr_raw, avail = _batch_stock(cur, DOFF_PLANT, sloc, batch)
            if not matnr_raw:
                matnr_raw = (out_matnr or "").strip()
            mat = str(int(matnr_raw)) if matnr_raw.isdigit() else matnr_raw
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
app.secret_key = SESSION_SECRET or os.urandom(32)
if not SESSION_SECRET:
    print("WARNING: SESSION_SECRET is not set - operator sessions will reset on "
          "restart. Set it in server/.env for stable 8-hour logins.")
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
)


# ---------------------------------------------------------------------------
# Operator login (QR badge + PIN, see ZSTM_LOGIN_QR) + session guard
# ---------------------------------------------------------------------------

def require_login(fn):
    """Guard an endpoint: 401 unless a valid operator session is present."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return jsonify({"error": "Not signed in.", "auth": False}), 401
        return fn(*args, **kwargs)
    return wrapper


def _is_admin():
    return session.get("user", "").upper() in ADMIN_USERS


def require_admin(fn):
    """Guard: 401 if not signed in, 403 if the user is not in ADMIN_USERS."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return jsonify({"error": "Not signed in.", "auth": False}), 401
        if not _is_admin():
            return jsonify({"error": "Not authorized."}), 403
        return fn(*args, **kwargs)
    return wrapper


@app.post("/api/auth/logout")
def api_auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/auth/me")
def api_auth_me():
    if session.get("user"):
        return jsonify({"user": session["user"], "fullName": session.get("fullName", ""),
                        "isAdmin": _is_admin()})
    return jsonify({"auth": False}), 401


# ---------------------------------------------------------------------------
# QR-badge login + badge administration (SAPHANADB.ZSTM_LOGIN_QR)
# ---------------------------------------------------------------------------

PIN_RE = re.compile(r"^\d{4,8}$")


def _valid_pin(pin):
    """A PIN is 4 to 8 digits."""
    return bool(PIN_RE.match(pin or ""))


def _qr_lookup(token):
    """Return {qrId, sapUser, fullName, active, validTo, pinHash} for a token, or None."""
    if SAP_RFC_MOCK:
        for b in _MOCK_BADGES:
            if b["token"] == token:
                return {"qrId": b["qrId"], "sapUser": b["sapUser"], "fullName": b["fullName"],
                        "active": b["active"], "validTo": None, "pinHash": b.get("pinHash", "")}
        return None
    conn = _hana_conn()
    if conn is None:
        raise RuntimeError("HANA is not configured for badge login.")
    try:
        cur = conn.cursor()
        cur.execute("SELECT \"QR_ID\",\"SAP_USER\",\"FULL_NAME\",\"ACTIVE\",\"VALID_TO\",\"PIN_HASH\" FROM "
                    + LOGIN_QR_TABLE + " WHERE \"MANDT\"=? AND \"QR_TOKEN\"=?", [MANDT, token])
        r = cur.fetchone()
        cur.close()
        if not r:
            return None
        return {"qrId": (r[0] or "").strip(), "sapUser": (r[1] or "").strip(),
                "fullName": (r[2] or "").strip(), "active": (r[3] or "").strip(),
                "validTo": r[4], "pinHash": (r[5] or "").strip()}
    finally:
        conn.close()


def _active_badges():
    """All ACTIVE badges as {qrId, sapUser, fullName, validTo, pinHash}. Used by
    the PIN-only login and PIN-uniqueness check (PINs are salted hashes, so they
    can't be looked up by value -- we scan the (small) active set)."""
    if SAP_RFC_MOCK:
        return [{"qrId": b["qrId"], "sapUser": b["sapUser"], "fullName": b["fullName"],
                 "validTo": None, "pinHash": b.get("pinHash", "")}
                for b in _MOCK_BADGES if b["active"] == "X"]
    conn = _hana_conn()
    if conn is None:
        raise RuntimeError("HANA is not configured for badge login.")
    try:
        cur = conn.cursor()
        cur.execute("SELECT \"QR_ID\",\"SAP_USER\",\"FULL_NAME\",\"VALID_TO\",\"PIN_HASH\" FROM "
                    + LOGIN_QR_TABLE + " WHERE \"MANDT\"=? AND \"ACTIVE\"='X'", [MANDT])
        rows = cur.fetchall()
        cur.close()
        return [{"qrId": (r[0] or "").strip(), "sapUser": (r[1] or "").strip(),
                 "fullName": (r[2] or "").strip(), "validTo": r[3],
                 "pinHash": (r[4] or "").strip()} for r in rows]
    finally:
        conn.close()


def _pin_lookup(pin):
    """The single ACTIVE badge whose PIN matches, or None (PINs are unique)."""
    for b in _active_badges():
        if b["pinHash"] and check_password_hash(b["pinHash"], pin):
            return b
    return None


def _pin_in_use(pin, exclude_qr_id=None):
    """True if some OTHER active badge already uses this PIN (keeps PINs unique
    so a PIN alone identifies one operator)."""
    b = _pin_lookup(pin)
    return bool(b and b["qrId"] != (exclude_qr_id or ""))


def _qr_touch(qr_id):
    """Best-effort LAST_LOGIN_AT update; never blocks a login."""
    if SAP_RFC_MOCK:
        return
    try:
        conn = _hana_conn()
        if conn is None:
            return
        try:
            cur = conn.cursor()
            cur.execute("UPDATE " + LOGIN_QR_TABLE + " SET \"LAST_LOGIN_AT\"=CURRENT_TIMESTAMP "
                        "WHERE \"MANDT\"=? AND \"QR_ID\"=?", [MANDT, qr_id])
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception:
        pass


def _client_info():
    """(ip, user_agent) of the caller, truncated to the log column widths."""
    xff = request.headers.get("X-Forwarded-For", "")
    ip = (xff.split(",")[0].strip() if xff else "") or (request.remote_addr or "")
    return ip[:45], request.headers.get("User-Agent", "")[:255]


def _log_login(sap_user, qr_id, method):
    """Append one login-audit row to ZSTM_LOGIN_LOG. Best-effort: any failure
    (HANA down, table/grant missing) is swallowed so it never blocks a login."""
    ip, ua = _client_info()
    if SAP_RFC_MOCK:
        _MOCK_LOGINS.append({"sapUser": sap_user, "qrId": qr_id, "method": method,
                             "loginAt": datetime.now().isoformat(timespec="seconds"),
                             "clientIp": ip, "userAgent": ua})
        return
    try:
        conn = _hana_conn()
        if conn is None:
            return
        try:
            for attempt in range(2):
                cur = conn.cursor()
                try:
                    cur.execute("SELECT COALESCE(MAX(TO_BIGINT(\"LOG_ID\")),0)+1 FROM " + LOGIN_LOG_TABLE +
                                " WHERE \"MANDT\"=? AND \"LOG_ID\" LIKE_REGEXPR '^[0-9]+$'", [MANDT])
                    row = cur.fetchone()
                    log_id = str(int(row[0]) if row and row[0] is not None else 1).zfill(10)
                    cur.execute("INSERT INTO " + LOGIN_LOG_TABLE + " (\"MANDT\",\"LOG_ID\",\"SAP_USER\","
                                "\"QR_ID\",\"METHOD\",\"LOGIN_AT\",\"CLIENT_IP\",\"USER_AGENT\") "
                                "VALUES (?,?,?,?,?,CURRENT_TIMESTAMP,?,?)",
                                [MANDT, log_id, sap_user, qr_id, method, ip, ua])
                    conn.commit()
                    cur.close()
                    return
                except Exception as exc:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    cur.close()
                    if attempt == 0 and getattr(exc, "errorcode", None) == 301:
                        continue      # LOG_ID clash under concurrency: recompute once
                    raise
        finally:
            conn.close()
    except Exception:
        pass


def _sign_in(row, method):
    """Open the operator session for a resolved badge row and audit it."""
    _qr_touch(row["qrId"])
    session.permanent = True
    session["user"] = row["sapUser"].upper()
    session["fullName"] = row.get("fullName", "")
    session["method"] = method.lower()
    session["ts"] = datetime.now().isoformat(timespec="seconds")
    _log_login(session["user"], row["qrId"], method)   # audit trail (best-effort)
    return jsonify({"user": session["user"], "fullName": session["fullName"], "isAdmin": _is_admin()})


@app.post("/api/auth/login-qr")
def api_auth_login_qr():
    """Sign in by scanning a badge QR. The scan alone signs the operator in."""
    data = request.get_json(silent=True) or {}
    token = str(data.get("qr", "")).strip()
    if not token:
        return jsonify({"error": "Nothing scanned."}), 400
    try:
        row = _qr_lookup(token)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception as exc:
        return jsonify({"error": "Badge lookup failed: " + str(exc)}), 502
    if not row:
        return jsonify({"error": "Badge not recognized."}), 401
    if row.get("active") != "X":
        return jsonify({"error": "This badge is disabled."}), 401
    vt = row.get("validTo")
    if vt and vt < date.today():
        return jsonify({"error": "This badge has expired."}), 401
    return _sign_in(row, "QR")


@app.post("/api/auth/login-pin")
def api_auth_login_pin():
    """Sign in with a PIN only (fallback when the badge can't be scanned). The
    PIN is unique, so it identifies exactly one active operator."""
    data = request.get_json(silent=True) or {}
    pin = str(data.get("pin", "")).strip()
    if not _valid_pin(pin):
        return jsonify({"error": "Enter your PIN."}), 400
    try:
        row = _pin_lookup(pin)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception as exc:
        return jsonify({"error": "PIN lookup failed: " + str(exc)}), 502
    if not row:
        return jsonify({"error": "Wrong PIN."}), 401
    vt = row.get("validTo")
    if vt and vt < date.today():
        return jsonify({"error": "This badge has expired."}), 401
    return _sign_in(row, "PIN")


def _qr_list():
    if SAP_RFC_MOCK:
        return [dict(b, lastLogin="") for b in _MOCK_BADGES]
    conn = _hana_conn()
    if conn is None:
        raise RuntimeError("HANA is not configured.")
    try:
        cur = conn.cursor()
        cur.execute("SELECT \"QR_ID\",\"SAP_USER\",\"FULL_NAME\",\"QR_TOKEN\",\"ACTIVE\","
                    "TO_VARCHAR(\"LAST_LOGIN_AT\",'YYYY-MM-DD HH24:MI') FROM " + LOGIN_QR_TABLE +
                    " WHERE \"MANDT\"=? ORDER BY \"SAP_USER\"", [MANDT])
        rows = cur.fetchall()
        cur.close()
        return [{"qrId": (r[0] or "").strip(), "sapUser": (r[1] or "").strip(),
                 "fullName": (r[2] or "").strip(), "token": (r[3] or "").strip(),
                 "active": (r[4] or "").strip(), "lastLogin": r[5] or ""} for r in rows]
    finally:
        conn.close()


def _qr_create(sap_user, full_name, token, pin_hash, admin):
    if SAP_RFC_MOCK:
        qr_id = str(len(_MOCK_BADGES) + 1).zfill(10)
        b = {"qrId": qr_id, "sapUser": sap_user, "fullName": full_name,
             "token": token, "pinHash": pin_hash, "active": "X", "lastLogin": ""}
        _MOCK_BADGES.append(b)
        return {k: v for k, v in b.items() if k != "pinHash"}
    conn = _hana_conn()
    if conn is None:
        raise RuntimeError("HANA is not configured.")
    try:
        for attempt in range(2):
            cur = conn.cursor()
            try:
                cur.execute("SELECT COALESCE(MAX(TO_BIGINT(\"QR_ID\")),0)+1 FROM " + LOGIN_QR_TABLE +
                            " WHERE \"MANDT\"=? AND \"QR_ID\" LIKE_REGEXPR '^[0-9]+$'", [MANDT])
                row = cur.fetchone()
                qr_id = str(int(row[0]) if row and row[0] is not None else 1).zfill(10)
                cur.execute("INSERT INTO " + LOGIN_QR_TABLE + " (\"MANDT\",\"QR_ID\",\"SAP_USER\","
                            "\"FULL_NAME\",\"QR_TOKEN\",\"PIN_HASH\",\"ACTIVE\",\"ERNAM\",\"CREATED_AT\") "
                            "VALUES (?,?,?,?,?,?,'X',?,CURRENT_TIMESTAMP)",
                            [MANDT, qr_id, sap_user, full_name, token, pin_hash, admin])
                conn.commit()
                cur.close()
                return {"qrId": qr_id, "sapUser": sap_user, "fullName": full_name,
                        "token": token, "active": "X", "lastLogin": ""}
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                cur.close()
                if attempt == 0 and getattr(exc, "errorcode", None) == 301:
                    continue
                raise
    finally:
        conn.close()


def _qr_set_active(qr_id, active):
    if SAP_RFC_MOCK:
        for b in _MOCK_BADGES:
            if b["qrId"] == qr_id:
                b["active"] = active
        return
    conn = _hana_conn()
    if conn is None:
        raise RuntimeError("HANA is not configured.")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE " + LOGIN_QR_TABLE + " SET \"ACTIVE\"=? WHERE \"MANDT\"=? AND \"QR_ID\"=?",
                    [active, MANDT, qr_id])
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _qr_set_pin(qr_id, pin_hash):
    """Set/reset a badge's PIN (stores a salted hash). Returns True if a row changed."""
    if SAP_RFC_MOCK:
        hit = False
        for b in _MOCK_BADGES:
            if b["qrId"] == qr_id:
                b["pinHash"] = pin_hash
                hit = True
        return hit
    conn = _hana_conn()
    if conn is None:
        raise RuntimeError("HANA is not configured.")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE " + LOGIN_QR_TABLE + " SET \"PIN_HASH\"=? WHERE \"MANDT\"=? AND \"QR_ID\"=?",
                    [pin_hash, MANDT, qr_id])
        n = cur.rowcount
        conn.commit()
        cur.close()
        return n > 0
    finally:
        conn.close()


@app.get("/api/admin/qr")
@require_admin
def api_admin_qr_list():
    try:
        return jsonify({"badges": _qr_list()})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.post("/api/admin/qr")
@require_admin
def api_admin_qr_create():
    data = request.get_json(silent=True) or {}
    sap_user = str(data.get("sapUser", "")).strip().upper()
    full_name = str(data.get("fullName", "")).strip()
    pin = str(data.get("pin", "")).strip()
    if not sap_user:
        return jsonify({"error": "Enter the SAP user."}), 400
    if not _valid_pin(pin):
        return jsonify({"error": "PIN must be 4 to 8 digits."}), 400
    token = secrets.token_hex(10)          # 20-char opaque badge token
    try:
        if _pin_in_use(pin):
            return jsonify({"error": "That PIN is already in use. Choose another."}), 409
        b = _qr_create(sap_user, full_name, token, generate_password_hash(pin),
                       session.get("user", ""))
    except Exception as exc:
        return jsonify({"error": "Could not create badge: " + str(exc)}), 502
    return jsonify(b), 201


@app.post("/api/admin/qr/pin")
@require_admin
def api_admin_qr_pin():
    """Reset a badge's PIN (admin only). Body: {qrId, pin}."""
    data = request.get_json(silent=True) or {}
    qr_id = str(data.get("qrId", "")).strip()
    pin = str(data.get("pin", "")).strip()
    if not qr_id:
        return jsonify({"error": "Missing badge id."}), 400
    if not _valid_pin(pin):
        return jsonify({"error": "PIN must be 4 to 8 digits."}), 400
    try:
        if _pin_in_use(pin, exclude_qr_id=qr_id):
            return jsonify({"error": "That PIN is already in use. Choose another."}), 409
        _qr_set_pin(qr_id, generate_password_hash(pin))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"ok": True, "qrId": qr_id})


@app.post("/api/admin/qr/toggle")
@require_admin
def api_admin_qr_toggle():
    data = request.get_json(silent=True) or {}
    qr_id = str(data.get("qrId", "")).strip()
    active = "X" if data.get("active") else ""
    if not qr_id:
        return jsonify({"error": "Missing badge id."}), 400
    try:
        _qr_set_active(qr_id, active)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"ok": True, "qrId": qr_id, "active": active})


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
@require_login
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
                      "batch": batch, "uom": uom, "qty": qty,
                      # doff/scan context (carried into ZSTM_TRANSIT_D)
                      "doffBatchNo": str(r.get("doffBatchNo", "")).strip(),
                      "article": str(r.get("article", "")).strip(),
                      "qrRaw": str(r.get("qrRaw", "")).strip(),
                      "lot": str(r.get("lot", "")).strip(),
                      "loom": str(r.get("loom", "")).strip(),
                      "beam": str(r.get("beam", "")).strip(),
                      "seq": str(r.get("seq", "")).strip()})
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


# ---------------------------------------------------------------------------
# "Doff in Transit" save -- writes ZSTM_TRANSIT_D instead of posting a 311
# ---------------------------------------------------------------------------

def _next_docid_base(cur):
    """MAX(numeric DOCID)+1 for the transit table (guarded against junk ids)."""
    cur.execute(
        "SELECT COALESCE(MAX(TO_BIGINT(\"DOCID\")), 0) + 1 FROM " + TRANSIT_TABLE +
        " WHERE \"MANDT\" = ? AND \"DOCID\" LIKE_REGEXPR '^[0-9]+$'", [MANDT])
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 1


def insert_transit(items, to_sloc, operator):
    """Insert each scanned doff as one 'Doff in Transit' row in ZSTM_TRANSIT_D.

    One DOCID per doff (MAX+1, zero-padded to 10), all committed together and
    retried once on a unique-key clash. NO SAP goods movement is posted.
    Returns {"docids": [...], "mock": bool}.
    """
    if SAP_RFC_MOCK:                       # dev: fake ids, no HANA needed
        base = int(datetime.now().strftime("%H%M%S"))
        return {"docids": [str(base + i).zfill(10) for i in range(len(items))], "mock": True}

    conn = _hana_conn()
    if conn is None:
        raise RuntimeError("HANA is not configured. Set HANA_HOST / HANA_PORT / "
                           "HANA_USER / HANA_PASSWORD in server/.env.")
    sql = ("INSERT INTO " + TRANSIT_TABLE + " (\"MANDT\",\"DOCID\",\"STATUS\","
           "\"WERKS\",\"LGORT\",\"UMLGO\",\"MATNR\",\"CHARG\",\"MENGE\",\"MEINS\","
           "\"DOFF_BATCHNO\",\"ARTICLE\",\"QR_RAW\",\"LOT\",\"LOOM\",\"BEAM\",\"SEQ\","
           "\"OPERATOR\",\"ERNAM\",\"CREATED_AT\") "
           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)")
    try:
        for attempt in range(2):
            cur = conn.cursor()
            try:
                base = _next_docid_base(cur)
                docids = []
                for i, it in enumerate(items):
                    docid = str(base + i).zfill(10)
                    docids.append(docid)
                    cur.execute(sql, [
                        MANDT, docid, "Doff in Transit",
                        it["plant"], it["sloc"], to_sloc,
                        it["material"], it.get("batch", ""), it["qty"], it["uom"],
                        it.get("doffBatchNo", ""), it.get("article", ""), it.get("qrRaw", ""),
                        it.get("lot", ""), it.get("loom", ""), it.get("beam", ""), it.get("seq", ""),
                        operator, HANA["user"],
                    ])
                conn.commit()
                return {"docids": docids, "mock": False}
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                if attempt == 0 and getattr(exc, "errorcode", None) == 301:  # DOCID clash
                    continue
                raise
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


@app.post("/api/stock/transit")
@require_login
def api_stock_transit():
    """Save the scanned doff list as 'Doff in Transit' rows (no 311)."""
    data = request.get_json(silent=True) or {}
    to_sloc = str(data.get("toSloc", "")).strip().upper()
    if not to_sloc:
        return jsonify({"error": "Enter the destination storage location first."}), 400
    items, err = _validate_items(data.get("items"), to_sloc)
    if err:
        return jsonify({"error": err}), 400

    operator = session.get("user", "")
    try:
        res = insert_transit(items, to_sloc, operator)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception as exc:
        return jsonify({"error": "Save failed: " + str(exc)}), 502

    saved = [{"docid": d, "plant": it["plant"], "sloc": it["sloc"],
              "material": it["material"], "batch": it["batch"],
              "qty": _qty_str(it["qty"]), "uom": it["uom"],
              "doffBatchNo": it.get("doffBatchNo", ""), "status": "Doff in Transit"}
             for d, it in zip(res["docids"], items)]
    return jsonify({"docids": res["docids"], "count": len(saved), "toSloc": to_sloc,
                    "operator": operator, "mock": res.get("mock", False),
                    "saved": saved}), 201


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


def _bootstrap_badge(argv):
    """One-off CLI to create the FIRST login badge (since there is no SAP login,
    the first admin can't use the in-app screen). Usage:

        python server/app.py --create-badge SAP_USER PIN ["Full Name"]

    Prints the generated QR token so a badge card can be made. Add the user to
    ADMIN_USERS in .env if they should manage badges. Runs against HANA (or the
    in-memory mock store, which is pointless outside a live process)."""
    sap_user = (argv[0] if len(argv) > 0 else "").strip().upper()
    pin = (argv[1] if len(argv) > 1 else "").strip()
    full = (argv[2] if len(argv) > 2 else "").strip()
    if not sap_user or not _valid_pin(pin):
        print("Usage: python server/app.py --create-badge SAP_USER PIN [\"Full Name\"]")
        print("       PIN must be 4 to 8 digits.")
        return 2
    token = secrets.token_hex(10)
    try:
        _qr_create(sap_user, full, token, generate_password_hash(pin), sap_user)
    except Exception as exc:
        print("Could not create badge:", exc)
        return 1
    print("Badge created for %s. QR token (encode this on the card):\n\n    %s\n"
          % (sap_user, token))
    print("Add %s to ADMIN_USERS in server/.env to let them manage badges." % sap_user)
    return 0


def _seed_mock_badges():
    """DEV ONLY (SAP_RFC_MOCK): auto-create a badge for each ADMIN_USERS entry so
    the app is usable without HANA. Token = the username lower-cased, PIN =
    MOCK_BADGE_PIN (default 1234). Never runs against a real system."""
    base = os.environ.get("MOCK_BADGE_PIN", "1234")
    for i, u in enumerate(sorted(ADMIN_USERS)):
        token = u.lower()
        pin = base if i == 0 else (base + str(i))   # keep PINs unique across seeds
        _qr_create(u, u.title(), token, generate_password_hash(pin), "mock-seed")
        print("[mock] seeded badge  user=%s  token=%s  pin=%s" % (u, token, pin))
    if not ADMIN_USERS:
        print("[mock] ADMIN_USERS is empty - no badge seeded; set it to sign in.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--create-badge":
        sys.exit(_bootstrap_badge(sys.argv[2:]))
    # Werkzeug's reloader runs this module twice; seed only in the child that
    # actually serves (WERKZEUG_RUN_MAIN set), so the token prints once.
    if SAP_RFC_MOCK and os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        _seed_mock_badges()
    app.run(host="0.0.0.0", port=PORT, debug=True, ssl_context=_ssl_context())
