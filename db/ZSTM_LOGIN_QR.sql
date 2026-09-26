-- =====================================================================
-- ZSTM_LOGIN_QR  --  QR-badge login (a scanned badge signs an operator in)
-- =====================================================================
-- Second sign-in method alongside SAP user/password. Each row is a badge:
-- scanning its QR_TOKEN signs the operator in as SAP_USER (same 8h session).
-- The row IS the access grant; revoke by setting ACTIVE = '' (no delete).
--
-- Design (per chosen options): the token is stored PLAINTEXT, scan-only
-- (no PIN), and no RFC re-check -- this table alone decides access. Anyone
-- who can read this table or copy a card can sign in as that operator, so
-- keep read access tight and revoke lost cards promptly.
--
-- Managed from the in-app "Login badges" admin screen (users listed in the
-- ADMIN_USERS env var). The app needs SELECT/INSERT/UPDATE.
-- ---------------------------------------------------------------------

CREATE COLUMN TABLE "SAPHANADB"."ZSTM_LOGIN_QR" (
    "MANDT"         NVARCHAR(3)  DEFAULT '900' NOT NULL,
    "QR_ID"         NVARCHAR(10)               NOT NULL,   -- surrogate id (MAX+1, padded 10)
    "SAP_USER"      NVARCHAR(12)               NOT NULL,   -- operator this badge signs in as
    "FULL_NAME"     NVARCHAR(80) DEFAULT ''    NOT NULL,
    "QR_TOKEN"      NVARCHAR(64)               NOT NULL,   -- exact value encoded on the QR (plaintext)
    "ACTIVE"        NVARCHAR(1)  DEFAULT 'X'   NOT NULL,   -- 'X' active, '' disabled (revoked)
    "VALID_TO"      DATE,                                  -- optional expiry (NULL = no expiry)
    "LAST_LOGIN_AT" TIMESTAMP,
    "ERNAM"         NVARCHAR(12) DEFAULT ''    NOT NULL,   -- admin who created it
    "CREATED_AT"    TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY ("MANDT","QR_ID")
);

-- one token maps to exactly one badge; the login lookup is by token
CREATE UNIQUE INDEX "ZSTM_LOGIN_QR~TOK" ON "SAPHANADB"."ZSTM_LOGIN_QR" ("MANDT","QR_TOKEN");

-- app: read to authenticate, update last-login/revoke, insert new badges
-- GRANT SELECT, INSERT, UPDATE ON "SAPHANADB"."ZSTM_LOGIN_QR" TO ZMSQL;
