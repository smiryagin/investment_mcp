CREATE OR ALTER PROCEDURE dbo.McpGetResearch
    @Symbol varchar(50) = NULL,
    @AssetType varchar(50) = NULL,
    @WatchedOnly bit = 0,
    @TradedOnly bit = 0,
    @Top int = 200
AS
BEGIN
    SET NOCOUNT ON;

    IF @Top IS NULL OR @Top < 1 SET @Top = 200;
    IF @Top > 2500 SET @Top = 2500;

    WITH ranked AS (
        SELECT
            Symbol,
            [Date],
            TradePrice,
            LastValue,
            [%DayChng],
            [%DayChng-1],
            [%DayChng-2],
            [%ToMaxChng],
            [%ToSplineMaxChng],
            [%Chng],
            [%50Chng],
            [%200Chng],
            [%50/200],
            [%50/200t],
            [%50/200w],
            [%50/200w2],
            Created,
            Updated,
            IsWatched,
            IsTraded,
            StatusId,
            [Name],
            PE,
            [Volatility],
            [Yield],
            [_52WkHigh],
            [_52WkLow],
            M1,
            M3,
            YTD,
            Y1,
            Y3,
            Y5,
            Liq,
            KeepMonths,
            RANK() OVER (ORDER BY (M3_number + Y1_number + Y3_number)) AS [Rank],
            EPS,
            DivAmount,
            Exchange,
            AssetType,
            AssetSubType,
            AnnualizedVolatility,
            SharpeRatio,
            AnnualizedReturn,
            MaximumDrawdown
        FROM dbo.TradeDaysChange()
        WHERE (@Symbol IS NULL OR Symbol = @Symbol)
          AND (@AssetType IS NULL OR AssetType = @AssetType)
          AND (@WatchedOnly = 0 OR IsWatched = 1)
          AND (@TradedOnly = 0 OR IsTraded = 1)
    )
    SELECT TOP (@Top) *
    FROM ranked
    ORDER BY [Rank], Symbol;
END;
