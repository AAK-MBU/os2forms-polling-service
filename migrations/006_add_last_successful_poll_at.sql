-- When the job last completed a full pass over its webform's listing: the source listed the
-- form, and every listed submission now has a state row for this job (delivered, dead-lettered
-- or recorded as failed).
--
-- This is the loss signal that works when OS2forms deletes submissions after a retention
-- period. The source re-lists every submission on every poll, so nothing still in OS2forms can
-- be missed; a submission is lost only if it is created *and* purged between two full passes.
-- A job whose timestamp is fresher than the retention period therefore cannot have lost one.
--
-- NULL until the first full pass, and it stays NULL (or goes stale) while the listing fails or
-- the job aborts every cycle on a bad destination — the "all counters at 0" case.
--
-- Idempotent DDL.

IF COL_LENGTH('polling.PollJob', 'last_successful_poll_at') IS NULL
    ALTER TABLE polling.PollJob ADD last_successful_poll_at DATETIME2 NULL;
