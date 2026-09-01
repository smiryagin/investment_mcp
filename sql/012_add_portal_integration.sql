/*
    Least-privilege contract between WiseLinePortal and the Trade database.

    Portal identities are provisioned idempotently. Entitlement state is kept
    in Trade so the hosted MCP can reject portal-managed users after their
    subscription boundary without connecting to the portal database.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

IF OBJECT_ID(N'invest.Users', N'U') IS NULL
    THROW 50100, 'Run sql/001_create_invest_schema.sql first.', 1;
IF OBJECT_ID(N'invest.ApiTokens', N'U') IS NULL
    THROW 50101, 'Run sql/004_add_database_api_tokens.sql first.', 1;
IF OBJECT_ID(N'invest.McpInstruments', N'V') IS NULL
    THROW 50102, 'Run sql/009_create_mcp_instruments_view.sql first.', 1;
GO

BEGIN TRY
    BEGIN TRANSACTION;

    IF OBJECT_ID(N'invest.PortalUserEntitlements', N'U') IS NULL
    BEGIN
        CREATE TABLE invest.PortalUserEntitlements
        (
            PortalUserId    uniqueidentifier NOT NULL,
            TradeUserId     uniqueidentifier NOT NULL,
            IsEntitled      bit NOT NULL
                CONSTRAINT DF_PortalUserEntitlements_IsEntitled DEFAULT (0),
            EntitledThrough datetimeoffset(7) NULL,
            UpdatedAt       datetimeoffset(7) NOT NULL
                CONSTRAINT DF_PortalUserEntitlements_UpdatedAt DEFAULT SYSDATETIMEOFFSET(),
            RowVersion      rowversion NOT NULL,

            CONSTRAINT PK_PortalUserEntitlements PRIMARY KEY (PortalUserId),
            CONSTRAINT UQ_PortalUserEntitlements_TradeUserId UNIQUE (TradeUserId),
            CONSTRAINT FK_PortalUserEntitlements_User
                FOREIGN KEY (TradeUserId) REFERENCES invest.Users (UserId)
        );
    END;

    IF COL_LENGTH(N'invest.ApiTokens', N'IsPortalManaged') IS NULL
    BEGIN
        ALTER TABLE invest.ApiTokens
            ADD IsPortalManaged bit NOT NULL
                CONSTRAINT DF_ApiTokens_IsPortalManaged DEFAULT (0) WITH VALUES;
    END;

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0
        ROLLBACK TRANSACTION;
    THROW;
END CATCH;
GO

CREATE OR ALTER PROCEDURE invest.Portal_EnsureUser
    @PortalUserId uniqueidentifier,
    @DisplayName nvarchar(200),
    @Email nvarchar(320) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    IF @PortalUserId IS NULL
        THROW 50110, 'Portal user identifier is required.', 1;
    IF NULLIF(LTRIM(RTRIM(@DisplayName)), N'') IS NULL
        THROW 50111, 'Display name is required.', 1;

    DECLARE @AuthenticationSubject nvarchar(450) =
        N'portal:' + LOWER(CONVERT(nvarchar(36), @PortalUserId));
    DECLARE @TradeUserId uniqueidentifier;
    DECLARE @IsActive bit;

    SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;
    BEGIN TRY
        BEGIN TRANSACTION;

        SELECT @TradeUserId = TradeUserId
        FROM invest.PortalUserEntitlements WITH (UPDLOCK, HOLDLOCK)
        WHERE PortalUserId = @PortalUserId;

        IF @TradeUserId IS NULL
        BEGIN
            SELECT
                @TradeUserId = UserId,
                @IsActive = IsActive
            FROM invest.Users WITH (UPDLOCK, HOLDLOCK)
            WHERE AuthenticationSubject = @AuthenticationSubject;

            IF @TradeUserId IS NULL
            BEGIN
                SET @TradeUserId = NEWID();
                INSERT invest.Users
                    (UserId, AuthenticationSubject, DisplayName, Email, IsActive)
                VALUES
                    (@TradeUserId, @AuthenticationSubject, LTRIM(RTRIM(@DisplayName)),
                     NULLIF(LTRIM(RTRIM(@Email)), N''), 1);
            END
            ELSE IF @IsActive = 0
                THROW 50112, 'The linked Trade user is inactive.', 1;
            ELSE
            BEGIN
                UPDATE invest.Users
                SET DisplayName = LTRIM(RTRIM(@DisplayName)),
                    Email = NULLIF(LTRIM(RTRIM(@Email)), N''),
                    UpdatedAt = SYSDATETIMEOFFSET()
                WHERE UserId = @TradeUserId;
            END;

            INSERT invest.PortalUserEntitlements
                (PortalUserId, TradeUserId, IsEntitled, EntitledThrough)
            VALUES
                (@PortalUserId, @TradeUserId, 0, NULL);
        END
        ELSE IF NOT EXISTS
        (
            SELECT 1
            FROM invest.Users
            WHERE UserId = @TradeUserId
              AND IsActive = 1
        )
            THROW 50112, 'The linked Trade user is inactive.', 1;

        COMMIT TRANSACTION;

        SELECT @TradeUserId AS TradeUserId;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END;
GO

CREATE OR ALTER PROCEDURE invest.Portal_SetEntitlement
    @TradeUserId uniqueidentifier,
    @PortalUserId uniqueidentifier,
    @IsEntitled bit,
    @EntitledThrough datetimeoffset(7) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    IF @TradeUserId IS NULL OR @PortalUserId IS NULL
        THROW 50120, 'Portal and Trade user identifiers are required.', 1;

    DECLARE @Now datetimeoffset(7) = SYSDATETIMEOFFSET();
    IF @EntitledThrough IS NOT NULL AND @EntitledThrough <= @Now
        SET @IsEntitled = 0;

    BEGIN TRY
        BEGIN TRANSACTION;

        IF NOT EXISTS
        (
            SELECT 1
            FROM invest.Users WITH (UPDLOCK, HOLDLOCK)
            WHERE UserId = @TradeUserId
              AND IsActive = 1
        )
            THROW 50121, 'Active Trade user not found.', 1;

        IF EXISTS
        (
            SELECT 1
            FROM invest.PortalUserEntitlements WITH (UPDLOCK, HOLDLOCK)
            WHERE (PortalUserId = @PortalUserId AND TradeUserId <> @TradeUserId)
               OR (TradeUserId = @TradeUserId AND PortalUserId <> @PortalUserId)
        )
            THROW 50122, 'Portal and Trade identity mapping conflicts with an existing link.', 1;

        IF EXISTS
        (
            SELECT 1
            FROM invest.PortalUserEntitlements WITH (UPDLOCK, HOLDLOCK)
            WHERE PortalUserId = @PortalUserId
              AND TradeUserId = @TradeUserId
        )
        BEGIN
            UPDATE invest.PortalUserEntitlements
            SET IsEntitled = @IsEntitled,
                EntitledThrough = CASE WHEN @IsEntitled = 1 THEN @EntitledThrough ELSE NULL END,
                UpdatedAt = @Now
            WHERE PortalUserId = @PortalUserId;
        END
        ELSE
        BEGIN
            INSERT invest.PortalUserEntitlements
                (PortalUserId, TradeUserId, IsEntitled, EntitledThrough, UpdatedAt)
            VALUES
                (@PortalUserId, @TradeUserId, @IsEntitled,
                 CASE WHEN @IsEntitled = 1 THEN @EntitledThrough ELSE NULL END, @Now);
        END;

        -- Portal-managed tokens follow the current paid/trial boundary. This
        -- also extends existing tokens after a successful renewal.
        UPDATE invest.ApiTokens
        SET ExpiresAt = CASE WHEN @IsEntitled = 1 THEN @EntitledThrough ELSE ExpiresAt END
        WHERE UserId = @TradeUserId
          AND IsPortalManaged = 1
          AND RevokedAt IS NULL;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END;
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
        CAST(COALESCE(valueset.MarketValue, 0) AS decimal(28, 2)) AS MarketValue,
        CAST(COALESCE(valueset.DayChange, 0) AS decimal(28, 2)) AS DayChange,
        CAST(CASE
                 WHEN COALESCE(valueset.PreviousMarketValue, 0) = 0 THEN 0
                 ELSE valueset.DayChange / valueset.PreviousMarketValue * 100
             END AS decimal(18, 4)) AS DayChangePercent,
        CONVERT(int, COALESCE(valueset.PositionCount, 0)) AS PositionCount,
        CASE
            WHEN valueset.LastActivityAt IS NULL OR a.UpdatedAt >= valueset.LastActivityAt THEN a.UpdatedAt
            ELSE valueset.LastActivityAt
        END AS UpdatedAt
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
        CAST(COALESCE(SUM(p.MarketValue), 0) AS decimal(28, 2)) AS MarketValue,
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
    WHERE a.AccountId = @PortfolioId
      AND a.OwnerUserId = @TradeUserId
      AND a.IsActive = 1
    GROUP BY a.AccountId, a.AccountName, a.ProviderName, a.AccountType, strategy.RuleName;

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

CREATE OR ALTER PROCEDURE invest.Portal_GetMcpTokens
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

    SELECT
        t.ApiTokenId AS TokenId,
        t.TokenName AS DisplayName,
        t.TokenPrefix,
        t.CreatedAt,
        t.LastUsedAt,
        t.ExpiresAt,
        CONVERT(bit, CASE WHEN t.RevokedAt IS NULL THEN 0 ELSE 1 END) AS IsRevoked
    FROM invest.ApiTokens AS t
    WHERE t.UserId = @TradeUserId
      AND t.IsPortalManaged = 1
    ORDER BY t.CreatedAt DESC;
END;
GO

CREATE OR ALTER PROCEDURE invest.Portal_CreateMcpToken
    @TradeUserId uniqueidentifier,
    @DisplayName nvarchar(100)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Now datetimeoffset(7) = SYSDATETIMEOFFSET();
    DECLARE @AuthenticationSubject nvarchar(450);
    DECLARE @EntitledThrough datetimeoffset(7);

    SELECT
        @AuthenticationSubject = u.AuthenticationSubject,
        @EntitledThrough = e.EntitledThrough
    FROM invest.Users AS u
    INNER JOIN invest.PortalUserEntitlements AS e ON e.TradeUserId = u.UserId
    WHERE u.UserId = @TradeUserId
      AND u.IsActive = 1
      AND e.IsEntitled = 1
      AND (e.EntitledThrough IS NULL OR e.EntitledThrough > @Now);

    IF @AuthenticationSubject IS NULL
        THROW 50130, 'An active portal subscription is required.', 1;

    DECLARE @Issued table
    (
        ApiTokenId uniqueidentifier NOT NULL,
        PlaintextToken varchar(69) NOT NULL,
        TokenPrefix varchar(20) NOT NULL,
        ExpiresAt datetimeoffset(7) NULL
    );

    BEGIN TRY
        BEGIN TRANSACTION;

        INSERT @Issued (ApiTokenId, PlaintextToken, TokenPrefix, ExpiresAt)
        EXEC invest.IssueApiToken
            @AuthenticationSubject = @AuthenticationSubject,
            @TokenName = @DisplayName,
            @ExpiresAt = @EntitledThrough;

        UPDATE t
        SET IsPortalManaged = 1
        FROM invest.ApiTokens AS t
        INNER JOIN @Issued AS issued ON issued.ApiTokenId = t.ApiTokenId;

        COMMIT TRANSACTION;

        SELECT
            issued.ApiTokenId AS TokenId,
            t.TokenName AS DisplayName,
            issued.PlaintextToken AS Token,
            issued.TokenPrefix,
            t.CreatedAt,
            issued.ExpiresAt
        FROM @Issued AS issued
        INNER JOIN invest.ApiTokens AS t ON t.ApiTokenId = issued.ApiTokenId;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END;
GO

CREATE OR ALTER PROCEDURE invest.Portal_RevokeMcpToken
    @TradeUserId uniqueidentifier,
    @TokenId uniqueidentifier
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

    DECLARE @AuthenticationSubject nvarchar(450);
    SELECT @AuthenticationSubject = u.AuthenticationSubject
    FROM invest.Users AS u
    INNER JOIN invest.ApiTokens AS t ON t.UserId = u.UserId
    WHERE u.UserId = @TradeUserId
      AND t.ApiTokenId = @TokenId
      AND t.IsPortalManaged = 1;

    IF @AuthenticationSubject IS NULL
        THROW 50131, 'Portal MCP token not found.', 1;

    EXEC invest.RevokeApiToken
        @AuthenticationSubject = @AuthenticationSubject,
        @ApiTokenId = @TokenId;
END;
GO

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
    LEFT JOIN invest.PortalUserEntitlements AS entitlement
        ON entitlement.TradeUserId = u.UserId
    WHERE t.TokenHash = @TokenHash
      AND t.RevokedAt IS NULL
      AND (t.ExpiresAt IS NULL OR t.ExpiresAt > @Now)
      AND u.IsActive = 1
      AND
      (
          entitlement.TradeUserId IS NULL
          OR
          (
              entitlement.IsEntitled = 1
              AND (entitlement.EntitledThrough IS NULL OR entitlement.EntitledThrough > @Now)
          )
      );

    IF @ApiTokenId IS NULL
        RETURN;

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
GO

IF DATABASE_PRINCIPAL_ID(N'investment_portal_runtime') IS NULL
    EXEC(N'CREATE ROLE [investment_portal_runtime] AUTHORIZATION dbo;');
GO

DENY SELECT, INSERT, UPDATE, DELETE ON SCHEMA::invest
    TO [investment_portal_runtime];
DENY SELECT ON OBJECT::dbo.SeriesData
    TO [investment_portal_runtime];
GRANT EXECUTE ON OBJECT::invest.Portal_EnsureUser
    TO [investment_portal_runtime];
GRANT EXECUTE ON OBJECT::invest.Portal_SetEntitlement
    TO [investment_portal_runtime];
GRANT EXECUTE ON OBJECT::invest.Portal_GetPortfolios
    TO [investment_portal_runtime];
GRANT EXECUTE ON OBJECT::invest.Portal_GetPortfolio
    TO [investment_portal_runtime];
GRANT EXECUTE ON OBJECT::invest.Portal_GetMcpTokens
    TO [investment_portal_runtime];
GRANT EXECUTE ON OBJECT::invest.Portal_CreateMcpToken
    TO [investment_portal_runtime];
GRANT EXECUTE ON OBJECT::invest.Portal_RevokeMcpToken
    TO [investment_portal_runtime];
GO

IF DATABASE_PRINCIPAL_ID(N'InvestmentPortal_Connector') IS NOT NULL
   AND NOT EXISTS
   (
       SELECT 1
       FROM sys.database_role_members AS drm
       INNER JOIN sys.database_principals AS role_principal
           ON role_principal.principal_id = drm.role_principal_id
       INNER JOIN sys.database_principals AS member_principal
           ON member_principal.principal_id = drm.member_principal_id
       WHERE role_principal.name = N'investment_portal_runtime'
         AND member_principal.name = N'InvestmentPortal_Connector'
   )
BEGIN
    ALTER ROLE [investment_portal_runtime]
        ADD MEMBER [InvestmentPortal_Connector];
END;
GO

IF DATABASE_PRINCIPAL_ID(N'mcp_connector') IS NOT NULL
BEGIN
    DENY SELECT, INSERT, UPDATE, DELETE ON OBJECT::invest.PortalUserEntitlements
        TO [mcp_connector];
    DENY EXECUTE ON OBJECT::invest.Portal_EnsureUser TO [mcp_connector];
    DENY EXECUTE ON OBJECT::invest.Portal_SetEntitlement TO [mcp_connector];
    DENY EXECUTE ON OBJECT::invest.Portal_GetPortfolios TO [mcp_connector];
    DENY EXECUTE ON OBJECT::invest.Portal_GetPortfolio TO [mcp_connector];
    DENY EXECUTE ON OBJECT::invest.Portal_GetMcpTokens TO [mcp_connector];
    DENY EXECUTE ON OBJECT::invest.Portal_CreateMcpToken TO [mcp_connector];
    DENY EXECUTE ON OBJECT::invest.Portal_RevokeMcpToken TO [mcp_connector];
END;
GO
