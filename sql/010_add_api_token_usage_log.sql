/*
    Persistent, least-privilege API-token usage telemetry.

    The runtime can insert only through invest.RecordApiTokenUsage. It cannot
    read or modify usage rows. Bearer tokens, authorization headers, MCP tool
    arguments, and response bodies are deliberately not stored.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

IF OBJECT_ID(N'invest.ApiTokenUsageLog', N'U') IS NULL
BEGIN
    CREATE TABLE invest.ApiTokenUsageLog
    (
        ApiTokenUsageLogId bigint IDENTITY(1, 1) NOT NULL
            CONSTRAINT PK_ApiTokenUsageLog PRIMARY KEY,
        ApiTokenId uniqueidentifier NOT NULL,
        UserId uniqueidentifier NOT NULL,
        RequestStartedAt datetimeoffset(7) NOT NULL,
        RequestCompletedAt datetimeoffset(7) NOT NULL,
        DurationMs int NOT NULL,
        HttpMethod varchar(10) NOT NULL,
        RequestPath nvarchar(512) NOT NULL,
        HostName nvarchar(255) NULL,
        RpcMethod nvarchar(100) NULL,
        ToolName nvarchar(200) NULL,
        HttpStatus smallint NOT NULL,
        Outcome varchar(30) NOT NULL,
        ErrorType nvarchar(100) NULL,
        WasRateLimited bit NOT NULL
            CONSTRAINT DF_ApiTokenUsageLog_WasRateLimited DEFAULT (0),
        RateLimitScope nvarchar(100) NULL,
        RequestBytes bigint NOT NULL
            CONSTRAINT DF_ApiTokenUsageLog_RequestBytes DEFAULT (0),
        ResponseBytes bigint NOT NULL
            CONSTRAINT DF_ApiTokenUsageLog_ResponseBytes DEFAULT (0),
        SchwabUnits int NOT NULL
            CONSTRAINT DF_ApiTokenUsageLog_SchwabUnits DEFAULT (0),
        SchwabUpstreamRequests int NOT NULL
            CONSTRAINT DF_ApiTokenUsageLog_SchwabRequests DEFAULT (0),
        SchwabCacheHits int NOT NULL
            CONSTRAINT DF_ApiTokenUsageLog_SchwabCacheHits DEFAULT (0),
        ClientIpAddress varchar(45) NULL,
        ClientNetwork varchar(50) NULL,
        ClientCountry varchar(8) NULL,
        ClientFingerprintHash binary(32) NULL,
        UserAgent nvarchar(512) NULL,
        CfRayId varchar(100) NULL,
        McpSessionIdHash binary(32) NULL,
        CreatedAt datetimeoffset(7) NOT NULL
            CONSTRAINT DF_ApiTokenUsageLog_CreatedAt DEFAULT SYSDATETIMEOFFSET(),

        CONSTRAINT FK_ApiTokenUsageLog_ApiToken
            FOREIGN KEY (ApiTokenId) REFERENCES invest.ApiTokens (ApiTokenId),
        CONSTRAINT FK_ApiTokenUsageLog_User
            FOREIGN KEY (UserId) REFERENCES invest.Users (UserId),
        CONSTRAINT CK_ApiTokenUsageLog_Duration
            CHECK (DurationMs >= 0),
        CONSTRAINT CK_ApiTokenUsageLog_HttpStatus
            CHECK (HttpStatus BETWEEN 100 AND 599),
        CONSTRAINT CK_ApiTokenUsageLog_Counters
            CHECK
            (
                RequestBytes >= 0
                AND ResponseBytes >= 0
                AND SchwabUnits >= 0
                AND SchwabUpstreamRequests >= 0
                AND SchwabCacheHits >= 0
            )
    );
END;
GO

IF NOT EXISTS
(
    SELECT 1
    FROM sys.indexes
    WHERE object_id = OBJECT_ID(N'invest.ApiTokenUsageLog')
      AND name = N'IX_ApiTokenUsageLog_User_Started'
)
BEGIN
    CREATE INDEX IX_ApiTokenUsageLog_User_Started
        ON invest.ApiTokenUsageLog (UserId, RequestStartedAt DESC)
        INCLUDE
        (
            ApiTokenId, ToolName, Outcome, HttpStatus, SchwabUnits,
            ClientCountry, ClientNetwork
        );
END;
GO

IF NOT EXISTS
(
    SELECT 1
    FROM sys.indexes
    WHERE object_id = OBJECT_ID(N'invest.ApiTokenUsageLog')
      AND name = N'IX_ApiTokenUsageLog_Token_Started'
)
BEGIN
    CREATE INDEX IX_ApiTokenUsageLog_Token_Started
        ON invest.ApiTokenUsageLog (ApiTokenId, RequestStartedAt DESC)
        INCLUDE (ToolName, Outcome, HttpStatus, DurationMs, SchwabUnits);
END;
GO

CREATE OR ALTER PROCEDURE invest.RecordApiTokenUsage
    @ApiTokenId uniqueidentifier,
    @UserId uniqueidentifier,
    @RequestStartedAt datetimeoffset(7),
    @RequestCompletedAt datetimeoffset(7),
    @DurationMs int,
    @HttpMethod varchar(10),
    @RequestPath nvarchar(512),
    @HostName nvarchar(255) = NULL,
    @RpcMethod nvarchar(100) = NULL,
    @ToolName nvarchar(200) = NULL,
    @HttpStatus smallint,
    @Outcome varchar(30),
    @ErrorType nvarchar(100) = NULL,
    @WasRateLimited bit = 0,
    @RateLimitScope nvarchar(100) = NULL,
    @RequestBytes bigint = 0,
    @ResponseBytes bigint = 0,
    @SchwabUnits int = 0,
    @SchwabUpstreamRequests int = 0,
    @SchwabCacheHits int = 0,
    @ClientIpAddress varchar(45) = NULL,
    @ClientNetwork varchar(50) = NULL,
    @ClientCountry varchar(8) = NULL,
    @ClientFingerprintHash binary(32) = NULL,
    @UserAgent nvarchar(512) = NULL,
    @CfRayId varchar(100) = NULL,
    @McpSessionIdHash binary(32) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    IF NOT EXISTS
    (
        SELECT 1
        FROM invest.ApiTokens
        WHERE ApiTokenId = @ApiTokenId
          AND UserId = @UserId
    )
        THROW 50030, 'Token and user identity do not match.', 1;

    INSERT invest.ApiTokenUsageLog
    (
        ApiTokenId, UserId, RequestStartedAt, RequestCompletedAt, DurationMs,
        HttpMethod, RequestPath, HostName, RpcMethod, ToolName, HttpStatus,
        Outcome, ErrorType, WasRateLimited, RateLimitScope, RequestBytes,
        ResponseBytes, SchwabUnits, SchwabUpstreamRequests, SchwabCacheHits,
        ClientIpAddress, ClientNetwork, ClientCountry, ClientFingerprintHash,
        UserAgent, CfRayId, McpSessionIdHash
    )
    VALUES
    (
        @ApiTokenId, @UserId, @RequestStartedAt, @RequestCompletedAt,
        @DurationMs, @HttpMethod, @RequestPath, @HostName, @RpcMethod,
        @ToolName, @HttpStatus, @Outcome, @ErrorType, @WasRateLimited,
        @RateLimitScope, @RequestBytes, @ResponseBytes, @SchwabUnits,
        @SchwabUpstreamRequests, @SchwabCacheHits, @ClientIpAddress,
        @ClientNetwork, @ClientCountry, @ClientFingerprintHash, @UserAgent,
        @CfRayId, @McpSessionIdHash
    );
END;
GO

CREATE OR ALTER VIEW invest.ApiTokenUsageDaily
AS
    SELECT
        CONVERT(date, RequestStartedAt) AS UsageDate,
        UserId,
        ApiTokenId,
        COUNT_BIG(*) AS RequestCount,
        SUM(CASE WHEN Outcome = 'Success' THEN CONVERT(bigint, 1) ELSE 0 END)
            AS SuccessfulRequests,
        SUM(CASE WHEN WasRateLimited = 1 THEN CONVERT(bigint, 1) ELSE 0 END)
            AS RateLimitedRequests,
        SUM(CONVERT(bigint, SchwabUnits)) AS SchwabUnits,
        SUM(CONVERT(bigint, SchwabUpstreamRequests)) AS SchwabUpstreamRequests,
        SUM(CONVERT(bigint, SchwabCacheHits)) AS SchwabCacheHits,
        AVG(CONVERT(decimal(18, 2), DurationMs)) AS AverageDurationMs,
        MAX(RequestCompletedAt) AS LastRequestAt
    FROM invest.ApiTokenUsageLog
    GROUP BY CONVERT(date, RequestStartedAt), UserId, ApiTokenId;
GO

IF DATABASE_PRINCIPAL_ID(N'mcp_connector') IS NOT NULL
BEGIN
    DENY SELECT, INSERT, UPDATE, DELETE
        ON OBJECT::invest.ApiTokenUsageLog TO [mcp_connector];
    DENY SELECT ON OBJECT::invest.ApiTokenUsageDaily TO [mcp_connector];
    GRANT EXECUTE ON OBJECT::invest.RecordApiTokenUsage TO [mcp_connector];
END;
GO
