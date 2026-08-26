/*
    Least-privilege access to the legacy dbo.TDAconfig Schwab OAuth row.

    The MCP runtime can read exactly one configured row through a procedure and
    can update only its access token and UTC update timestamp. It receives no
    direct SELECT or UPDATE permission on dbo.TDAconfig.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;

IF SCHEMA_ID(N'invest') IS NULL
    THROW 50001, 'Schema [invest] does not exist. Run migration 001 first.', 1;

IF OBJECT_ID(N'dbo.TDAconfig', N'U') IS NULL
    THROW 50002, 'Table [dbo].[TDAconfig] does not exist.', 1;
GO

CREATE OR ALTER PROCEDURE invest.GetSchwabOAuthConfig
    @TDAconfigId int
WITH EXECUTE AS OWNER
AS
BEGIN
    SET NOCOUNT ON;

    SELECT
        TDAconfigId,
        Name,
        RefreshToken,
        client_id,
        client_secret,
        AccessToken,
        AccessTokenUpdateTime,
        RefreshTokenUpdateTime
    FROM dbo.TDAconfig
    WHERE TDAconfigId = @TDAconfigId;
END;
GO

CREATE OR ALTER PROCEDURE invest.UpdateSchwabAccessToken
    @TDAconfigId int,
    @AccessToken nvarchar(max)
WITH EXECUTE AS OWNER
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    IF NULLIF(LTRIM(RTRIM(@AccessToken)), N'') IS NULL
        THROW 50003, 'Access token is required.', 1;

    UPDATE dbo.TDAconfig
    SET AccessToken = @AccessToken,
        AccessTokenUpdateTime = GETUTCDATE()
    WHERE TDAconfigId = @TDAconfigId;

    IF @@ROWCOUNT = 0
        THROW 50004, 'Schwab OAuth configuration was not found.', 1;

    SELECT
        TDAconfigId,
        AccessTokenUpdateTime
    FROM dbo.TDAconfig
    WHERE TDAconfigId = @TDAconfigId;
END;
GO

IF DATABASE_PRINCIPAL_ID(N'mcp_connector') IS NULL
    THROW 50005, 'Database user [mcp_connector] does not exist.', 1;
GO

GRANT EXECUTE ON OBJECT::invest.GetSchwabOAuthConfig TO [mcp_connector];
GRANT EXECUTE ON OBJECT::invest.UpdateSchwabAccessToken TO [mcp_connector];
GO
