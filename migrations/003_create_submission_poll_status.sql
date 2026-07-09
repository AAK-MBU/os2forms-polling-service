-- Per-(job, submission) runtime state. Owns idempotency, retries, and retention.
-- Dedup key is (job_id, submission_uuid): the same submission delivered to two jobs
-- (destinations) yields two rows. FK cascades so deleting a job clears its state.
-- Idempotent DDL; depends on polling.PollJob (migration 001).

IF OBJECT_ID('polling.SubmissionPollStatus', 'U') IS NULL
BEGIN
    CREATE TABLE polling.SubmissionPollStatus (
        id                    BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        job_id                INT            NOT NULL,
        os2formWebformId      NVARCHAR(255)  NOT NULL,   -- denormalized for reporting
        submission_uuid       NVARCHAR(255)  NOT NULL,
        status                NVARCHAR(20)   NOT NULL,   -- new | delivered | failed | dead_letter
        attempts              INT            NOT NULL CONSTRAINT DF_SPS_attempts DEFAULT 0,
        last_error            NVARCHAR(2000) NULL,
        destination_reference NVARCHAR(255)  NULL,
        first_seen_at         DATETIME2      NOT NULL CONSTRAINT DF_SPS_first_seen DEFAULT SYSUTCDATETIME(),
        delivered_at          DATETIME2      NULL,
        erase_at              DATETIME2      NULL,
        CONSTRAINT UQ_SubmissionPollStatus_job_uuid UNIQUE (job_id, submission_uuid),
        CONSTRAINT FK_SubmissionPollStatus_job FOREIGN KEY (job_id)
            REFERENCES polling.PollJob (id) ON DELETE CASCADE
    );
    CREATE INDEX IX_SubmissionPollStatus_erase_at ON polling.SubmissionPollStatus (erase_at);
    CREATE INDEX IX_SubmissionPollStatus_status ON polling.SubmissionPollStatus (status);
END;
