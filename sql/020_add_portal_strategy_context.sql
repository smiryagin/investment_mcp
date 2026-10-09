/*
    Expose portfolio-specific and global strategy rules to WiseLine Portal.

    The portal runtime receives procedure execution only. It does not receive
    direct SELECT permission on invest.StrategyRules.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

IF OBJECT_ID(N'invest.StrategyRules', N'U') IS NULL
    THROW 50160, 'Run sql/001_create_invest_schema.sql first.', 1;
IF OBJECT_ID(N'invest.PortalUserEntitlements', N'U') IS NULL
    THROW 50161, 'Run sql/012_add_portal_integration.sql first.', 1;
GO

CREATE OR ALTER PROCEDURE invest.Portal_GetPortfolioStrategies
    @TradeUserId uniqueidentifier,
    @PortfolioId uniqueidentifier
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @Now datetimeoffset(7) = SYSDATETIMEOFFSET();
    IF NOT EXISTS
    (
        SELECT 1
        FROM invest.PortalUserEntitlements
        WHERE TradeUserId = @TradeUserId
          AND IsEntitled = 1
          AND (EntitledThrough IS NULL OR EntitledThrough > @Now)
    )
        THROW 50130, 'An active portal subscription is required.', 1;

    SELECT
        r.StrategyRuleId,
        r.AccountId,
        r.RuleName AS StrategyName,
        r.RuleType AS StrategyType,
        r.RuleJson,
        CASE
            WHEN r.AccountId IS NULL THEN N'global'
            ELSE N'portfolio'
        END AS StrategyScope,
        r.UpdatedAt
    FROM invest.StrategyRules AS r
    WHERE r.UserId = @TradeUserId
      AND r.IsEnabled = 1
      AND (r.AccountId = @PortfolioId OR r.AccountId IS NULL)
      AND EXISTS
      (
          SELECT 1
          FROM invest.Accounts AS a
          WHERE a.AccountId = @PortfolioId
            AND a.OwnerUserId = @TradeUserId
            AND a.IsActive = 1
      )
    ORDER BY
        CASE WHEN r.AccountId = @PortfolioId THEN 0 ELSE 1 END,
        r.UpdatedAt DESC,
        r.RuleName;
END;
GO

IF DATABASE_PRINCIPAL_ID(N'investment_portal_runtime') IS NULL
    THROW 50162, 'Run sql/012_add_portal_integration.sql first.', 1;
GO

GRANT EXECUTE ON OBJECT::invest.Portal_GetPortfolioStrategies
    TO [investment_portal_runtime];
GO

IF DATABASE_PRINCIPAL_ID(N'mcp_connector') IS NOT NULL
BEGIN
    DENY EXECUTE ON OBJECT::invest.Portal_GetPortfolioStrategies
        TO [mcp_connector];
END;
GO
