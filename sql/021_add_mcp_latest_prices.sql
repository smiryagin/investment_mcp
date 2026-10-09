/*
    Expose one latest stored market price per instrument symbol to the MCP.

    The runtime remains denied direct access to dbo.Series and dbo.SeriesData.
    This view relies on same-owner SQL Server ownership chaining and exposes only
    the minimal instrument and valuation fields needed by read-only MCP tools.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

IF OBJECT_ID(N'invest.McpInstruments', N'V') IS NULL
    THROW 50170, 'Run sql/009_create_mcp_instruments_view.sql first.', 1;
GO

CREATE OR ALTER VIEW invest.McpLatestPrices
AS
    WITH RankedInstruments AS
    (
        SELECT
            instrument.SeriesId,
            instrument.Symbol,
            instrument.[Name],
            instrument.AssetType,
            instrument.AssetSubType,
            instrument.Exchange,
            ROW_NUMBER() OVER
            (
                PARTITION BY UPPER(LTRIM(RTRIM(instrument.Symbol)))
                ORDER BY instrument.SeriesId
            ) AS SymbolRank
        FROM invest.McpInstruments AS instrument
    )
    SELECT
        instrument.SeriesId,
        instrument.Symbol,
        instrument.[Name],
        instrument.AssetType,
        instrument.AssetSubType,
        instrument.Exchange,
        latest.LastValue AS LastPrice,
        latest.[Date] AS PriceDate,
        latest.Updated AS PriceUpdatedAt
    FROM RankedInstruments AS instrument
    OUTER APPLY
    (
        SELECT TOP (1)
            data.LastValue,
            data.[Date],
            data.Updated
        FROM dbo.SeriesData AS data
        WHERE data.SeriesId = instrument.SeriesId
        ORDER BY data.[Date] DESC, data.Updated DESC
    ) AS latest
    WHERE instrument.SymbolRank = 1;
GO

IF DATABASE_PRINCIPAL_ID(N'mcp_connector') IS NULL
    THROW 50171, 'Database user [mcp_connector] does not exist.', 1;
GO

BEGIN TRY
    BEGIN TRANSACTION;

    DENY SELECT ON OBJECT::dbo.SeriesData TO [mcp_connector];
    GRANT SELECT ON OBJECT::invest.McpLatestPrices TO [mcp_connector];

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0
        ROLLBACK TRANSACTION;
    THROW;
END CATCH;
GO
