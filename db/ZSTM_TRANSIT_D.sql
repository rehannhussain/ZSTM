-- =====================================================================
-- ZSTM_TRANSIT_D  --  "Doff in Transit" custom log (NO standard 311 move)
-- =====================================================================
-- Each scanned doff is recorded here instead of posting a goods movement.
-- STATUS drives the lifecycle:  'Doff in Transit'  ->  'Reversed'.
-- Field names follow SAP MM conventions so this can later become a DDIC
-- Z-table with the same columns (WERKS/LGORT/UMLGO/MATNR/CHARG/MENGE/MEINS).
--
-- Run once as a user allowed to CREATE in SAPHANADB (a DBA). The app's
-- ZMSQL user then needs:  GRANT SELECT, INSERT, UPDATE ON
-- SAPHANADB.ZSTM_TRANSIT_D TO ZMSQL;
-- ---------------------------------------------------------------------

-- 1) TABLE ------------------------------------------------------------
CREATE COLUMN TABLE "SAPHANADB"."ZSTM_TRANSIT_D" (
    "MANDT"            NVARCHAR(3)   DEFAULT '900'              NOT NULL,  -- client
    "DOCID"            NVARCHAR(10)                             NOT NULL,  -- MAX+1, zero-padded 10
    "STATUS"           NVARCHAR(20)  DEFAULT 'Doff in Transit'  NOT NULL,  -- 'Doff in Transit' | 'Reversed'

    -- move context (what a 311 would have carried)
    "WERKS"            NVARCHAR(4)   DEFAULT ''  NOT NULL,   -- plant
    "LGORT"            NVARCHAR(4)   DEFAULT ''  NOT NULL,   -- from storage location
    "UMLGO"            NVARCHAR(4)   DEFAULT ''  NOT NULL,   -- to (receiving) storage location
    "MATNR"            NVARCHAR(40)  DEFAULT ''  NOT NULL,   -- material (S/4 long, plain-not ALPHA)
    "CHARG"            NVARCHAR(10)  DEFAULT ''  NOT NULL,   -- batch
    "MENGE"            DECIMAL(13,3) DEFAULT 0   NOT NULL,   -- quantity / length moved
    "MEINS"            NVARCHAR(3)   DEFAULT ''  NOT NULL,   -- unit of measure

    -- doff identity / scan context (not present on a standard document)
    -- NOTE: keep >= the source length. SAPHANADB.ZWV_DOF_DD2.DOFF_BATCHNO is
    -- NVARCHAR(20); 30 leaves headroom. If an existing table was created
    -- smaller (e.g. 15) and Save fails with HANA 274 "value too large", widen it:
    --   ALTER TABLE SAPHANADB.ZSTM_TRANSIT_D ALTER (DOFF_BATCHNO NVARCHAR(24));
    "DOFF_BATCHNO"     NVARCHAR(30)  DEFAULT ''  NOT NULL,   -- e.g. 266314KT3L737A1901
    "ARTICLE"          NVARCHAR(40)  DEFAULT ''  NOT NULL,   -- e.g. FF FF-14561-S
    "QR_RAW"           NVARCHAR(50)  DEFAULT ''  NOT NULL,   -- scanned code e.g. 266314-737-A19-01
    "LOT"              NVARCHAR(10)  DEFAULT ''  NOT NULL,
    "LOOM"             NVARCHAR(10)  DEFAULT ''  NOT NULL,
    "BEAM"             NVARCHAR(20)  DEFAULT ''  NOT NULL,
    "SEQ"              NVARCHAR(4)   DEFAULT ''  NOT NULL,

    -- who / when created
    "OPERATOR"         NVARCHAR(12)  DEFAULT ''  NOT NULL,   -- shop-floor operator (entered)
    "ERNAM"            NVARCHAR(12)  DEFAULT ''  NOT NULL,   -- DB/RFC user that inserted
    "CREATED_AT"       TIMESTAMP     DEFAULT CURRENT_TIMESTAMP NOT NULL,

    -- reversal (filled only when reversed; password-gated in the app)
    "REVERSED_BY"      NVARCHAR(12)  DEFAULT ''  NOT NULL,
    "REVERSED_AT"      TIMESTAMP,
    "REVERSAL_REASON"  NVARCHAR(100) DEFAULT ''  NOT NULL,

    PRIMARY KEY ("MANDT", "DOCID")
);

-- helpful lookups: open transit list, and find a doff's current row
CREATE INDEX "ZSTM_TRANSIT_D~STAT" ON "SAPHANADB"."ZSTM_TRANSIT_D" ("MANDT","STATUS");
CREATE INDEX "ZSTM_TRANSIT_D~DOFF" ON "SAPHANADB"."ZSTM_TRANSIT_D" ("MANDT","DOFF_BATCHNO");


-- 2) NEXT DOCID (same guarded MAX+1 pattern as ZFN_FAB_PRD_D) ---------
--    The app runs this, zero-pads the result to 10, and inserts.
SELECT LPAD(
         COALESCE(MAX(TO_BIGINT("DOCID")), 0) + 1, 10, '0') AS "NEXT_DOCID"
  FROM "SAPHANADB"."ZSTM_TRANSIT_D"
 WHERE "MANDT" = '900'
   AND "DOCID" LIKE_REGEXPR '^[0-9]+$';   -- HANA regex: only all-numeric ids


-- 3) INSERT one doff as "Doff in Transit" (example values) ------------
INSERT INTO "SAPHANADB"."ZSTM_TRANSIT_D"
    ("MANDT","DOCID","STATUS",
     "WERKS","LGORT","UMLGO","MATNR","CHARG","MENGE","MEINS",
     "DOFF_BATCHNO","ARTICLE","QR_RAW","LOT","LOOM","BEAM","SEQ",
     "OPERATOR","ERNAM","CREATED_AT")
VALUES
    ('900','0000000001','Doff in Transit',
     '3000','3019','3055','3600007398','2663140301',1973,'M',
     '266314KT3L737A1901','FF FF-14561-S','266314-737-A19-01','266314','737','A19','01',
     'KT_REHAN','ZMSQL', CURRENT_TIMESTAMP);


-- 4) REVERSE (soft) -- only rows still in transit; password enforced by
--    the API before this runs. :who / :reason are bound by the app.
UPDATE "SAPHANADB"."ZSTM_TRANSIT_D"
   SET "STATUS"          = 'Reversed',
       "REVERSED_BY"     = ?,          -- operator/supervisor who reversed
       "REVERSED_AT"     = CURRENT_TIMESTAMP,
       "REVERSAL_REASON" = ?           -- optional reason
 WHERE "MANDT"  = '900'
   AND "DOCID"  = ?                    -- the row to reverse
   AND "STATUS" = 'Doff in Transit';   -- idempotent: never reverses twice

-- (Alternative HARD reverse, if you'd rather delete than keep the audit row:)
-- DELETE FROM "SAPHANADB"."ZSTM_TRANSIT_D"
--  WHERE "MANDT"='900' AND "DOCID"=? AND "STATUS"='Doff in Transit';
