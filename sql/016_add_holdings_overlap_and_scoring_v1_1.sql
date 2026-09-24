/*
    Correct value-fund classification and add auditable fund-holdings overlap.

    Holdings are global market-reference data, not user-owned portfolio data.
    Provider exports are loaded through invest.ReplaceFundHoldingsSnapshot by a
    member of investment_data_loader. The MCP runtime receives read-only access.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

IF OBJECT_ID(N'invest.ReferenceInstrumentClassifications', N'U') IS NULL
    THROW 50001, 'Run migration 013 before migration 016.', 1;
IF OBJECT_ID(N'invest.ScoringModelVersions', N'U') IS NULL
    THROW 50002, 'Run migration 014 before migration 016.', 1;
GO

IF OBJECT_ID(N'invest.FundHoldingSnapshotSets', N'U') IS NULL
BEGIN
    CREATE TABLE invest.FundHoldingSnapshotSets
    (
        FundHoldingSnapshotSetId uniqueidentifier NOT NULL
            CONSTRAINT DF_FundHoldingSnapshotSets_Id DEFAULT NEWSEQUENTIALID(),
        FundSymbol varchar(50) NOT NULL,
        AsOfDate date NOT NULL,
        SourceName nvarchar(100) NOT NULL,
        SourceUrl nvarchar(1000) NULL,
        ReportedCoveragePercent decimal(7,4) NOT NULL,
        IsComplete bit NOT NULL
            CONSTRAINT DF_FundHoldingSnapshotSets_IsComplete DEFAULT (0),
        ImportedAt datetimeoffset(7) NOT NULL
            CONSTRAINT DF_FundHoldingSnapshotSets_ImportedAt
            DEFAULT SYSDATETIMEOFFSET(),

        CONSTRAINT PK_FundHoldingSnapshotSets
            PRIMARY KEY (FundHoldingSnapshotSetId),
        CONSTRAINT UX_FundHoldingSnapshotSets_Source
            UNIQUE (FundSymbol, AsOfDate, SourceName),
        CONSTRAINT CK_FundHoldingSnapshotSets_Symbol
            CHECK (FundSymbol = UPPER(FundSymbol)),
        CONSTRAINT CK_FundHoldingSnapshotSets_Coverage
            CHECK
            (
                ReportedCoveragePercent >= 0
                AND ReportedCoveragePercent <= 110
            )
    );
END;
GO

IF OBJECT_ID(N'invest.FundHoldingSnapshotItems', N'U') IS NULL
BEGIN
    CREATE TABLE invest.FundHoldingSnapshotItems
    (
        FundHoldingSnapshotSetId uniqueidentifier NOT NULL,
        HoldingKey varchar(100) NOT NULL,
        HoldingSymbol varchar(50) NULL,
        HoldingName nvarchar(500) NULL,
        WeightPercent decimal(9,6) NOT NULL,

        CONSTRAINT PK_FundHoldingSnapshotItems
            PRIMARY KEY (FundHoldingSnapshotSetId, HoldingKey),
        CONSTRAINT FK_FundHoldingSnapshotItems_Set
            FOREIGN KEY (FundHoldingSnapshotSetId)
            REFERENCES invest.FundHoldingSnapshotSets
                (FundHoldingSnapshotSetId)
            ON DELETE CASCADE,
        CONSTRAINT CK_FundHoldingSnapshotItems_Key
            CHECK (HoldingKey = UPPER(HoldingKey)),
        CONSTRAINT CK_FundHoldingSnapshotItems_Weight
            CHECK (WeightPercent > 0 AND WeightPercent <= 100)
    );
END;
GO

CREATE OR ALTER VIEW invest.McpLatestFundHoldings
AS
    WITH latest AS
    (
        SELECT
            snapshot_set.FundHoldingSnapshotSetId,
            snapshot_set.FundSymbol,
            snapshot_set.AsOfDate,
            snapshot_set.SourceName,
            snapshot_set.SourceUrl,
            snapshot_set.ReportedCoveragePercent,
            snapshot_set.IsComplete,
            snapshot_set.ImportedAt,
            ROW_NUMBER() OVER
            (
                PARTITION BY snapshot_set.FundSymbol
                ORDER BY
                    snapshot_set.AsOfDate DESC,
                    snapshot_set.IsComplete DESC,
                    snapshot_set.ReportedCoveragePercent DESC,
                    snapshot_set.ImportedAt DESC,
                    snapshot_set.FundHoldingSnapshotSetId DESC
            ) AS SnapshotRank
        FROM invest.FundHoldingSnapshotSets AS snapshot_set
    )
    SELECT
        latest.FundSymbol,
        item.HoldingKey,
        item.HoldingSymbol,
        item.HoldingName,
        item.WeightPercent,
        latest.AsOfDate,
        latest.SourceName,
        latest.SourceUrl,
        latest.ReportedCoveragePercent,
        latest.IsComplete,
        latest.ImportedAt
    FROM latest
    INNER JOIN invest.FundHoldingSnapshotItems AS item
        ON item.FundHoldingSnapshotSetId =
           latest.FundHoldingSnapshotSetId
    WHERE latest.SnapshotRank = 1;
GO

CREATE OR ALTER PROCEDURE invest.ReplaceFundHoldingsSnapshot
    @FundSymbol varchar(50),
    @AsOfDate date,
    @SourceName nvarchar(100),
    @HoldingsJson nvarchar(max),
    @SourceUrl nvarchar(1000) = NULL,
    @ReportedCoveragePercent decimal(7,4) = NULL,
    @IsComplete bit = 0
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    SET @FundSymbol = UPPER(LTRIM(RTRIM(@FundSymbol)));
    SET @SourceName = NULLIF(LTRIM(RTRIM(@SourceName)), N'');
    SET @SourceUrl = NULLIF(LTRIM(RTRIM(@SourceUrl)), N'');

    IF @FundSymbol = '' OR LEN(@FundSymbol) > 50
        THROW 50010, 'A valid fund symbol is required.', 1;
    IF @AsOfDate IS NULL OR @AsOfDate > CONVERT(date, SYSDATETIMEOFFSET())
        THROW 50011, 'A non-future holdings as-of date is required.', 1;
    IF @SourceName IS NULL
        THROW 50012, 'A holdings source name is required.', 1;
    IF ISJSON(@HoldingsJson) <> 1
        THROW 50013, 'HoldingsJson must be a JSON array.', 1;

    DECLARE @Parsed TABLE
    (
        HoldingKey varchar(100) NULL,
        HoldingSymbol varchar(50) NULL,
        HoldingName nvarchar(500) NULL,
        WeightPercent decimal(9,6) NOT NULL
    );

    INSERT INTO @Parsed
    (
        HoldingKey,
        HoldingSymbol,
        HoldingName,
        WeightPercent
    )
    SELECT
        UPPER
        (
            COALESCE
            (
                NULLIF(LTRIM(RTRIM(parsed.HoldingKey)), ''),
                CASE
                    WHEN NULLIF(LTRIM(RTRIM(parsed.HoldingSymbol)), '') IS NOT NULL
                        THEN CONCAT
                        (
                            'TICKER:',
                            NULLIF(LTRIM(RTRIM(parsed.HoldingSymbol)), '')
                        )
                END
            )
        ),
        UPPER(NULLIF(LTRIM(RTRIM(parsed.HoldingSymbol)), '')),
        NULLIF(LTRIM(RTRIM(parsed.HoldingName)), N''),
        parsed.WeightPercent
    FROM OPENJSON(@HoldingsJson)
    WITH
    (
        HoldingKey varchar(100) '$.holdingKey',
        HoldingSymbol varchar(50) '$.holdingSymbol',
        HoldingName nvarchar(500) '$.holdingName',
        WeightPercent decimal(9,6) '$.weightPercent'
    ) AS parsed
    WHERE parsed.WeightPercent IS NOT NULL;

    IF NOT EXISTS (SELECT 1 FROM @Parsed)
        THROW 50014, 'At least one valid holding is required.', 1;
    IF EXISTS
    (
        SELECT 1
        FROM @Parsed
        WHERE HoldingKey IS NULL OR HoldingKey = ''
           OR WeightPercent <= 0 OR WeightPercent > 100
    )
        THROW 50015, 'Every holding requires a key or ticker and a valid weight.', 1;
    IF EXISTS
    (
        SELECT HoldingKey
        FROM @Parsed
        GROUP BY HoldingKey
        HAVING COUNT(*) > 1
    )
        THROW 50016, 'HoldingsJson contains duplicate holding keys.', 1;

    DECLARE @CalculatedCoverage decimal(9,6);
    SELECT @CalculatedCoverage = SUM(WeightPercent) FROM @Parsed;
    IF @CalculatedCoverage > 110
        THROW 50017, 'Total holding weight exceeds 110 percent.', 1;
    SET @ReportedCoveragePercent = COALESCE
    (
        @ReportedCoveragePercent,
        CONVERT(decimal(7,4), @CalculatedCoverage)
    );
    IF @ReportedCoveragePercent < 0 OR @ReportedCoveragePercent > 110
        THROW 50018, 'Reported coverage must be between 0 and 110 percent.', 1;

    DECLARE @SnapshotSetId uniqueidentifier = NEWID();

    BEGIN TRANSACTION;

    DELETE FROM invest.FundHoldingSnapshotSets
    WHERE FundSymbol = @FundSymbol
      AND AsOfDate = @AsOfDate
      AND SourceName = @SourceName;

    INSERT INTO invest.FundHoldingSnapshotSets
    (
        FundHoldingSnapshotSetId,
        FundSymbol,
        AsOfDate,
        SourceName,
        SourceUrl,
        ReportedCoveragePercent,
        IsComplete
    )
    VALUES
    (
        @SnapshotSetId,
        @FundSymbol,
        @AsOfDate,
        @SourceName,
        @SourceUrl,
        @ReportedCoveragePercent,
        @IsComplete
    );

    INSERT INTO invest.FundHoldingSnapshotItems
    (
        FundHoldingSnapshotSetId,
        HoldingKey,
        HoldingSymbol,
        HoldingName,
        WeightPercent
    )
    SELECT
        @SnapshotSetId,
        HoldingKey,
        HoldingSymbol,
        HoldingName,
        WeightPercent
    FROM @Parsed;

    COMMIT TRANSACTION;

    SELECT
        @SnapshotSetId AS FundHoldingSnapshotSetId,
        @FundSymbol AS FundSymbol,
        @AsOfDate AS AsOfDate,
        @SourceName AS SourceName,
        COUNT(*) AS HoldingCount,
        @CalculatedCoverage AS CalculatedCoveragePercent,
        @ReportedCoveragePercent AS ReportedCoveragePercent,
        @IsComplete AS IsComplete
    FROM @Parsed;
END;
GO

CREATE OR ALTER FUNCTION invest.ResolveReferenceInstrumentClassification
(
    @Type nvarchar(100),
    @AssetType nvarchar(100),
    @AssetSubType nvarchar(100),
    @Symbol nvarchar(50),
    @Name nvarchar(500)
)
RETURNS @Classification TABLE
(
    CandidateClass varchar(50) NOT NULL,
    Archetype varchar(100) NOT NULL,
    ClassificationConfidence decimal(5,4) NOT NULL,
    ClassificationRuleVersion varchar(20) NOT NULL,
    NeedsReview bit NOT NULL
)
AS
BEGIN
    DECLARE @TypeText nvarchar(500) = UPPER(CONCAT
    (
        N' ', COALESCE(@Type, N''),
        N' ', COALESCE(@AssetType, N''),
        N' ', COALESCE(@AssetSubType, N''), N' '
    ));

    DECLARE @DescriptionText nvarchar(1000) = UPPER(CONCAT
    (
        N' ', COALESCE(@Symbol, N''),
        N' ', COALESCE(@Name, N''),
        N' ', COALESCE(@Type, N''),
        N' ', COALESCE(@AssetType, N''),
        N' ', COALESCE(@AssetSubType, N''), N' '
    ));
    DECLARE @NormalizedSymbol nvarchar(50) =
        UPPER(LTRIM(RTRIM(COALESCE(@Symbol, N''))));

    DECLARE @CandidateClass varchar(50) =
        CASE
            WHEN @DescriptionText LIKE N'%TARGET%RETIREMENT%'
              OR @DescriptionText LIKE N'%TARGET%DATE%'
                THEN 'MutualFund'
            WHEN @TypeText LIKE N'%MUTUAL%' THEN 'MutualFund'
            WHEN @TypeText LIKE N'%ETF%' THEN 'ETF'
            WHEN @TypeText LIKE N'%EQUITY%'
              OR @TypeText LIKE N'%STOCK%' THEN 'Stock'
            WHEN @TypeText LIKE N'%BOND%'
              OR @TypeText LIKE N'%FIXED INCOME%' THEN 'Bond'
            ELSE 'Other'
        END;

    DECLARE @Archetype varchar(100) =
        CASE
            WHEN @DescriptionText LIKE N'%TARGET%RETIREMENT%'
              OR @DescriptionText LIKE N'%TARGET%DATE%'
                THEN 'TargetDateMultiAsset'
            WHEN @DescriptionText LIKE N'%INTERNATIONAL%'
              OR @DescriptionText LIKE N'%EX-US%'
              OR @DescriptionText LIKE N'%FOREIGN%'
              OR @DescriptionText LIKE N'%EMERGING%'
                THEN 'InternationalEquity'
            WHEN @DescriptionText LIKE N'%BOND%'
              OR @DescriptionText LIKE N'%TREASURY%'
              OR @DescriptionText LIKE N'%CREDIT%'
              OR @DescriptionText LIKE N'%FIXED INCOME%'
              OR @DescriptionText LIKE N'%HIGH YIELD%'
                THEN 'FixedIncomeCredit'
            WHEN @DescriptionText LIKE N'%GOLD%'
              OR @DescriptionText LIKE N'%COMMOD%'
              OR @DescriptionText LIKE N'%REAL ESTATE%'
              OR @DescriptionText LIKE N'%REIT%'
                THEN 'DefensiveRealAssets'
            WHEN @DescriptionText LIKE N'%SMALL CAP%'
              OR @DescriptionText LIKE N'%SMALL-CAP%'
              OR @DescriptionText LIKE N'%MID CAP%'
              OR @DescriptionText LIKE N'%MID-CAP%'
                THEN 'SmallMidFactorEquity'
            WHEN @DescriptionText LIKE N'%TECHNOLOGY%'
              OR @DescriptionText LIKE N'%SEMICONDUCTOR%'
              OR @DescriptionText LIKE N'%HEALTH CARE%'
              OR @DescriptionText LIKE N'% ENERGY %'
              OR @DescriptionText LIKE N'% SECTOR %'
                THEN 'SectorEquity'
            WHEN @DescriptionText LIKE N'% GROWTH %'
                THEN 'GrowthEquityFund'
            WHEN @NormalizedSymbol IN (N'VTV', N'SCHV', N'IWD', N'IVE')
              OR
              (
                  @DescriptionText LIKE N'% VALUE %'
                  AND
                  (
                      @DescriptionText LIKE N'%LARGE CAP%'
                      OR @DescriptionText LIKE N'%LARGE-CAP%'
                  )
              )
                THEN 'LargeValueEquity'
            WHEN @DescriptionText LIKE N'% VALUE %'
                THEN 'ValueEquityFund'
            WHEN @DescriptionText LIKE N'%S&P 500%'
              OR @DescriptionText LIKE N'%TOTAL STOCK%'
              OR @DescriptionText LIKE N'%TOTAL MARKET%'
              OR @DescriptionText LIKE N'%BROAD MARKET%'
              OR @DescriptionText LIKE N'%LARGE CAP%'
                THEN 'BroadUSEquity'
            WHEN @TypeText LIKE N'%ETF%'
              OR @TypeText LIKE N'%MUTUAL%' THEN 'EquityFund'
            WHEN @TypeText LIKE N'%EQUITY%'
              OR @TypeText LIKE N'%STOCK%' THEN 'IndividualStock'
            ELSE 'Unclassified'
        END;

    DECLARE @ClassificationConfidence decimal(5,4) =
        CASE
            WHEN @DescriptionText LIKE N'%TARGET%RETIREMENT%'
              OR @DescriptionText LIKE N'%TARGET%DATE%'
                THEN CONVERT(decimal(5,4), 0.9500)
            WHEN @NormalizedSymbol IN (N'VTV', N'SCHV', N'IWD', N'IVE')
                THEN CONVERT(decimal(5,4), 0.9000)
            WHEN @TypeText LIKE N'%ETF%'
              OR @TypeText LIKE N'%MUTUAL%'
              OR @TypeText LIKE N'%EQUITY%'
              OR @TypeText LIKE N'%STOCK%'
              OR @TypeText LIKE N'%BOND%'
                THEN CONVERT(decimal(5,4), 0.7500)
            ELSE CONVERT(decimal(5,4), 0.2500)
        END;

    INSERT INTO @Classification
    (
        CandidateClass,
        Archetype,
        ClassificationConfidence,
        ClassificationRuleVersion,
        NeedsReview
    )
    VALUES
    (
        @CandidateClass,
        @Archetype,
        @ClassificationConfidence,
        '2',
        CONVERT(bit, CASE WHEN @Archetype = 'Unclassified' THEN 1 ELSE 0 END)
    );

    RETURN;
END;
GO

UPDATE classification
SET
    CandidateClass = resolved.CandidateClass,
    Archetype = resolved.Archetype,
    ClassificationConfidence = resolved.ClassificationConfidence,
    ClassificationRuleVersion = resolved.ClassificationRuleVersion,
    NeedsReview = resolved.NeedsReview,
    UpdatedAt = SYSDATETIMEOFFSET()
FROM invest.ReferenceInstrumentClassifications AS classification
INNER JOIN dbo.Series AS source
    ON source.SeriesId = classification.SeriesId
CROSS APPLY invest.ResolveReferenceInstrumentClassification
(
    source.[Type],
    source.AssetType,
    source.AssetSubType,
    source.Symbol,
    source.[Name]
) AS resolved
WHERE classification.ClassificationMethod = 'AUTO'
  AND source.Active = 1;
GO

CREATE OR ALTER PROCEDURE invest.SetReferenceInstrumentClassification
    @Symbol varchar(50),
    @CandidateClass varchar(50),
    @Archetype varchar(100),
    @InclusionReason nvarchar(500) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    SET @Symbol = UPPER(LTRIM(RTRIM(@Symbol)));
    SET @CandidateClass = LTRIM(RTRIM(@CandidateClass));
    SET @Archetype = LTRIM(RTRIM(@Archetype));
    IF @Symbol = '' OR @CandidateClass = '' OR @Archetype = ''
        THROW 50019, 'Symbol, candidate class, and archetype are required.', 1;

    DECLARE @SeriesId int;
    SELECT TOP (1) @SeriesId = SeriesId
    FROM dbo.Series
    WHERE UPPER(LTRIM(RTRIM(Symbol))) = @Symbol
      AND Active = 1
    ORDER BY SeriesId;

    IF @SeriesId IS NULL
        THROW 50020, 'Active reference instrument was not found.', 1;

    BEGIN TRANSACTION;

    UPDATE invest.ReferenceInstrumentClassifications WITH (UPDLOCK, HOLDLOCK)
    SET
        CandidateClass = @CandidateClass,
        Archetype = @Archetype,
        ClassificationMethod = 'MANUAL',
        ClassificationConfidence = 1.0000,
        ClassificationRuleVersion = '2',
        NeedsReview = 0,
        InclusionReason = NULLIF(LTRIM(RTRIM(@InclusionReason)), N''),
        UpdatedAt = SYSDATETIMEOFFSET()
    WHERE SeriesId = @SeriesId;

    IF @@ROWCOUNT = 0
    BEGIN
        INSERT INTO invest.ReferenceInstrumentClassifications
        (
            SeriesId,
            CandidateClass,
            Archetype,
            ClassificationMethod,
            ClassificationConfidence,
            ClassificationRuleVersion,
            NeedsReview,
            InclusionReason
        )
        VALUES
        (
            @SeriesId,
            @CandidateClass,
            @Archetype,
            'MANUAL',
            1.0000,
            '2',
            0,
            NULLIF(LTRIM(RTRIM(@InclusionReason)), N'')
        );
    END;

    COMMIT TRANSACTION;

    SELECT *
    FROM invest.ReferenceInstrumentClassifications
    WHERE SeriesId = @SeriesId;
END;
GO

IF NOT EXISTS
(
    SELECT 1
    FROM invest.ScoringModelVersions
    WHERE ModelVersion = '1.1'
)
BEGIN
    INSERT INTO invest.ScoringModelVersions
    (
        ModelVersion,
        [Description],
        BenchmarkSymbol,
        WeightsJson,
        NormalizationJson,
        ClassificationRuleVersion,
        EffectiveAt,
        IsActive
    )
    VALUES
    (
        '1.1',
        N'Transparent scoring with corrected value classification, fund-overlap, and return-correlation portfolio fit.',
        'VOO',
        N'{"quality":35,"valuation":20,"trend":15,"portfolioFit":20,"referenceSimilarity":10}',
        N'{"method":"peer_percentile","outliers":"bounded_by_percentile","minimumPeerCount":5,"missingValue":"neutral_with_completeness_penalty","portfolioFit":{"holdingsOverlap":true,"returnCorrelation":true,"missingFundHoldingsCap":60,"holdingsMaxAgeDays":45,"minimumCorrelationObservations":60}}',
        '2',
        SYSDATETIMEOFFSET(),
        1
    );
END;

UPDATE invest.ScoringModelVersions
SET IsActive = CASE WHEN ModelVersion = '1.1' THEN 1 ELSE 0 END;
GO

IF DATABASE_PRINCIPAL_ID(N'investment_data_loader') IS NULL
    EXEC(N'CREATE ROLE investment_data_loader AUTHORIZATION dbo;');
GO

GRANT EXECUTE ON OBJECT::invest.ReplaceFundHoldingsSnapshot
    TO investment_data_loader;
GRANT SELECT ON OBJECT::invest.McpLatestFundHoldings
    TO investment_data_loader;

IF DATABASE_PRINCIPAL_ID(N'mcp_connector') IS NOT NULL
BEGIN
    GRANT SELECT ON OBJECT::invest.McpLatestFundHoldings
        TO [mcp_connector];
    DENY INSERT, UPDATE, DELETE
        ON OBJECT::invest.FundHoldingSnapshotSets
        TO [mcp_connector];
    DENY INSERT, UPDATE, DELETE
        ON OBJECT::invest.FundHoldingSnapshotItems
        TO [mcp_connector];
END;
GO

SELECT
    ModelVersion,
    ClassificationRuleVersion,
    IsActive,
    EffectiveAt
FROM invest.ScoringModelVersions
WHERE ModelVersion IN ('1.0', '1.1')
ORDER BY ModelVersion;

SELECT
    source.Symbol,
    classification.CandidateClass,
    classification.Archetype,
    classification.ClassificationMethod,
    classification.ClassificationRuleVersion
FROM dbo.Series AS source
INNER JOIN invest.ReferenceInstrumentClassifications AS classification
    ON classification.SeriesId = source.SeriesId
WHERE UPPER(source.Symbol) IN ('VTV', 'VBR', 'AVUV')
ORDER BY source.Symbol;
GO
