-- =====================================================================
-- ZSTM_LOGIN_LOG  --  login audit trail (one row per successful sign-in)
-- =====================================================================
-- Written whenever an operator signs in with a valid QR badge (a token that
-- exists and is active in ZSTM_LOGIN_QR). Append-only history: the app only
-- INSERTs, never updates or deletes, so it is a permanent record of who signed
-- in, with which badge, when, and from where.
--
-- METHOD leaves room to also log the SAP user/password path later ('SAP');
-- today the app writes 'QR'. The write is best-effort in the app -- a failure
-- here never blocks the login.
--
-- The app needs SELECT (to show history) and INSERT.
-- ---------------------------------------------------------------------

CREATE COLUMN TABLE "SAPHANADB"."ZSTM_LOGIN_LOG" (
    "MANDT"      NVARCHAR(3)   DEFAULT '900' NOT NULL,
    "LOG_ID"     NVARCHAR(10)                NOT NULL,   -- surrogate id (MAX+1, padded 10)
    "SAP_USER"   NVARCHAR(12)                NOT NULL,   -- operator signed in as
    "QR_ID"      NVARCHAR(10)  DEFAULT ''    NOT NULL,   -- badge used (ZSTM_LOGIN_QR.QR_ID)
    "METHOD"     NVARCHAR(3)   DEFAULT 'QR'  NOT NULL,   -- 'QR' badge, 'SAP' user/password
    "LOGIN_AT"   TIMESTAMP     DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "CLIENT_IP"  NVARCHAR(45)  DEFAULT ''    NOT NULL,   -- IPv4/IPv6 of the device
    "USER_AGENT" NVARCHAR(255) DEFAULT ''    NOT NULL,   -- browser/device user-agent
    PRIMARY KEY ("MANDT","LOG_ID")
);

-- query a user's or a badge's sign-in history quickly
CREATE INDEX "ZSTM_LOGIN_LOG~USR" ON "SAPHANADB"."ZSTM_LOGIN_LOG" ("MANDT","SAP_USER","LOGIN_AT");
CREATE INDEX "ZSTM_LOGIN_LOG~QR"  ON "SAPHANADB"."ZSTM_LOGIN_LOG" ("MANDT","QR_ID","LOGIN_AT");

-- app: insert an audit row per login, read to show history
-- GRANT SELECT, INSERT ON "SAPHANADB"."ZSTM_LOGIN_LOG" TO ZMSQL;
