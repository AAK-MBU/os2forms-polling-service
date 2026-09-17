-- OS2forms assigns every submission a serial that is consecutive per webform. Storing it
-- makes backward reconciliation possible: a hole in the sequence of serials we have rows for
-- means a submission was never listed to us at all — the one kind of loss no other signal in
-- this service can reveal. (A submission that failed delivery already has a row and is visible
-- via /polling/jobs/{id}/status; it is not a hole.)
--
-- NULLable by necessity: rows written before this column existed have no serial, as do
-- submissions whose source gave a non-numeric serial. The poller backfills historical rows
-- opportunistically as it re-lists them.
--
-- Idempotent DDL. Both statements go through EXEC because migrations run as a single T-SQL
-- batch (no GO): the CREATE INDEX would otherwise fail to compile against a column that the
-- ALTER TABLE in the same batch has not yet added.

IF COL_LENGTH('polling.SubmissionPollStatus', 'submission_serial') IS NULL
    EXEC('ALTER TABLE polling.SubmissionPollStatus ADD submission_serial BIGINT NULL');

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_SubmissionPollStatus_job_serial'
      AND object_id = OBJECT_ID('polling.SubmissionPollStatus')
)
    EXEC('CREATE INDEX IX_SubmissionPollStatus_job_serial
          ON polling.SubmissionPollStatus (job_id, submission_serial)');
