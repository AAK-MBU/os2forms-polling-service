-- Per-job payload customization. Holds JSON: {"mode": "raw"|"parsed"|"map", ...}.
-- NULL => "raw" (deliver the source response verbatim) — the pre-existing behavior, so
-- every already-registered job is unaffected. Idempotent DDL.

IF COL_LENGTH('polling.PollJob', 'payload_mapping') IS NULL
    ALTER TABLE polling.PollJob ADD payload_mapping NVARCHAR(MAX) NULL;
