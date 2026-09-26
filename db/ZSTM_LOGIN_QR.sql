-- =====================================================================
-- ZSTM_LOGIN_QR  --  QR-badge + PIN login (the ONLY way into the app)
-- =====================================================================
-- There is no SAP user/password login. Each row is a badge: the operator
-- scans its QR_TOKEN and types their PIN to sign in as SAP_USER (8h session).
-- The row IS the access grant; revoke by setting ACTIVE = '' (no delete).
--
-- Two factors: the QR_TOKEN is "something you have" (stored PLAINTEXT -- it is
-- the value printed on the card), and the PIN is "something you know" (stored
-- only as a salted HASH in PIN_HASH, never in clear). A copied card alone is
-- not enough to log in; still, keep read access tight and revoke lost cards.
-- The admin sets each badge's PIN when creating it and can reset it later.
--
-- Managed from the in-app "Login badges" admin screen (users listed in the
-- ADMIN_USERS env var); seed the FIRST admin badge from the command line:
--   python server/app.py --create-badge SAP_USER PIN "Full Name"
-- The app needs SELECT/INSERT/UPDATE.
-- ---------------------------------------------------------------------

CREATE COLUMN TABLE "SAPHANADB"."ZSTM_LOGIN_QR" (
    "MANDT"         NVARCHAR(3)   DEFAULT '900' NOT NULL,
    "QR_ID"         NVARCHAR(10)                NOT NULL,   -- surrogate id (MAX+1, padded 10)
    "SAP_USER"      NVARCHAR(12)                NOT NULL,   -- operator this badge signs in as
    "FULL_NAME"     NVARCHAR(80)  DEFAULT ''    NOT NULL,
    "QR_TOKEN"      NVARCHAR(64)                NOT NULL,   -- exact value encoded on the QR (plaintext)
    "PIN_HASH"      NVARCHAR(255) DEFAULT ''    NOT NULL,   -- salted hash of the PIN ('' = no PIN, cannot log in)
    "ACTIVE"        NVARCHAR(1)   DEFAULT 'X'   NOT NULL,   -- 'X' active, '' disabled (revoked)
    "VALID_TO"      DATE,                                   -- optional expiry (NULL = no expiry)
    "LAST_LOGIN_AT" TIMESTAMP,
    "ERNAM"         NVARCHAR(12)  DEFAULT ''    NOT NULL,   -- admin who created it
    "CREATED_AT"    TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY ("MANDT","QR_ID")
);

-- one token maps to exactly one badge; the login lookup is by token
CREATE UNIQUE INDEX "ZSTM_LOGIN_QR~TOK" ON "SAPHANADB"."ZSTM_LOGIN_QR" ("MANDT","QR_TOKEN");

-- app: read to authenticate, update last-login/revoke/PIN, insert new badges
-- GRANT SELECT, INSERT, UPDATE ON "SAPHANADB"."ZSTM_LOGIN_QR" TO ZMSQL;

-- --- Migration for an EXISTING install (table already created without PIN) ---
-- Run this once instead of the CREATE above; every existing badge then has an
-- empty PIN and must have one set (admin screen or --create-badge) before it
-- can log in again:
--   ALTER TABLE "SAPHANADB"."ZSTM_LOGIN_QR" ADD ("PIN_HASH" NVARCHAR(255) DEFAULT '' NOT NULL);
