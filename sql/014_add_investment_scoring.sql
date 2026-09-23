/*
    Versioned, transparent investment scoring storage.

    Global features and standalone scores are shared across authenticated MCP
    users.  Portfolio-fit snapshots remain scoped to an owned user/account pair.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

IF OBJECT_ID(N'invest.ScoringModelVersions', N'U') IS NULL
BEGIN
    CREATE TABLE invest.ScoringModelVersions
    (
        ModelVersion varchar(50) NOT NULL,
        [Description] nvarchar(500) NOT NULL,
        BenchmarkSymbol varchar(50) NOT NULL,
        WeightsJson nvarchar(max) NOT NULL,
        NormalizationJson nvarchar(max) NOT NULL,
        ClassificationRuleVersion varchar(20) NOT NULL,
        EffectiveAt datetimeoffset(7) NOT NULL,
        IsActive bit NOT NULL
            CONSTRAINT DF_ScoringModelVersions_IsActive DEFAULT (1),
        CreatedAt datetimeoffset(7) NOT NULL
            CONSTRAINT DF_ScoringModelVersions_CreatedAt
            DEFAULT SYSDATETIMEOFFSET(),

        CONSTRAINT PK_ScoringModelVersions PRIMARY KEY (ModelVersion),
        CONSTRAINT CK_ScoringModelVersions_WeightsJson
            CHECK (ISJSON(WeightsJson) = 1),
        CONSTRAINT CK_ScoringModelVersions_NormalizationJson
            CHECK (ISJSON(NormalizationJson) = 1)
    );
END;
GO

IF NOT EXISTS
(
    SELECT 1
    FROM invest.ScoringModelVersions
    WHERE ModelVersion = '1.0'
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
        '1.0',
        N'Transparent percentile scoring with class/archetype peer comparison.',
        'VOO',
        N'{"quality":35,"valuation":20,"trend":15,"portfolioFit":20,"referenceSimilarity":10}',
        N'{"method":"peer_percentile","outliers":"bounded_by_percentile","minimumPeerCount":5,"missingValue":"neutral_with_completeness_penalty"}',
        '1',
        SYSDATETIMEOFFSET(),
        1
    );
END;
GO

IF OBJECT_ID(N'invest.InstrumentFeatureSnapshots', N'U') IS NULL
BEGIN
    CREATE TABLE invest.InstrumentFeatureSnapshots
    (
        FeatureSnapshotId uniqueidentifier NOT NULL
            CONSTRAINT DF_InstrumentFeatureSnapshots_Id
            DEFAULT NEWSEQUENTIALID(),
        SeriesId int NULL,
        Symbol varchar(50) NOT NULL,
        CandidateClass varchar(50) NOT NULL,
        Archetype varchar(100) NOT NULL,
        FeatureAsOf date NOT NULL,
        PriceFeaturesJson nvarchar(max) NOT NULL,
        FundamentalFeaturesJson nvarchar(max) NOT NULL,
        ExposureFeaturesJson nvarchar(max) NOT NULL,
        DataSourcesJson nvarchar(max) NOT NULL,
        DataCompletenessScore decimal(5,2) NOT NULL,
        ModelVersion varchar(50) NOT NULL,
        CreatedAt datetimeoffset(7) NOT NULL
            CONSTRAINT DF_InstrumentFeatureSnapshots_CreatedAt
            DEFAULT SYSDATETIMEOFFSET(),
        UpdatedAt datetimeoffset(7) NOT NULL
            CONSTRAINT DF_InstrumentFeatureSnapshots_UpdatedAt
            DEFAULT SYSDATETIMEOFFSET(),

        CONSTRAINT PK_InstrumentFeatureSnapshots PRIMARY KEY (FeatureSnapshotId),
        CONSTRAINT FK_InstrumentFeatureSnapshots_Series
            FOREIGN KEY (SeriesId) REFERENCES dbo.Series (SeriesId),
        CONSTRAINT FK_InstrumentFeatureSnapshots_Model
            FOREIGN KEY (ModelVersion)
            REFERENCES invest.ScoringModelVersions (ModelVersion),
        CONSTRAINT CK_InstrumentFeatureSnapshots_Symbol
            CHECK (Symbol = UPPER(Symbol)),
        CONSTRAINT CK_InstrumentFeatureSnapshots_Completeness
            CHECK (DataCompletenessScore >= 0 AND DataCompletenessScore <= 100),
        CONSTRAINT CK_InstrumentFeatureSnapshots_PriceJson
            CHECK (ISJSON(PriceFeaturesJson) = 1),
        CONSTRAINT CK_InstrumentFeatureSnapshots_FundamentalJson
            CHECK (ISJSON(FundamentalFeaturesJson) = 1),
        CONSTRAINT CK_InstrumentFeatureSnapshots_ExposureJson
            CHECK (ISJSON(ExposureFeaturesJson) = 1),
        CONSTRAINT CK_InstrumentFeatureSnapshots_SourcesJson
            CHECK (ISJSON(DataSourcesJson) = 1)
    );

    CREATE UNIQUE INDEX UX_InstrumentFeatureSnapshots_Symbol_Date_Model
        ON invest.InstrumentFeatureSnapshots (Symbol, FeatureAsOf, ModelVersion);
END;
GO

IF NOT EXISTS
(
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID(N'invest.InstrumentFeatureSnapshots')
      AND name = N'UX_InstrumentFeatureSnapshots_Symbol_Date_Model'
)
    CREATE UNIQUE INDEX UX_InstrumentFeatureSnapshots_Symbol_Date_Model
        ON invest.InstrumentFeatureSnapshots (Symbol, FeatureAsOf, ModelVersion);
GO

IF OBJECT_ID(N'invest.InstrumentScoreSnapshots', N'U') IS NULL
BEGIN
    CREATE TABLE invest.InstrumentScoreSnapshots
    (
        InstrumentScoreSnapshotId uniqueidentifier NOT NULL
            CONSTRAINT DF_InstrumentScoreSnapshots_Id
            DEFAULT NEWSEQUENTIALID(),
        FeatureSnapshotId uniqueidentifier NULL,
        Symbol varchar(50) NOT NULL,
        CandidateClass varchar(50) NOT NULL,
        Archetype varchar(100) NOT NULL,
        QualityScore decimal(5,2) NOT NULL,
        ValuationScore decimal(5,2) NOT NULL,
        GrowthScore decimal(5,2) NOT NULL,
        TrendScore decimal(5,2) NOT NULL,
        RiskScore decimal(5,2) NOT NULL,
        LiquidityCostScore decimal(5,2) NOT NULL,
        InvestmentQualityScore decimal(5,2) NOT NULL,
        TechnicalOpportunityScore decimal(5,2) NOT NULL,
        ReferenceSimilarityScore decimal(5,2) NULL,
        StandaloneCandidateScore decimal(5,2) NOT NULL,
        DataCompletenessScore decimal(5,2) NOT NULL,
        PeerGroupLevel varchar(30) NOT NULL,
        PeerCount int NOT NULL,
        ClosestPeersJson nvarchar(max) NOT NULL,
        StrengthsJson nvarchar(max) NOT NULL,
        ConcernsJson nvarchar(max) NOT NULL,
        MissingFeaturesJson nvarchar(max) NOT NULL,
        ScoreAsOf date NOT NULL,
        ModelVersion varchar(50) NOT NULL,
        CreatedAt datetimeoffset(7) NOT NULL
            CONSTRAINT DF_InstrumentScoreSnapshots_CreatedAt
            DEFAULT SYSDATETIMEOFFSET(),
        UpdatedAt datetimeoffset(7) NOT NULL
            CONSTRAINT DF_InstrumentScoreSnapshots_UpdatedAt
            DEFAULT SYSDATETIMEOFFSET(),

        CONSTRAINT PK_InstrumentScoreSnapshots
            PRIMARY KEY (InstrumentScoreSnapshotId),
        CONSTRAINT FK_InstrumentScoreSnapshots_Feature
            FOREIGN KEY (FeatureSnapshotId)
            REFERENCES invest.InstrumentFeatureSnapshots (FeatureSnapshotId),
        CONSTRAINT FK_InstrumentScoreSnapshots_Model
            FOREIGN KEY (ModelVersion)
            REFERENCES invest.ScoringModelVersions (ModelVersion),
        CONSTRAINT CK_InstrumentScoreSnapshots_Symbol
            CHECK (Symbol = UPPER(Symbol)),
        CONSTRAINT CK_InstrumentScoreSnapshots_PeersJson
            CHECK (ISJSON(ClosestPeersJson) = 1),
        CONSTRAINT CK_InstrumentScoreSnapshots_StrengthsJson
            CHECK (ISJSON(StrengthsJson) = 1),
        CONSTRAINT CK_InstrumentScoreSnapshots_ConcernsJson
            CHECK (ISJSON(ConcernsJson) = 1),
        CONSTRAINT CK_InstrumentScoreSnapshots_MissingJson
            CHECK (ISJSON(MissingFeaturesJson) = 1)
    );

    CREATE UNIQUE INDEX UX_InstrumentScoreSnapshots_Symbol_Date_Model
        ON invest.InstrumentScoreSnapshots (Symbol, ScoreAsOf, ModelVersion);
END;
GO

IF NOT EXISTS
(
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID(N'invest.InstrumentScoreSnapshots')
      AND name = N'UX_InstrumentScoreSnapshots_Symbol_Date_Model'
)
    CREATE UNIQUE INDEX UX_InstrumentScoreSnapshots_Symbol_Date_Model
        ON invest.InstrumentScoreSnapshots (Symbol, ScoreAsOf, ModelVersion);
GO

IF OBJECT_ID(N'invest.PortfolioCandidateScoreSnapshots', N'U') IS NULL
BEGIN
    CREATE TABLE invest.PortfolioCandidateScoreSnapshots
    (
        PortfolioCandidateScoreSnapshotId uniqueidentifier NOT NULL
            CONSTRAINT DF_PortfolioCandidateScoreSnapshots_Id
            DEFAULT NEWSEQUENTIALID(),
        UserId uniqueidentifier NOT NULL,
        AccountId uniqueidentifier NOT NULL,
        InstrumentScoreSnapshotId uniqueidentifier NOT NULL,
        TargetWeightPercent decimal(7,4) NOT NULL,
        PortfolioFitScore decimal(5,2) NOT NULL,
        CompositeCandidateScore decimal(5,2) NOT NULL,
        ReviewTier nvarchar(100) NOT NULL,
        PortfolioImpactJson nvarchar(max) NOT NULL,
        ScoreAsOf date NOT NULL,
        ModelVersion varchar(50) NOT NULL,
        CreatedAt datetimeoffset(7) NOT NULL
            CONSTRAINT DF_PortfolioCandidateScoreSnapshots_CreatedAt
            DEFAULT SYSDATETIMEOFFSET(),

        CONSTRAINT PK_PortfolioCandidateScoreSnapshots
            PRIMARY KEY (PortfolioCandidateScoreSnapshotId),
        CONSTRAINT FK_PortfolioCandidateScoreSnapshots_User
            FOREIGN KEY (UserId) REFERENCES invest.Users (UserId),
        CONSTRAINT FK_PortfolioCandidateScoreSnapshots_AccountOwner
            FOREIGN KEY (AccountId, UserId)
            REFERENCES invest.Accounts (AccountId, OwnerUserId),
        CONSTRAINT FK_PortfolioCandidateScoreSnapshots_InstrumentScore
            FOREIGN KEY (InstrumentScoreSnapshotId)
            REFERENCES invest.InstrumentScoreSnapshots
                (InstrumentScoreSnapshotId),
        CONSTRAINT FK_PortfolioCandidateScoreSnapshots_Model
            FOREIGN KEY (ModelVersion)
            REFERENCES invest.ScoringModelVersions (ModelVersion),
        CONSTRAINT CK_PortfolioCandidateScoreSnapshots_TargetWeight
            CHECK (TargetWeightPercent > 0 AND TargetWeightPercent <= 25),
        CONSTRAINT CK_PortfolioCandidateScoreSnapshots_ImpactJson
            CHECK (ISJSON(PortfolioImpactJson) = 1)
    );

    CREATE INDEX IX_PortfolioCandidateScoreSnapshots_UserAccountDate
        ON invest.PortfolioCandidateScoreSnapshots
            (UserId, AccountId, ScoreAsOf DESC);

    CREATE UNIQUE INDEX UX_PortfolioCandidateScoreSnapshots_Context
        ON invest.PortfolioCandidateScoreSnapshots
        (
            UserId,
            AccountId,
            InstrumentScoreSnapshotId,
            TargetWeightPercent,
            ScoreAsOf,
            ModelVersion
        );
END;
GO

IF NOT EXISTS
(
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID(N'invest.PortfolioCandidateScoreSnapshots')
      AND name = N'IX_PortfolioCandidateScoreSnapshots_UserAccountDate'
)
    CREATE INDEX IX_PortfolioCandidateScoreSnapshots_UserAccountDate
        ON invest.PortfolioCandidateScoreSnapshots
            (UserId, AccountId, ScoreAsOf DESC);
GO

IF NOT EXISTS
(
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID(N'invest.PortfolioCandidateScoreSnapshots')
      AND name = N'UX_PortfolioCandidateScoreSnapshots_Context'
)
    CREATE UNIQUE INDEX UX_PortfolioCandidateScoreSnapshots_Context
        ON invest.PortfolioCandidateScoreSnapshots
        (
            UserId,
            AccountId,
            InstrumentScoreSnapshotId,
            TargetWeightPercent,
            ScoreAsOf,
            ModelVersion
        );
GO

CREATE OR ALTER PROCEDURE invest.UpsertInstrumentFeatureSnapshot
    @SeriesId int = NULL,
    @Symbol varchar(50),
    @CandidateClass varchar(50),
    @Archetype varchar(100),
    @FeatureAsOf date,
    @PriceFeaturesJson nvarchar(max),
    @FundamentalFeaturesJson nvarchar(max),
    @ExposureFeaturesJson nvarchar(max),
    @DataSourcesJson nvarchar(max),
    @DataCompletenessScore decimal(5,2),
    @ModelVersion varchar(50)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    SET @Symbol = UPPER(LTRIM(RTRIM(@Symbol)));
    IF @Symbol = '' THROW 50010, 'Symbol is required.', 1;
    IF ISJSON(@PriceFeaturesJson) <> 1
       OR ISJSON(@FundamentalFeaturesJson) <> 1
       OR ISJSON(@ExposureFeaturesJson) <> 1
       OR ISJSON(@DataSourcesJson) <> 1
        THROW 50011, 'Feature snapshot JSON is invalid.', 1;

    BEGIN TRANSACTION;

    UPDATE invest.InstrumentFeatureSnapshots WITH (UPDLOCK, HOLDLOCK)
    SET
        SeriesId = @SeriesId,
        CandidateClass = @CandidateClass,
        Archetype = @Archetype,
        PriceFeaturesJson = @PriceFeaturesJson,
        FundamentalFeaturesJson = @FundamentalFeaturesJson,
        ExposureFeaturesJson = @ExposureFeaturesJson,
        DataSourcesJson = @DataSourcesJson,
        DataCompletenessScore = @DataCompletenessScore,
        UpdatedAt = SYSDATETIMEOFFSET()
    WHERE Symbol = @Symbol
      AND FeatureAsOf = @FeatureAsOf
      AND ModelVersion = @ModelVersion;

    IF @@ROWCOUNT = 0
    BEGIN
        INSERT INTO invest.InstrumentFeatureSnapshots
        (
            SeriesId, Symbol, CandidateClass, Archetype, FeatureAsOf,
            PriceFeaturesJson, FundamentalFeaturesJson, ExposureFeaturesJson,
            DataSourcesJson, DataCompletenessScore, ModelVersion
        )
        VALUES
        (
            @SeriesId, @Symbol, @CandidateClass, @Archetype, @FeatureAsOf,
            @PriceFeaturesJson, @FundamentalFeaturesJson, @ExposureFeaturesJson,
            @DataSourcesJson, @DataCompletenessScore, @ModelVersion
        );
    END;

    COMMIT TRANSACTION;

    SELECT TOP (1) *
    FROM invest.InstrumentFeatureSnapshots
    WHERE Symbol = @Symbol
      AND FeatureAsOf = @FeatureAsOf
      AND ModelVersion = @ModelVersion;
END;
GO

CREATE OR ALTER PROCEDURE invest.UpsertInstrumentScoreSnapshot
    @FeatureSnapshotId uniqueidentifier = NULL,
    @Symbol varchar(50),
    @CandidateClass varchar(50),
    @Archetype varchar(100),
    @QualityScore decimal(5,2),
    @ValuationScore decimal(5,2),
    @GrowthScore decimal(5,2),
    @TrendScore decimal(5,2),
    @RiskScore decimal(5,2),
    @LiquidityCostScore decimal(5,2),
    @InvestmentQualityScore decimal(5,2),
    @TechnicalOpportunityScore decimal(5,2),
    @ReferenceSimilarityScore decimal(5,2) = NULL,
    @StandaloneCandidateScore decimal(5,2),
    @DataCompletenessScore decimal(5,2),
    @PeerGroupLevel varchar(30),
    @PeerCount int,
    @ClosestPeersJson nvarchar(max),
    @StrengthsJson nvarchar(max),
    @ConcernsJson nvarchar(max),
    @MissingFeaturesJson nvarchar(max),
    @ScoreAsOf date,
    @ModelVersion varchar(50)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    SET @Symbol = UPPER(LTRIM(RTRIM(@Symbol)));
    IF ISJSON(@ClosestPeersJson) <> 1
       OR ISJSON(@StrengthsJson) <> 1
       OR ISJSON(@ConcernsJson) <> 1
       OR ISJSON(@MissingFeaturesJson) <> 1
        THROW 50012, 'Score snapshot JSON is invalid.', 1;

    BEGIN TRANSACTION;

    UPDATE invest.InstrumentScoreSnapshots WITH (UPDLOCK, HOLDLOCK)
    SET
        FeatureSnapshotId = @FeatureSnapshotId,
        CandidateClass = @CandidateClass,
        Archetype = @Archetype,
        QualityScore = @QualityScore,
        ValuationScore = @ValuationScore,
        GrowthScore = @GrowthScore,
        TrendScore = @TrendScore,
        RiskScore = @RiskScore,
        LiquidityCostScore = @LiquidityCostScore,
        InvestmentQualityScore = @InvestmentQualityScore,
        TechnicalOpportunityScore = @TechnicalOpportunityScore,
        ReferenceSimilarityScore = @ReferenceSimilarityScore,
        StandaloneCandidateScore = @StandaloneCandidateScore,
        DataCompletenessScore = @DataCompletenessScore,
        PeerGroupLevel = @PeerGroupLevel,
        PeerCount = @PeerCount,
        ClosestPeersJson = @ClosestPeersJson,
        StrengthsJson = @StrengthsJson,
        ConcernsJson = @ConcernsJson,
        MissingFeaturesJson = @MissingFeaturesJson,
        UpdatedAt = SYSDATETIMEOFFSET()
    WHERE Symbol = @Symbol
      AND ScoreAsOf = @ScoreAsOf
      AND ModelVersion = @ModelVersion;

    IF @@ROWCOUNT = 0
    BEGIN
        INSERT INTO invest.InstrumentScoreSnapshots
        (
            FeatureSnapshotId, Symbol, CandidateClass, Archetype,
            QualityScore, ValuationScore, GrowthScore, TrendScore, RiskScore,
            LiquidityCostScore, InvestmentQualityScore,
            TechnicalOpportunityScore, ReferenceSimilarityScore,
            StandaloneCandidateScore, DataCompletenessScore, PeerGroupLevel,
            PeerCount, ClosestPeersJson, StrengthsJson, ConcernsJson,
            MissingFeaturesJson, ScoreAsOf, ModelVersion
        )
        VALUES
        (
            @FeatureSnapshotId, @Symbol, @CandidateClass, @Archetype,
            @QualityScore, @ValuationScore, @GrowthScore, @TrendScore,
            @RiskScore, @LiquidityCostScore, @InvestmentQualityScore,
            @TechnicalOpportunityScore, @ReferenceSimilarityScore,
            @StandaloneCandidateScore, @DataCompletenessScore,
            @PeerGroupLevel, @PeerCount, @ClosestPeersJson, @StrengthsJson,
            @ConcernsJson, @MissingFeaturesJson, @ScoreAsOf, @ModelVersion
        );
    END;

    COMMIT TRANSACTION;

    SELECT TOP (1) *
    FROM invest.InstrumentScoreSnapshots
    WHERE Symbol = @Symbol
      AND ScoreAsOf = @ScoreAsOf
      AND ModelVersion = @ModelVersion;
END;
GO

CREATE OR ALTER PROCEDURE invest.UpsertPortfolioCandidateScoreSnapshot
    @UserId uniqueidentifier,
    @AccountId uniqueidentifier,
    @InstrumentScoreSnapshotId uniqueidentifier,
    @TargetWeightPercent decimal(7,4),
    @PortfolioFitScore decimal(5,2),
    @CompositeCandidateScore decimal(5,2),
    @ReviewTier nvarchar(100),
    @PortfolioImpactJson nvarchar(max),
    @ScoreAsOf date,
    @ModelVersion varchar(50)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    IF ISJSON(@PortfolioImpactJson) <> 1
        THROW 50013, 'Portfolio-impact JSON is invalid.', 1;
    IF NOT EXISTS
    (
        SELECT 1
        FROM invest.Accounts
        WHERE AccountId = @AccountId
          AND OwnerUserId = @UserId
          AND IsActive = 1
    )
        THROW 50014, 'Account not found.', 1;

    BEGIN TRANSACTION;

    UPDATE invest.PortfolioCandidateScoreSnapshots WITH (UPDLOCK, HOLDLOCK)
    SET
        PortfolioFitScore = @PortfolioFitScore,
        CompositeCandidateScore = @CompositeCandidateScore,
        ReviewTier = @ReviewTier,
        PortfolioImpactJson = @PortfolioImpactJson
    WHERE UserId = @UserId
      AND AccountId = @AccountId
      AND InstrumentScoreSnapshotId = @InstrumentScoreSnapshotId
      AND TargetWeightPercent = @TargetWeightPercent
      AND ScoreAsOf = @ScoreAsOf
      AND ModelVersion = @ModelVersion;

    IF @@ROWCOUNT = 0
    BEGIN
        INSERT INTO invest.PortfolioCandidateScoreSnapshots
        (
            UserId, AccountId, InstrumentScoreSnapshotId,
            TargetWeightPercent, PortfolioFitScore, CompositeCandidateScore,
            ReviewTier, PortfolioImpactJson, ScoreAsOf, ModelVersion
        )
        VALUES
        (
            @UserId, @AccountId, @InstrumentScoreSnapshotId,
            @TargetWeightPercent, @PortfolioFitScore, @CompositeCandidateScore,
            @ReviewTier, @PortfolioImpactJson, @ScoreAsOf, @ModelVersion
        );
    END;

    COMMIT TRANSACTION;

    SELECT TOP (1) *
    FROM invest.PortfolioCandidateScoreSnapshots
    WHERE UserId = @UserId
      AND AccountId = @AccountId
      AND InstrumentScoreSnapshotId = @InstrumentScoreSnapshotId
      AND TargetWeightPercent = @TargetWeightPercent
      AND ScoreAsOf = @ScoreAsOf
      AND ModelVersion = @ModelVersion;
END;
GO
