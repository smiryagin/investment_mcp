/* Least-privilege runtime grants for global scoring and private portfolio fit. */

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

IF DATABASE_PRINCIPAL_ID(N'mcp_connector') IS NULL
    THROW 50001, 'Database user [mcp_connector] does not exist.', 1;
GO

BEGIN TRY
    BEGIN TRANSACTION;

    GRANT SELECT ON OBJECT::invest.McpScoringReferenceInstruments
        TO [mcp_connector];
    GRANT SELECT ON OBJECT::invest.ScoringModelVersions
        TO [mcp_connector];
    GRANT SELECT ON OBJECT::invest.InstrumentFeatureSnapshots
        TO [mcp_connector];
    GRANT SELECT ON OBJECT::invest.InstrumentScoreSnapshots
        TO [mcp_connector];
    GRANT SELECT ON OBJECT::invest.PortfolioCandidateScoreSnapshots
        TO [mcp_connector];

    GRANT EXECUTE ON OBJECT::invest.UpsertInstrumentFeatureSnapshot
        TO [mcp_connector];
    GRANT EXECUTE ON OBJECT::invest.UpsertInstrumentScoreSnapshot
        TO [mcp_connector];
    GRANT EXECUTE ON OBJECT::invest.UpsertPortfolioCandidateScoreSnapshot
        TO [mcp_connector];

    DENY INSERT, UPDATE, DELETE
        ON OBJECT::invest.ReferenceInstrumentClassifications
        TO [mcp_connector];
    DENY INSERT, UPDATE, DELETE
        ON OBJECT::invest.ScoringModelVersions
        TO [mcp_connector];
    DENY INSERT, UPDATE, DELETE
        ON OBJECT::invest.InstrumentFeatureSnapshots
        TO [mcp_connector];
    DENY INSERT, UPDATE, DELETE
        ON OBJECT::invest.InstrumentScoreSnapshots
        TO [mcp_connector];
    DENY INSERT, UPDATE, DELETE
        ON OBJECT::invest.PortfolioCandidateScoreSnapshots
        TO [mcp_connector];

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0
        ROLLBACK TRANSACTION;
    THROW;
END CATCH;
GO
