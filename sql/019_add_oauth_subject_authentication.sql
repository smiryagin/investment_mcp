/*
    Resolve a portal-issued OAuth subject through the current Trade entitlement.

    The MCP runtime receives procedure execution only. It does not receive direct
    access to invest.PortalUserEntitlements or the WiseLinePortal database.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

IF OBJECT_ID(N'invest.PortalUserEntitlements', N'U') IS NULL
    THROW 50150, 'Run sql/012_add_portal_integration.sql first.', 1;
GO

CREATE OR ALTER PROCEDURE invest.AuthenticateOAuthSubject
    @AuthenticationSubject nvarchar(450)
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @Now datetimeoffset(7) = SYSDATETIMEOFFSET();

    SELECT
        users.UserId,
        users.AuthenticationSubject,
        users.DisplayName,
        entitlement.EntitledThrough
    FROM invest.Users AS users
    INNER JOIN invest.PortalUserEntitlements AS entitlement
        ON entitlement.TradeUserId = users.UserId
    WHERE users.AuthenticationSubject = LOWER(LTRIM(RTRIM(@AuthenticationSubject)))
      AND users.AuthenticationSubject LIKE N'portal:%'
      AND users.IsActive = 1
      AND entitlement.IsEntitled = 1
      AND (entitlement.EntitledThrough IS NULL OR entitlement.EntitledThrough > @Now);
END;
GO

IF DATABASE_PRINCIPAL_ID(N'mcp_connector') IS NULL
    THROW 50151, 'Database user [mcp_connector] does not exist.', 1;
GO

GRANT EXECUTE ON OBJECT::invest.AuthenticateOAuthSubject
    TO [mcp_connector];
GO
