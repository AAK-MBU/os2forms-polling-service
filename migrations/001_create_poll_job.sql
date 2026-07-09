-- The polling service's own job registry. One row = poll this form (source) and deliver
-- to this destination with this config. Owned solely by the poller. Idempotent DDL.

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'polling')
    EXEC('CREATE SCHEMA polling');

IF OBJECT_ID('polling.PollJob', 'U') IS NULL
BEGIN
    CREATE TABLE polling.PollJob (
        id                 INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        name               NVARCHAR(255)  NOT NULL,
        owner              NVARCHAR(255)  NOT NULL,   -- registering consumer (= JWT sub)
        source             NVARCHAR(255)  NOT NULL,   -- source-adapter key (e.g. os2forms)
        webformId          NVARCHAR(255)  NOT NULL,
        destination_system NVARCHAR(255)  NOT NULL,   -- 'adapter:target' routing
        destination_config NVARCHAR(MAX)  NOT NULL CONSTRAINT DF_PollJob_config DEFAULT '{}',
        isActive           BIT            NOT NULL CONSTRAINT DF_PollJob_active DEFAULT 1,
        erase_after        INT            NULL,
        created_at         DATETIME2      NOT NULL CONSTRAINT DF_PollJob_created DEFAULT SYSUTCDATETIME(),
        updated_at         DATETIME2      NOT NULL CONSTRAINT DF_PollJob_updated DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_PollJob_owner_form_dest UNIQUE (owner, webformId, destination_system)
    );
    CREATE INDEX IX_PollJob_active_source_form ON polling.PollJob (isActive, source, webformId);
    CREATE INDEX IX_PollJob_owner ON polling.PollJob (owner);
END;
