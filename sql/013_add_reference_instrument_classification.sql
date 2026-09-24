/*
    Global reference-instrument classification for investment scoring.

    dbo.Series remains the single global curated instrument catalog.  This
    migration adds scoring-only metadata, backfills current active rows, and
    classifies future dbo.Series inserts/updates without changing the existing
    symbol-management application.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

IF SCHEMA_ID(N'invest') IS NULL
    EXEC(N'CREATE SCHEMA invest AUTHORIZATION dbo;');
GO

IF OBJECT_ID(N'dbo.Series', N'U') IS NULL
    THROW 50001, 'Required table dbo.Series does not exist.', 1;
GO

IF OBJECT_ID(N'invest.ReferenceInstrumentClassifications', N'U') IS NULL
BEGIN
    CREATE TABLE invest.ReferenceInstrumentClassifications
    (
        SeriesId int NOT NULL,
        CandidateClass varchar(50) NOT NULL,
        Archetype varchar(100) NOT NULL,
        ClassificationMethod varchar(10) NOT NULL
            CONSTRAINT DF_ReferenceClassification_Method DEFAULT ('AUTO'),
        ClassificationConfidence decimal(5,4) NOT NULL,
        ClassificationRuleVersion varchar(20) NOT NULL,
        NeedsReview bit NOT NULL
            CONSTRAINT DF_ReferenceClassification_NeedsReview DEFAULT (0),
        InclusionReason nvarchar(500) NULL,
        CreatedAt datetimeoffset(7) NOT NULL
            CONSTRAINT DF_ReferenceClassification_CreatedAt
            DEFAULT SYSDATETIMEOFFSET(),
        UpdatedAt datetimeoffset(7) NOT NULL
            CONSTRAINT DF_ReferenceClassification_UpdatedAt
            DEFAULT SYSDATETIMEOFFSET(),
        RowVersion rowversion NOT NULL,

        CONSTRAINT PK_ReferenceInstrumentClassifications
            PRIMARY KEY (SeriesId),
        CONSTRAINT FK_ReferenceInstrumentClassifications_Series
            FOREIGN KEY (SeriesId) REFERENCES dbo.Series (SeriesId),
        CONSTRAINT CK_ReferenceClassification_Method
            CHECK (ClassificationMethod IN ('AUTO', 'MANUAL')),
        CONSTRAINT CK_ReferenceClassification_Confidence
            CHECK (ClassificationConfidence >= 0 AND ClassificationConfidence <= 1)
    );
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
    DECLARE @TypeText nvarchar(500) = UPPER(CONCAT(
        N' ', COALESCE(@Type, N''),
        N' ', COALESCE(@AssetType, N''),
        N' ', COALESCE(@AssetSubType, N''), N' '
    ));

    DECLARE @DescriptionText nvarchar(1000) = UPPER(CONCAT(
        N' ', COALESCE(@Symbol, N''),
        N' ', COALESCE(@Name, N''),
        N' ', COALESCE(@Type, N''),
        N' ', COALESCE(@AssetType, N''),
        N' ', COALESCE(@AssetSubType, N''), N' '
    ));

    DECLARE @CandidateClass varchar(50) =
        CASE
            WHEN @DescriptionText LIKE N'%TARGET%RETIREMENT%'
              OR @DescriptionText LIKE N'%TARGET%DATE%'
                THEN 'MutualFund'
            WHEN @TypeText LIKE N'%MUTUAL%'
                THEN 'MutualFund'
            WHEN @TypeText LIKE N'%ETF%'
                THEN 'ETF'
            WHEN @TypeText LIKE N'%EQUITY%'
              OR @TypeText LIKE N'%STOCK%'
                THEN 'Stock'
            WHEN @TypeText LIKE N'%BOND%'
              OR @TypeText LIKE N'%FIXED INCOME%'
                THEN 'Bond'
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
              OR @DescriptionText LIKE N'% VALUE %'
                THEN 'SmallMidFactorEquity'
            WHEN @DescriptionText LIKE N'%TECHNOLOGY%'
              OR @DescriptionText LIKE N'%SEMICONDUCTOR%'
              OR @DescriptionText LIKE N'%HEALTH CARE%'
              OR @DescriptionText LIKE N'% ENERGY %'
              OR @DescriptionText LIKE N'% SECTOR %'
                THEN 'SectorEquity'
            WHEN @DescriptionText LIKE N'% GROWTH %'
                THEN 'GrowthEquityFund'
            WHEN @DescriptionText LIKE N'%S&P 500%'
              OR @DescriptionText LIKE N'%TOTAL STOCK%'
              OR @DescriptionText LIKE N'%TOTAL MARKET%'
              OR @DescriptionText LIKE N'%BROAD MARKET%'
              OR @DescriptionText LIKE N'%LARGE CAP%'
                THEN 'BroadUSEquity'
            WHEN @TypeText LIKE N'%ETF%'
              OR @TypeText LIKE N'%MUTUAL%'
                THEN 'EquityFund'
            WHEN @TypeText LIKE N'%EQUITY%'
              OR @TypeText LIKE N'%STOCK%'
                THEN 'IndividualStock'
            ELSE 'Unclassified'
        END;

    DECLARE @ClassificationConfidence decimal(5,4) =
        CASE
            WHEN @DescriptionText LIKE N'%TARGET%RETIREMENT%'
              OR @DescriptionText LIKE N'%TARGET%DATE%'
                THEN CONVERT(decimal(5,4), 0.9500)
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
        '1',
        CONVERT(bit, CASE WHEN @Archetype = 'Unclassified' THEN 1 ELSE 0 END)
    );

    RETURN;
END;
GO

BEGIN TRY
    BEGIN TRANSACTION;

    UPDATE target
    SET
        CandidateClass = resolved.CandidateClass,
        Archetype = resolved.Archetype,
        ClassificationConfidence = resolved.ClassificationConfidence,
        ClassificationRuleVersion = resolved.ClassificationRuleVersion,
        NeedsReview = resolved.NeedsReview,
        UpdatedAt = SYSDATETIMEOFFSET()
    FROM invest.ReferenceInstrumentClassifications AS target
    INNER JOIN dbo.Series AS source
        ON source.SeriesId = target.SeriesId
    CROSS APPLY invest.ResolveReferenceInstrumentClassification
    (
        source.[Type], source.AssetType, source.AssetSubType,
        source.Symbol, source.[Name]
    ) AS resolved
    WHERE source.Active = 1
      AND target.ClassificationMethod = 'AUTO';

    INSERT INTO invest.ReferenceInstrumentClassifications
    (
        SeriesId,
        CandidateClass,
        Archetype,
        ClassificationMethod,
        ClassificationConfidence,
        ClassificationRuleVersion,
        NeedsReview
    )
    SELECT
        source.SeriesId,
        resolved.CandidateClass,
        resolved.Archetype,
        'AUTO',
        resolved.ClassificationConfidence,
        resolved.ClassificationRuleVersion,
        resolved.NeedsReview
    FROM dbo.Series AS source
    CROSS APPLY invest.ResolveReferenceInstrumentClassification
    (
        source.[Type], source.AssetType, source.AssetSubType,
        source.Symbol, source.[Name]
    ) AS resolved
    WHERE source.Active = 1
      AND UPPER(LTRIM(RTRIM(source.Symbol))) NOT IN ('TEMP', 'PORTF')
      AND NOT EXISTS
      (
          SELECT 1
          FROM invest.ReferenceInstrumentClassifications AS existing
          WHERE existing.SeriesId = source.SeriesId
      );

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0
        ROLLBACK TRANSACTION;
    THROW;
END CATCH;
GO

CREATE OR ALTER TRIGGER dbo.TR_Series_AutoClassify
ON dbo.Series
WITH EXECUTE AS OWNER
AFTER INSERT, UPDATE
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    UPDATE target
    SET
        CandidateClass = resolved.CandidateClass,
        Archetype = resolved.Archetype,
        ClassificationConfidence = resolved.ClassificationConfidence,
        ClassificationRuleVersion = resolved.ClassificationRuleVersion,
        NeedsReview = resolved.NeedsReview,
        UpdatedAt = SYSDATETIMEOFFSET()
    FROM invest.ReferenceInstrumentClassifications AS target
    INNER JOIN inserted AS source
        ON source.SeriesId = target.SeriesId
    CROSS APPLY invest.ResolveReferenceInstrumentClassification
    (
        source.[Type], source.AssetType, source.AssetSubType,
        source.Symbol, source.[Name]
    ) AS resolved
    WHERE source.Active = 1
      AND target.ClassificationMethod = 'AUTO';

    INSERT INTO invest.ReferenceInstrumentClassifications
    (
        SeriesId,
        CandidateClass,
        Archetype,
        ClassificationMethod,
        ClassificationConfidence,
        ClassificationRuleVersion,
        NeedsReview
    )
    SELECT
        source.SeriesId,
        resolved.CandidateClass,
        resolved.Archetype,
        'AUTO',
        resolved.ClassificationConfidence,
        resolved.ClassificationRuleVersion,
        resolved.NeedsReview
    FROM inserted AS source
    CROSS APPLY invest.ResolveReferenceInstrumentClassification
    (
        source.[Type], source.AssetType, source.AssetSubType,
        source.Symbol, source.[Name]
    ) AS resolved
    WHERE source.Active = 1
      AND UPPER(LTRIM(RTRIM(source.Symbol))) NOT IN ('TEMP', 'PORTF')
      AND NOT EXISTS
      (
          SELECT 1
          FROM invest.ReferenceInstrumentClassifications AS existing
          WHERE existing.SeriesId = source.SeriesId
      );
END;
GO

CREATE OR ALTER VIEW invest.McpScoringReferenceInstruments
AS
    SELECT
        instrument.SeriesId,
        instrument.Symbol,
        instrument.[Name],
        instrument.[Type],
        instrument.PE,
        instrument.Volatility,
        instrument.[Yield],
        instrument.EPS,
        instrument.DivAmount,
        instrument.Exchange,
        instrument.AssetType,
        instrument.AssetSubType,
        classification.CandidateClass,
        classification.Archetype,
        classification.ClassificationMethod,
        classification.ClassificationConfidence,
        classification.ClassificationRuleVersion,
        classification.NeedsReview,
        classification.InclusionReason,
        classification.UpdatedAt AS ClassificationUpdatedAt
    FROM invest.McpInstruments AS instrument
    INNER JOIN invest.ReferenceInstrumentClassifications AS classification
        ON classification.SeriesId = instrument.SeriesId;
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
        THROW 50010, 'Symbol, candidate class, and archetype are required.', 1;

    DECLARE @SeriesId int;
    SELECT TOP (1) @SeriesId = SeriesId
    FROM dbo.Series
    WHERE UPPER(LTRIM(RTRIM(Symbol))) = @Symbol
      AND Active = 1
    ORDER BY SeriesId;

    IF @SeriesId IS NULL
        THROW 50011, 'Active reference instrument was not found.', 1;

    BEGIN TRANSACTION;

    UPDATE invest.ReferenceInstrumentClassifications WITH (UPDLOCK, HOLDLOCK)
    SET
        CandidateClass = @CandidateClass,
        Archetype = @Archetype,
        ClassificationMethod = 'MANUAL',
        ClassificationConfidence = 1.0000,
        NeedsReview = 0,
        InclusionReason = NULLIF(LTRIM(RTRIM(@InclusionReason)), N''),
        UpdatedAt = SYSDATETIMEOFFSET()
    WHERE SeriesId = @SeriesId;

    IF @@ROWCOUNT = 0
    BEGIN
        INSERT INTO invest.ReferenceInstrumentClassifications
        (
            SeriesId, CandidateClass, Archetype, ClassificationMethod,
            ClassificationConfidence, ClassificationRuleVersion,
            NeedsReview, InclusionReason
        )
        VALUES
        (
            @SeriesId, @CandidateClass, @Archetype, 'MANUAL',
            1.0000, '1', 0,
            NULLIF(LTRIM(RTRIM(@InclusionReason)), N'')
        );
    END;

    COMMIT TRANSACTION;

    SELECT *
    FROM invest.ReferenceInstrumentClassifications
    WHERE SeriesId = @SeriesId;
END;
GO

SELECT
    CandidateClass,
    Archetype,
    NeedsReview,
    COUNT(*) AS InstrumentCount
FROM invest.ReferenceInstrumentClassifications
GROUP BY CandidateClass, Archetype, NeedsReview
ORDER BY CandidateClass, Archetype, NeedsReview;
GO

SELECT
    source.SeriesId,
    source.Symbol,
    source.[Name],
    source.[Type],
    source.AssetType,
    source.AssetSubType,
    classification.CandidateClass,
    classification.Archetype,
    classification.ClassificationConfidence
FROM dbo.Series AS source
INNER JOIN invest.ReferenceInstrumentClassifications AS classification
    ON classification.SeriesId = source.SeriesId
WHERE source.Active = 1
  AND classification.NeedsReview = 1
ORDER BY source.Symbol;
GO
