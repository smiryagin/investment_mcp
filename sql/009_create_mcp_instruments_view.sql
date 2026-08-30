/*
    Least-privilege instrument catalog for the Investment MCP runtime.

    The MCP reads only the columns it needs and does not see internal
    calculation series such as TEMP and PORTF. Active status, watch/trade flags,
    fallback prices, data-range dates, and alternate symbols are intentionally
    not exposed through the view.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

CREATE OR ALTER VIEW invest.McpInstruments
AS
    SELECT
        SeriesId,
        Symbol,
        [Name],
        [Type],
        PE,
        Volatility,
        [Yield],
        EPS,
        DivAmount,
        Exchange,
        AssetType,
        AssetSubType
    FROM dbo.Series
    WHERE Active = 1
      AND UPPER(LTRIM(RTRIM(Symbol))) NOT IN ('TEMP', 'PORTF');
GO

IF DATABASE_PRINCIPAL_ID(N'mcp_connector') IS NULL
    THROW 50001, 'Database user [mcp_connector] does not exist.', 1;
GO

/*
    DENY also protects deployments where mcp_connector inherited table access
    through a broad database role. The view continues to work through SQL
    Server ownership chaining because dbo owns both schemas.
*/
BEGIN TRY
    BEGIN TRANSACTION;

    REVOKE SELECT ON OBJECT::dbo.Series TO [mcp_connector];
    DENY SELECT ON OBJECT::dbo.Series TO [mcp_connector];
    GRANT SELECT ON OBJECT::invest.McpInstruments TO [mcp_connector];

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0
        ROLLBACK TRANSACTION;
    THROW;
END CATCH;
GO
