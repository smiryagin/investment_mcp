/*
    Include USD cash in the WiseLine Portal portfolio contract.

    MarketValue is total account value: USD cash plus valued positions.
    TotalCost and unrealized return remain position-only measures.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

IF OBJECT_ID(N'invest.CashBalances', N'U') IS NULL
    THROW 50140, 'Run sql/001_create_invest_schema.sql first.', 1;
IF OBJECT_ID(N'invest.PortalUserEntitlements', N'U') IS NULL
    THROW 50141, 'Run sql/012_add_portal_integration.sql first.', 1;
GO

CREATE OR ALTER PROCEDURE invest.Portal_GetPortfolios
    @TradeUserId uniqueidentifier
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

    ;WITH TransactionPositions AS
    (
        SELECT
            t.AccountId,
            UPPER(LTRIM(RTRIM(t.Symbol))) AS Symbol,
            SUM(CASE
                    WHEN UPPER(t.TransactionType) IN ('BUY', 'PURCHASE', 'OPENING_POSITION')
                        THEN COALESCE(t.Quantity, 0)
                    WHEN UPPER(t.TransactionType) IN ('SELL', 'SALE')
                        THEN -COALESCE(t.Quantity, 0)
                    ELSE 0
                END) AS Quantity,
            SUM(CASE
                    WHEN UPPER(t.TransactionType) IN ('BUY', 'PURCHASE', 'OPENING_POSITION')
                        THEN COALESCE(t.Quantity, 0)
                    ELSE 0
                END) AS PurchasedQuantity,
            SUM(CASE
                    WHEN UPPER(t.TransactionType) IN ('BUY', 'PURCHASE', 'OPENING_POSITION')
                        THEN COALESCE(NULLIF(ABS(t.GrossAmount), 0), ABS(t.Quantity * t.Price), 0)
                    ELSE 0
                END) AS PurchasedCost,
            MAX(t.OccurredAt) AS LastActivityAt
        FROM invest.Transactions AS t
        WHERE t.UserId = @TradeUserId
          AND t.IsDeleted = 0
          AND NULLIF(LTRIM(RTRIM(t.Symbol)), N'') IS NOT NULL
        GROUP BY t.AccountId, UPPER(LTRIM(RTRIM(t.Symbol)))
        HAVING SUM(CASE
                       WHEN UPPER(t.TransactionType) IN ('BUY', 'PURCHASE', 'OPENING_POSITION')
                           THEN COALESCE(t.Quantity, 0)
                       WHEN UPPER(t.TransactionType) IN ('SELL', 'SALE')
                           THEN -COALESCE(t.Quantity, 0)
                       ELSE 0
                   END) <> 0
    ),
    ValuedPositions AS
    (
        SELECT
            p.AccountId,
            p.Symbol,
            p.Quantity,
            CAST(p.PurchasedCost / NULLIF(p.PurchasedQuantity, 0) AS decimal(28, 10)) AS AveragePrice,
            CAST(COALESCE(latest.LastValue,
                          p.PurchasedCost / NULLIF(p.PurchasedQuantity, 0), 0) AS decimal(28, 10)) AS CurrentPrice,
            CAST(COALESCE(previous.LastValue, latest.LastValue,
                          p.PurchasedCost / NULLIF(p.PurchasedQuantity, 0), 0) AS decimal(28, 10)) AS PreviousPrice,
            p.LastActivityAt,
            latest.PriceDate
        FROM TransactionPositions AS p
        OUTER APPLY
        (
            SELECT TOP (1) i.SeriesId
            FROM invest.McpInstruments AS i
            WHERE UPPER(LTRIM(RTRIM(i.Symbol))) = p.Symbol
            ORDER BY i.SeriesId
        ) AS instrument
        OUTER APPLY
        (
            SELECT TOP (1) sd.LastValue, sd.[Date] AS PriceDate
            FROM dbo.SeriesData AS sd
            WHERE sd.SeriesId = instrument.SeriesId
            ORDER BY sd.[Date] DESC
        ) AS latest
        OUTER APPLY
        (
            SELECT TOP (1) sd.LastValue
            FROM dbo.SeriesData AS sd
            WHERE sd.SeriesId = instrument.SeriesId
              AND sd.[Date] < latest.PriceDate
            ORDER BY sd.[Date] DESC
        ) AS previous
    )
    SELECT
        a.AccountId AS PortfolioId,
        a.AccountName AS [Name],
        strategy.RuleName AS StrategyName,
        CAST(COALESCE(valueset.MarketValue, 0) + COALESCE(cashset.CashBalance, 0)
             AS decimal(28, 2)) AS MarketValue,
        CAST(COALESCE(cashset.CashBalance, 0) AS decimal(28, 2)) AS CashBalance,
        CAST(COALESCE(valueset.DayChange, 0) AS decimal(28, 2)) AS DayChange,
        CAST(CASE
                 WHEN COALESCE(valueset.PreviousMarketValue, 0)
                      + COALESCE(cashset.CashBalance, 0) = 0 THEN 0
                 ELSE COALESCE(valueset.DayChange, 0)
                      / (COALESCE(valueset.PreviousMarketValue, 0)
                         + COALESCE(cashset.CashBalance, 0)) * 100
             END AS decimal(18, 4)) AS DayChangePercent,
        CONVERT(int, COALESCE(valueset.PositionCount, 0)) AS PositionCount,
        lastupdate.UpdatedAt
    FROM invest.Accounts AS a
    OUTER APPLY
    (
        SELECT TOP (1) r.RuleName
        FROM invest.StrategyRules AS r
        WHERE r.UserId = @TradeUserId
          AND r.IsEnabled = 1
          AND (r.AccountId = a.AccountId OR r.AccountId IS NULL)
        ORDER BY CASE WHEN r.AccountId = a.AccountId THEN 0 ELSE 1 END, r.UpdatedAt DESC
    ) AS strategy
    OUTER APPLY
    (
        SELECT
            SUM(v.Quantity * v.CurrentPrice) AS MarketValue,
            SUM(v.Quantity * (v.CurrentPrice - v.PreviousPrice)) AS DayChange,
            SUM(v.Quantity * v.PreviousPrice) AS PreviousMarketValue,
            COUNT_BIG(*) AS PositionCount,
            MAX(v.LastActivityAt) AS LastActivityAt
        FROM ValuedPositions AS v
        WHERE v.AccountId = a.AccountId
    ) AS valueset
    OUTER APPLY
    (
        SELECT
            SUM(cb.TotalAmount) AS CashBalance,
            MAX(cb.UpdatedAt) AS LastActivityAt
        FROM invest.CashBalances AS cb
        WHERE cb.UserId = @TradeUserId
          AND cb.AccountId = a.AccountId
          AND cb.Currency = 'USD'
    ) AS cashset
    OUTER APPLY
    (
        SELECT MAX(changes.UpdatedAt) AS UpdatedAt
        FROM (VALUES (a.UpdatedAt), (valueset.LastActivityAt), (cashset.LastActivityAt))
             AS changes(UpdatedAt)
    ) AS lastupdate
    WHERE a.OwnerUserId = @TradeUserId
      AND a.IsActive = 1
    ORDER BY a.AccountName, a.AccountId;
END;
GO

CREATE OR ALTER PROCEDURE invest.Portal_GetPortfolio
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

    CREATE TABLE #PortalPositions
    (
        PositionId nvarchar(50) NOT NULL,
        Symbol nvarchar(50) NOT NULL,
        [Description] nvarchar(200) NULL,
        Quantity decimal(28, 10) NOT NULL,
        AveragePrice decimal(28, 10) NULL,
        CurrentPrice decimal(28, 10) NULL,
        MarketValue decimal(38, 10) NOT NULL,
        TotalCost decimal(38, 10) NOT NULL,
        UnrealizedGain decimal(38, 10) NOT NULL,
        UnrealizedGainPercent decimal(18, 4) NOT NULL
    );

    ;WITH TransactionPositions AS
    (
        SELECT
            UPPER(LTRIM(RTRIM(t.Symbol))) AS Symbol,
            SUM(CASE
                    WHEN UPPER(t.TransactionType) IN ('BUY', 'PURCHASE', 'OPENING_POSITION')
                        THEN COALESCE(t.Quantity, 0)
                    WHEN UPPER(t.TransactionType) IN ('SELL', 'SALE')
                        THEN -COALESCE(t.Quantity, 0)
                    ELSE 0
                END) AS Quantity,
            SUM(CASE
                    WHEN UPPER(t.TransactionType) IN ('BUY', 'PURCHASE', 'OPENING_POSITION')
                        THEN COALESCE(t.Quantity, 0)
                    ELSE 0
                END) AS PurchasedQuantity,
            SUM(CASE
                    WHEN UPPER(t.TransactionType) IN ('BUY', 'PURCHASE', 'OPENING_POSITION')
                        THEN COALESCE(NULLIF(ABS(t.GrossAmount), 0), ABS(t.Quantity * t.Price), 0)
                    ELSE 0
                END) AS PurchasedCost
        FROM invest.Transactions AS t
        WHERE t.UserId = @TradeUserId
          AND t.AccountId = @PortfolioId
          AND t.IsDeleted = 0
          AND NULLIF(LTRIM(RTRIM(t.Symbol)), N'') IS NOT NULL
        GROUP BY UPPER(LTRIM(RTRIM(t.Symbol)))
        HAVING SUM(CASE
                       WHEN UPPER(t.TransactionType) IN ('BUY', 'PURCHASE', 'OPENING_POSITION')
                           THEN COALESCE(t.Quantity, 0)
                       WHEN UPPER(t.TransactionType) IN ('SELL', 'SALE')
                           THEN -COALESCE(t.Quantity, 0)
                       ELSE 0
                   END) <> 0
    )
    INSERT #PortalPositions
        (PositionId, Symbol, [Description], Quantity, AveragePrice, CurrentPrice,
         MarketValue, TotalCost, UnrealizedGain, UnrealizedGainPercent)
    SELECT
        p.Symbol,
        p.Symbol,
        instrument.[Name],
        p.Quantity,
        prices.AveragePrice,
        latest.LastValue,
        valueset.MarketValue,
        valueset.TotalCost,
        valueset.MarketValue - valueset.TotalCost,
        CAST(CASE
                 WHEN valueset.TotalCost = 0 THEN 0
                 ELSE (valueset.MarketValue - valueset.TotalCost) / valueset.TotalCost * 100
             END AS decimal(18, 4))
    FROM TransactionPositions AS p
    OUTER APPLY
    (
        SELECT TOP (1) i.SeriesId, i.[Name]
        FROM invest.McpInstruments AS i
        WHERE UPPER(LTRIM(RTRIM(i.Symbol))) = p.Symbol
        ORDER BY i.SeriesId
    ) AS instrument
    OUTER APPLY
    (
        SELECT TOP (1) sd.LastValue
        FROM dbo.SeriesData AS sd
        WHERE sd.SeriesId = instrument.SeriesId
        ORDER BY sd.[Date] DESC
    ) AS latest
    CROSS APPLY
    (
        SELECT CAST(p.PurchasedCost / NULLIF(p.PurchasedQuantity, 0) AS decimal(28, 10)) AS AveragePrice
    ) AS prices
    CROSS APPLY
    (
        SELECT
            CAST(p.Quantity * COALESCE(latest.LastValue, prices.AveragePrice, 0) AS decimal(38, 10)) AS MarketValue,
            CAST(p.Quantity * COALESCE(prices.AveragePrice, 0) AS decimal(38, 10)) AS TotalCost
    ) AS valueset
    WHERE EXISTS
    (
        SELECT 1
        FROM invest.Accounts AS owned
        WHERE owned.AccountId = @PortfolioId
          AND owned.OwnerUserId = @TradeUserId
          AND owned.IsActive = 1
    );

    SELECT
        a.AccountId AS PortfolioId,
        a.AccountName AS [Name],
        COALESCE(a.ProviderName, CONVERT(nvarchar(100), a.AccountType)) AS [Description],
        strategy.RuleName AS StrategyName,
        CAST(COALESCE(SUM(p.MarketValue), 0) + COALESCE(cashset.CashBalance, 0)
             AS decimal(28, 2)) AS MarketValue,
        CAST(COALESCE(cashset.CashBalance, 0) AS decimal(28, 2)) AS CashBalance,
        CAST(COALESCE(SUM(p.TotalCost), 0) AS decimal(28, 2)) AS TotalCost,
        CAST(COALESCE(SUM(p.UnrealizedGain), 0) AS decimal(28, 2)) AS UnrealizedGain,
        CAST(CASE
                 WHEN COALESCE(SUM(p.TotalCost), 0) = 0 THEN 0
                 ELSE SUM(p.UnrealizedGain) / SUM(p.TotalCost) * 100
             END AS decimal(18, 4)) AS UnrealizedGainPercent
    FROM invest.Accounts AS a
    LEFT JOIN #PortalPositions AS p ON 1 = 1
    OUTER APPLY
    (
        SELECT TOP (1) r.RuleName
        FROM invest.StrategyRules AS r
        WHERE r.UserId = @TradeUserId
          AND r.IsEnabled = 1
          AND (r.AccountId = a.AccountId OR r.AccountId IS NULL)
        ORDER BY CASE WHEN r.AccountId = a.AccountId THEN 0 ELSE 1 END, r.UpdatedAt DESC
    ) AS strategy
    OUTER APPLY
    (
        SELECT SUM(cb.TotalAmount) AS CashBalance
        FROM invest.CashBalances AS cb
        WHERE cb.UserId = @TradeUserId
          AND cb.AccountId = a.AccountId
          AND cb.Currency = 'USD'
    ) AS cashset
    WHERE a.AccountId = @PortfolioId
      AND a.OwnerUserId = @TradeUserId
      AND a.IsActive = 1
    GROUP BY a.AccountId, a.AccountName, a.ProviderName, a.AccountType,
             strategy.RuleName, cashset.CashBalance;

    SELECT
        PositionId,
        Symbol,
        [Description],
        Quantity,
        AveragePrice,
        CurrentPrice,
        CAST(MarketValue AS decimal(28, 2)) AS MarketValue,
        CAST(UnrealizedGain AS decimal(28, 2)) AS UnrealizedGain,
        UnrealizedGainPercent
    FROM #PortalPositions
    ORDER BY Symbol;
END;
GO

GRANT EXECUTE ON OBJECT::invest.Portal_GetPortfolios
    TO [investment_portal_runtime];
GRANT EXECUTE ON OBJECT::invest.Portal_GetPortfolio
    TO [investment_portal_runtime];
GO
