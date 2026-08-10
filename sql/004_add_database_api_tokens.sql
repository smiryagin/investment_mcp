/*
    Database-backed bearer tokens for the hosted Investment MCP server.

    Plaintext tokens are returned once by invest.IssueApiToken and are never
    stored. The MCP runtime receives EXECUTE only on AuthenticateApiToken.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;

BEGIN TRY
    BEGIN TRANSACTION;

    IF OBJECT_ID(N'invest.ApiTokens', N'U') IS NULL
    BEGIN
        CREATE TABLE invest.ApiTokens
        (
            ApiTokenId  uniqueidentifier NOT NULL
                CONSTRAINT DF_ApiTokens_ApiTokenId DEFAULT NEWSEQUENTIALID(),
            UserId      uniqueidentifier NOT NULL,
            TokenName   nvarchar(100) NOT NULL,
            TokenPrefix varchar(20) NOT NULL,
            TokenHash   binary(32) NOT NULL,
            ExpiresAt   datetimeoffset(7) NULL,
            RevokedAt   datetimeoffset(7) NULL,
            LastUsedAt  datetimeoffset(7) NULL,
            CreatedAt   datetimeoffset(7) NOT NULL
                CONSTRAINT DF_ApiTokens_CreatedAt DEFAULT SYSDATETIMEOFFSET(),
            RowVersion  rowversion NOT NULL,

            CONSTRAINT PK_ApiTokens PRIMARY KEY (ApiTokenId),
            CONSTRAINT UQ_ApiTokens_TokenHash UNIQUE (TokenHash),
            CONSTRAINT FK_ApiTokens_User
                FOREIGN KEY (UserId) REFERENCES invest.Users (UserId),
            CONSTRAINT CK_ApiTokens_TokenPrefix
                CHECK (TokenPrefix LIKE 'imcp[_]%')
        );

        CREATE INDEX IX_ApiTokens_User_CreatedAt
            ON invest.ApiTokens (UserId, CreatedAt DESC);
    END;

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0
        ROLLBACK TRANSACTION;
    THROW;
END CATCH;

EXEC sys.sp_executesql N'
CREATE OR ALTER PROCEDURE invest.AuthenticateApiToken
    @TokenHash binary(32)
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @Now datetimeoffset(7) = SYSDATETIMEOFFSET();
    DECLARE @ApiTokenId uniqueidentifier;
    DECLARE @UserId uniqueidentifier;
    DECLARE @AuthenticationSubject nvarchar(450);
    DECLARE @ExpiresAt datetimeoffset(7);

    SELECT
        @ApiTokenId = t.ApiTokenId,
        @UserId = u.UserId,
        @AuthenticationSubject = u.AuthenticationSubject,
        @ExpiresAt = t.ExpiresAt
    FROM invest.ApiTokens AS t
    INNER JOIN invest.Users AS u ON u.UserId = t.UserId
    WHERE t.TokenHash = @TokenHash
      AND t.RevokedAt IS NULL
      AND (t.ExpiresAt IS NULL OR t.ExpiresAt > @Now)
      AND u.IsActive = 1;

    IF @ApiTokenId IS NULL
        RETURN;

    -- Avoid one database write for every MCP request while retaining useful
    -- activity information for token management and incident response.
    UPDATE invest.ApiTokens
    SET LastUsedAt = @Now
    WHERE ApiTokenId = @ApiTokenId
      AND (LastUsedAt IS NULL OR LastUsedAt < DATEADD(MINUTE, -15, @Now));

    SELECT
        @ApiTokenId AS ApiTokenId,
        @UserId AS UserId,
        @AuthenticationSubject AS AuthenticationSubject,
        @ExpiresAt AS ExpiresAt;
END;
';

EXEC sys.sp_executesql N'
CREATE OR ALTER PROCEDURE invest.IssueApiToken
    @AuthenticationSubject nvarchar(450),
    @TokenName nvarchar(100),
    @ExpiresAt datetimeoffset(7) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @UserId uniqueidentifier;
    SELECT @UserId = UserId
    FROM invest.Users
    WHERE AuthenticationSubject = @AuthenticationSubject
      AND IsActive = 1;

    IF @UserId IS NULL
        THROW 50010, ''Active user not found.'', 1;
    IF NULLIF(LTRIM(RTRIM(@TokenName)), N'''') IS NULL
        THROW 50011, ''Token name is required.'', 1;
    IF @ExpiresAt IS NOT NULL AND @ExpiresAt <= SYSDATETIMEOFFSET()
        THROW 50012, ''Token expiration must be in the future.'', 1;

    DECLARE @RandomBytes binary(32) = CRYPT_GEN_RANDOM(32);
    DECLARE @PlaintextToken varchar(69) =
        ''imcp_'' + LOWER(CONVERT(varchar(64), @RandomBytes, 2));
    DECLARE @TokenHash binary(32) =
        HASHBYTES(''SHA2_256'', CONVERT(varbinary(8000), @PlaintextToken));
    DECLARE @ApiTokenId uniqueidentifier = NEWID();

    BEGIN TRANSACTION;

    INSERT invest.ApiTokens
        (ApiTokenId, UserId, TokenName, TokenPrefix, TokenHash, ExpiresAt)
    VALUES
        (@ApiTokenId, @UserId, @TokenName, LEFT(@PlaintextToken, 17), @TokenHash, @ExpiresAt);

    INSERT invest.AuditLog
        (ActorUserId, Operation, TargetType, TargetId, DetailsJson)
    VALUES
        (@UserId, N''ISSUE_API_TOKEN'', N''ApiToken'', CONVERT(nvarchar(36), @ApiTokenId),
         CONCAT(N''{"tokenName":"'', STRING_ESCAPE(@TokenName, ''json''), N''"}''));

    COMMIT TRANSACTION;

    -- This is the only time the plaintext token is returned.
    SELECT
        @ApiTokenId AS ApiTokenId,
        @PlaintextToken AS PlaintextToken,
        LEFT(@PlaintextToken, 17) AS TokenPrefix,
        @ExpiresAt AS ExpiresAt;
END;
';

EXEC sys.sp_executesql N'
CREATE OR ALTER PROCEDURE invest.ListApiTokens
    @AuthenticationSubject nvarchar(450)
AS
BEGIN
    SET NOCOUNT ON;

    SELECT
        t.ApiTokenId,
        t.TokenName,
        t.TokenPrefix,
        t.ExpiresAt,
        t.RevokedAt,
        t.LastUsedAt,
        t.CreatedAt,
        t.RowVersion
    FROM invest.ApiTokens AS t
    INNER JOIN invest.Users AS u ON u.UserId = t.UserId
    WHERE u.AuthenticationSubject = @AuthenticationSubject
    ORDER BY t.CreatedAt DESC;
END;
';

EXEC sys.sp_executesql N'
CREATE OR ALTER PROCEDURE invest.RevokeApiToken
    @AuthenticationSubject nvarchar(450),
    @ApiTokenId uniqueidentifier
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @UserId uniqueidentifier;
    SELECT @UserId = UserId
    FROM invest.Users
    WHERE AuthenticationSubject = @AuthenticationSubject;

    BEGIN TRANSACTION;

    UPDATE t
    SET RevokedAt = COALESCE(RevokedAt, SYSDATETIMEOFFSET())
    FROM invest.ApiTokens AS t
    WHERE t.ApiTokenId = @ApiTokenId
      AND t.UserId = @UserId;

    IF @@ROWCOUNT = 0
    BEGIN
        ROLLBACK TRANSACTION;
        THROW 50013, ''Token not found.'', 1;
    END;

    INSERT invest.AuditLog
        (ActorUserId, Operation, TargetType, TargetId)
    VALUES
        (@UserId, N''REVOKE_API_TOKEN'', N''ApiToken'', CONVERT(nvarchar(36), @ApiTokenId));

    COMMIT TRANSACTION;
END;
';

IF DATABASE_PRINCIPAL_ID(N'mcp_connector') IS NOT NULL
BEGIN
    DENY SELECT, INSERT, UPDATE, DELETE ON OBJECT::invest.ApiTokens
        TO [mcp_connector];
    GRANT EXECUTE ON OBJECT::invest.AuthenticateApiToken
        TO [mcp_connector];
    DENY EXECUTE ON OBJECT::invest.IssueApiToken
        TO [mcp_connector];
    DENY EXECUTE ON OBJECT::invest.ListApiTokens
        TO [mcp_connector];
    DENY EXECUTE ON OBJECT::invest.RevokeApiToken
        TO [mcp_connector];
END;

-- Deliberately do not grant the MCP runtime SELECT on invest.ApiTokens or
-- EXECUTE on IssueApiToken, ListApiTokens, or RevokeApiToken. A future website
-- should use a separate least-privilege database principal.
