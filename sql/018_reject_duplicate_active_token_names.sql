/*
    Require active API token names to be unique per user.

    Names are trimmed before storage and compared case-insensitively. Revoked or
    expired tokens do not reserve their names. The existing user-row lock keeps
    the duplicate-name check and two-active-token limit safe under concurrency.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

CREATE OR ALTER PROCEDURE invest.IssueApiToken
    @AuthenticationSubject nvarchar(450),
    @TokenName nvarchar(100),
    @ExpiresAt datetimeoffset(7) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    SET @TokenName = LTRIM(RTRIM(@TokenName));

    IF NULLIF(@TokenName, N'') IS NULL
        THROW 50011, 'Token name is required.', 1;
    IF @ExpiresAt IS NOT NULL AND @ExpiresAt <= SYSDATETIMEOFFSET()
        THROW 50012, 'Token expiration must be in the future.', 1;

    DECLARE @UserId uniqueidentifier;
    DECLARE @Now datetimeoffset(7) = SYSDATETIMEOFFSET();
    DECLARE @ActiveTokenCount int;
    DECLARE @RandomBytes binary(32) = CRYPT_GEN_RANDOM(32);
    DECLARE @PlaintextToken varchar(69) =
        'imcp_' + LOWER(CONVERT(varchar(64), @RandomBytes, 2));
    DECLARE @TokenHash binary(32) =
        HASHBYTES('SHA2_256', CONVERT(varbinary(8000), @PlaintextToken));
    DECLARE @ApiTokenId uniqueidentifier = NEWID();

    BEGIN TRY
        BEGIN TRANSACTION;

        SELECT @UserId = UserId
        FROM invest.Users WITH (UPDLOCK, HOLDLOCK)
        WHERE AuthenticationSubject = @AuthenticationSubject
          AND IsActive = 1;

        IF @UserId IS NULL
            THROW 50010, 'Active user not found.', 1;

        IF EXISTS
        (
            SELECT 1
            FROM invest.ApiTokens
            WHERE UserId = @UserId
              AND RevokedAt IS NULL
              AND (ExpiresAt IS NULL OR ExpiresAt > @Now)
              AND TokenName COLLATE Latin1_General_100_CI_AS =
                  @TokenName COLLATE Latin1_General_100_CI_AS
        )
            THROW 50015, 'An active API token with this name already exists.', 1;

        SELECT @ActiveTokenCount = COUNT_BIG(*)
        FROM invest.ApiTokens
        WHERE UserId = @UserId
          AND RevokedAt IS NULL
          AND (ExpiresAt IS NULL OR ExpiresAt > @Now);

        IF @ActiveTokenCount >= 2
            THROW 50014, 'A user can have at most two active API tokens.', 1;

        INSERT invest.ApiTokens
            (ApiTokenId, UserId, TokenName, TokenPrefix, TokenHash, ExpiresAt)
        VALUES
            (@ApiTokenId, @UserId, @TokenName, LEFT(@PlaintextToken, 17), @TokenHash, @ExpiresAt);

        INSERT invest.AuditLog
            (ActorUserId, Operation, TargetType, TargetId, DetailsJson)
        VALUES
            (@UserId, N'ISSUE_API_TOKEN', N'ApiToken', CONVERT(nvarchar(36), @ApiTokenId),
             CONCAT(N'{"tokenName":"', STRING_ESCAPE(@TokenName, 'json'), N'"}'));

        COMMIT TRANSACTION;

        SELECT
            @ApiTokenId AS ApiTokenId,
            @PlaintextToken AS PlaintextToken,
            LEFT(@PlaintextToken, 17) AS TokenPrefix,
            @ExpiresAt AS ExpiresAt;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END;
GO
