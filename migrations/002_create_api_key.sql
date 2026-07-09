-- Registered API keys (stored hashed). A caller exchanges a raw key for a short-lived JWT
-- at POST /auth/token. The raw key is never stored. Idempotent DDL.

IF OBJECT_ID('polling.ApiKey', 'U') IS NULL
BEGIN
    CREATE TABLE polling.ApiKey (
        id         INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        owner      NVARCHAR(255)  NOT NULL,
        key_hash   CHAR(64)       NOT NULL,          -- SHA-256 hex of the raw key
        scopes     NVARCHAR(255)  NOT NULL CONSTRAINT DF_ApiKey_scopes DEFAULT '',
        isActive   BIT            NOT NULL CONSTRAINT DF_ApiKey_active DEFAULT 1,
        created_at DATETIME2      NOT NULL CONSTRAINT DF_ApiKey_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_ApiKey_key_hash UNIQUE (key_hash)
    );
END;
