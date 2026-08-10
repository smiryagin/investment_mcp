/*
    SQL Server schema for user-owned investment data.

    Ownership is enforced with composite foreign keys. For example, a row in
    invest.Transactions cannot reference an AccountId owned by a different UserId.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;

BEGIN TRY
    BEGIN TRANSACTION;

    IF SCHEMA_ID(N'invest') IS NULL
        EXEC(N'CREATE SCHEMA invest AUTHORIZATION dbo;');

    IF OBJECT_ID(N'invest.Users', N'U') IS NULL
    BEGIN
        CREATE TABLE invest.Users
        (
            UserId                uniqueidentifier NOT NULL
                CONSTRAINT DF_Users_UserId DEFAULT NEWSEQUENTIALID(),
            AuthenticationSubject nvarchar(450) NOT NULL,
            DisplayName           nvarchar(200) NULL,
            Email                 nvarchar(320) NULL,
            IsActive              bit NOT NULL
                CONSTRAINT DF_Users_IsActive DEFAULT (1),
            CreatedAt             datetimeoffset(7) NOT NULL
                CONSTRAINT DF_Users_CreatedAt DEFAULT SYSDATETIMEOFFSET(),
            UpdatedAt             datetimeoffset(7) NOT NULL
                CONSTRAINT DF_Users_UpdatedAt DEFAULT SYSDATETIMEOFFSET(),
            RowVersion            rowversion NOT NULL,

            CONSTRAINT PK_Users PRIMARY KEY (UserId),
            CONSTRAINT UQ_Users_AuthenticationSubject UNIQUE (AuthenticationSubject)
        );
    END;

    IF OBJECT_ID(N'invest.Households', N'U') IS NULL
    BEGIN
        CREATE TABLE invest.Households
        (
            HouseholdId    uniqueidentifier NOT NULL
                CONSTRAINT DF_Households_HouseholdId DEFAULT NEWSEQUENTIALID(),
            Name           nvarchar(200) NOT NULL,
            CreatedByUserId uniqueidentifier NOT NULL,
            CreatedAt      datetimeoffset(7) NOT NULL
                CONSTRAINT DF_Households_CreatedAt DEFAULT SYSDATETIMEOFFSET(),
            UpdatedAt      datetimeoffset(7) NOT NULL
                CONSTRAINT DF_Households_UpdatedAt DEFAULT SYSDATETIMEOFFSET(),
            RowVersion     rowversion NOT NULL,

            CONSTRAINT PK_Households PRIMARY KEY (HouseholdId),
            CONSTRAINT FK_Households_CreatedByUser
                FOREIGN KEY (CreatedByUserId) REFERENCES invest.Users (UserId)
        );
    END;

    IF OBJECT_ID(N'invest.HouseholdMembers', N'U') IS NULL
    BEGIN
        CREATE TABLE invest.HouseholdMembers
        (
            HouseholdId uniqueidentifier NOT NULL,
            UserId      uniqueidentifier NOT NULL,
            RoleName    varchar(20) NOT NULL,
            JoinedAt    datetimeoffset(7) NOT NULL
                CONSTRAINT DF_HouseholdMembers_JoinedAt DEFAULT SYSDATETIMEOFFSET(),

            CONSTRAINT PK_HouseholdMembers PRIMARY KEY (HouseholdId, UserId),
            CONSTRAINT CK_HouseholdMembers_RoleName
                CHECK (RoleName IN ('OWNER', 'ADMIN', 'MEMBER', 'VIEWER')),
            CONSTRAINT FK_HouseholdMembers_Household
                FOREIGN KEY (HouseholdId) REFERENCES invest.Households (HouseholdId),
            CONSTRAINT FK_HouseholdMembers_User
                FOREIGN KEY (UserId) REFERENCES invest.Users (UserId)
        );
    END;

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'invest.HouseholdMembers')
          AND name = N'UX_HouseholdMembers_OneOwner'
    )
    BEGIN
        CREATE UNIQUE INDEX UX_HouseholdMembers_OneOwner
            ON invest.HouseholdMembers (HouseholdId)
            WHERE RoleName = 'OWNER';
    END;

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'invest.HouseholdMembers')
          AND name = N'IX_HouseholdMembers_UserId'
    )
    BEGIN
        CREATE INDEX IX_HouseholdMembers_UserId
            ON invest.HouseholdMembers (UserId, HouseholdId);
    END;

    IF OBJECT_ID(N'invest.Accounts', N'U') IS NULL
    BEGIN
        CREATE TABLE invest.Accounts
        (
            AccountId        uniqueidentifier NOT NULL
                CONSTRAINT DF_Accounts_AccountId DEFAULT NEWSEQUENTIALID(),
            OwnerUserId      uniqueidentifier NOT NULL,
            AccountName      nvarchar(200) NOT NULL,
            AccountType      varchar(50) NULL,
            ProviderName     nvarchar(100) NULL,
            ProviderAccountId nvarchar(200) NULL,
            BaseCurrency     char(3) NOT NULL
                CONSTRAINT DF_Accounts_BaseCurrency DEFAULT ('USD'),
            IsActive         bit NOT NULL
                CONSTRAINT DF_Accounts_IsActive DEFAULT (1),
            CreatedAt        datetimeoffset(7) NOT NULL
                CONSTRAINT DF_Accounts_CreatedAt DEFAULT SYSDATETIMEOFFSET(),
            UpdatedAt        datetimeoffset(7) NOT NULL
                CONSTRAINT DF_Accounts_UpdatedAt DEFAULT SYSDATETIMEOFFSET(),
            RowVersion       rowversion NOT NULL,

            CONSTRAINT PK_Accounts PRIMARY KEY (AccountId),
            CONSTRAINT UQ_Accounts_AccountId_OwnerUserId UNIQUE (AccountId, OwnerUserId),
            CONSTRAINT CK_Accounts_BaseCurrency
                CHECK (BaseCurrency NOT LIKE '%[^A-Z]%'),
            CONSTRAINT FK_Accounts_OwnerUser
                FOREIGN KEY (OwnerUserId) REFERENCES invest.Users (UserId)
        );
    END;

    IF NOT EXISTS
    (
        SELECT 1 FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'invest.Accounts')
          AND name = N'IX_Accounts_OwnerUserId'
    )
    BEGIN
        CREATE INDEX IX_Accounts_OwnerUserId
            ON invest.Accounts (OwnerUserId, IsActive)
            INCLUDE (AccountName, AccountType, BaseCurrency);
    END;

    IF OBJECT_ID(N'invest.Transactions', N'U') IS NULL
    BEGIN
        CREATE TABLE invest.Transactions
        (
            TransactionId         uniqueidentifier NOT NULL
                CONSTRAINT DF_Transactions_TransactionId DEFAULT NEWSEQUENTIALID(),
            UserId                uniqueidentifier NOT NULL,
            AccountId             uniqueidentifier NOT NULL,
            ExternalTransactionId nvarchar(200) NULL,
            TransactionType       varchar(50) NOT NULL,
            Symbol                nvarchar(50) NULL,
            Quantity              decimal(28, 10) NULL,
            Price                 decimal(28, 10) NULL,
            GrossAmount           decimal(28, 10) NOT NULL,
            Fees                  decimal(28, 10) NOT NULL
                CONSTRAINT DF_Transactions_Fees DEFAULT (0),
            Currency              char(3) NOT NULL
                CONSTRAINT DF_Transactions_Currency DEFAULT ('USD'),
            TradeDate             date NULL,
            OccurredAt            datetimeoffset(7) NOT NULL,
            SettledAt             datetimeoffset(7) NULL,
            MetadataJson          nvarchar(max) NULL,
            CreatedAt             datetimeoffset(7) NOT NULL
                CONSTRAINT DF_Transactions_CreatedAt DEFAULT SYSDATETIMEOFFSET(),

            CONSTRAINT PK_Transactions PRIMARY KEY (TransactionId),
            CONSTRAINT CK_Transactions_Currency
                CHECK (Currency NOT LIKE '%[^A-Z]%'),
            CONSTRAINT CK_Transactions_MetadataJson
                CHECK (MetadataJson IS NULL OR ISJSON(MetadataJson) = 1),
            CONSTRAINT FK_Transactions_User
                FOREIGN KEY (UserId) REFERENCES invest.Users (UserId),
            CONSTRAINT FK_Transactions_AccountOwner
                FOREIGN KEY (AccountId, UserId)
                REFERENCES invest.Accounts (AccountId, OwnerUserId)
        );
    END;

    IF NOT EXISTS
    (
        SELECT 1 FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'invest.Transactions')
          AND name = N'IX_Transactions_User_Account_OccurredAt'
    )
    BEGIN
        CREATE INDEX IX_Transactions_User_Account_OccurredAt
            ON invest.Transactions (UserId, AccountId, OccurredAt DESC)
            INCLUDE (TransactionType, Symbol, Quantity, Price, GrossAmount, Currency);
    END;

    IF NOT EXISTS
    (
        SELECT 1 FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'invest.Transactions')
          AND name = N'UX_Transactions_Account_ExternalId'
    )
    BEGIN
        CREATE UNIQUE INDEX UX_Transactions_Account_ExternalId
            ON invest.Transactions (AccountId, ExternalTransactionId)
            WHERE ExternalTransactionId IS NOT NULL;
    END;

    IF OBJECT_ID(N'invest.OpenOrders', N'U') IS NULL
    BEGIN
        CREATE TABLE invest.OpenOrders
        (
            OrderId         uniqueidentifier NOT NULL
                CONSTRAINT DF_OpenOrders_OrderId DEFAULT NEWSEQUENTIALID(),
            UserId          uniqueidentifier NOT NULL,
            AccountId       uniqueidentifier NOT NULL,
            ExternalOrderId nvarchar(200) NULL,
            Symbol          nvarchar(50) NOT NULL,
            Side            varchar(10) NOT NULL,
            OrderType       varchar(30) NOT NULL,
            Quantity        decimal(28, 10) NOT NULL,
            FilledQuantity  decimal(28, 10) NOT NULL
                CONSTRAINT DF_OpenOrders_FilledQuantity DEFAULT (0),
            LimitPrice      decimal(28, 10) NULL,
            StopPrice       decimal(28, 10) NULL,
            Duration        varchar(10) NULL,
            ExpiresOn       date NULL,
            Status          varchar(30) NOT NULL
                CONSTRAINT DF_OpenOrders_Status DEFAULT ('OPEN'),
            SubmittedAt     datetimeoffset(7) NOT NULL,
            UpdatedAt       datetimeoffset(7) NOT NULL
                CONSTRAINT DF_OpenOrders_UpdatedAt DEFAULT SYSDATETIMEOFFSET(),
            MetadataJson    nvarchar(max) NULL,
            RowVersion      rowversion NOT NULL,

            CONSTRAINT PK_OpenOrders PRIMARY KEY (OrderId),
            CONSTRAINT CK_OpenOrders_Side CHECK (Side IN ('BUY', 'SELL')),
            CONSTRAINT CK_OpenOrders_Quantity CHECK (Quantity > 0),
            CONSTRAINT CK_OpenOrders_FilledQuantity
                CHECK (FilledQuantity >= 0 AND FilledQuantity <= Quantity),
            CONSTRAINT CK_OpenOrders_Duration
                CHECK (Duration IS NULL OR Duration IN ('DAY', 'GTC', 'GTD', 'IOC', 'FOK')),
            CONSTRAINT CK_OpenOrders_ExpiresOn
                CHECK (ExpiresOn IS NULL OR ExpiresOn >= CONVERT(date, SubmittedAt)),
            CONSTRAINT CK_OpenOrders_MetadataJson
                CHECK (MetadataJson IS NULL OR ISJSON(MetadataJson) = 1),
            CONSTRAINT FK_OpenOrders_User
                FOREIGN KEY (UserId) REFERENCES invest.Users (UserId),
            CONSTRAINT FK_OpenOrders_AccountOwner
                FOREIGN KEY (AccountId, UserId)
                REFERENCES invest.Accounts (AccountId, OwnerUserId)
        );
    END;

    IF NOT EXISTS
    (
        SELECT 1 FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'invest.OpenOrders')
          AND name = N'IX_OpenOrders_User_Account_Status'
    )
    BEGIN
        CREATE INDEX IX_OpenOrders_User_Account_Status
            ON invest.OpenOrders (UserId, AccountId, Status)
            INCLUDE (Symbol, Side, Quantity, FilledQuantity, LimitPrice, SubmittedAt);
    END;

    IF NOT EXISTS
    (
        SELECT 1 FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'invest.OpenOrders')
          AND name = N'UX_OpenOrders_Account_ExternalId'
    )
    BEGIN
        CREATE UNIQUE INDEX UX_OpenOrders_Account_ExternalId
            ON invest.OpenOrders (AccountId, ExternalOrderId)
            WHERE ExternalOrderId IS NOT NULL;
    END;

    IF OBJECT_ID(N'invest.CashBalances', N'U') IS NULL
    BEGIN
        CREATE TABLE invest.CashBalances
        (
            UserId          uniqueidentifier NOT NULL,
            AccountId       uniqueidentifier NOT NULL,
            Currency        char(3) NOT NULL,
            TotalAmount     decimal(28, 10) NOT NULL,
            AvailableAmount decimal(28, 10) NOT NULL,
            AsOf            datetimeoffset(7) NOT NULL,
            UpdatedAt       datetimeoffset(7) NOT NULL
                CONSTRAINT DF_CashBalances_UpdatedAt DEFAULT SYSDATETIMEOFFSET(),
            RowVersion      rowversion NOT NULL,

            CONSTRAINT PK_CashBalances PRIMARY KEY (AccountId, Currency),
            CONSTRAINT CK_CashBalances_Currency
                CHECK (Currency NOT LIKE '%[^A-Z]%'),
            CONSTRAINT FK_CashBalances_User
                FOREIGN KEY (UserId) REFERENCES invest.Users (UserId),
            CONSTRAINT FK_CashBalances_AccountOwner
                FOREIGN KEY (AccountId, UserId)
                REFERENCES invest.Accounts (AccountId, OwnerUserId)
        );
    END;

    IF NOT EXISTS
    (
        SELECT 1 FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'invest.CashBalances')
          AND name = N'IX_CashBalances_UserId'
    )
    BEGIN
        CREATE INDEX IX_CashBalances_UserId
            ON invest.CashBalances (UserId, AccountId)
            INCLUDE (Currency, TotalAmount, AvailableAmount, AsOf);
    END;

    IF OBJECT_ID(N'invest.StrategyRules', N'U') IS NULL
    BEGIN
        CREATE TABLE invest.StrategyRules
        (
            StrategyRuleId uniqueidentifier NOT NULL
                CONSTRAINT DF_StrategyRules_StrategyRuleId DEFAULT NEWSEQUENTIALID(),
            UserId         uniqueidentifier NOT NULL,
            AccountId      uniqueidentifier NULL,
            RuleName       nvarchar(200) NOT NULL,
            RuleType       varchar(50) NOT NULL,
            RuleJson       nvarchar(max) NOT NULL,
            IsEnabled      bit NOT NULL
                CONSTRAINT DF_StrategyRules_IsEnabled DEFAULT (1),
            CreatedAt      datetimeoffset(7) NOT NULL
                CONSTRAINT DF_StrategyRules_CreatedAt DEFAULT SYSDATETIMEOFFSET(),
            UpdatedAt      datetimeoffset(7) NOT NULL
                CONSTRAINT DF_StrategyRules_UpdatedAt DEFAULT SYSDATETIMEOFFSET(),
            RowVersion     rowversion NOT NULL,

            CONSTRAINT PK_StrategyRules PRIMARY KEY (StrategyRuleId),
            CONSTRAINT CK_StrategyRules_RuleJson CHECK (ISJSON(RuleJson) = 1),
            CONSTRAINT FK_StrategyRules_User
                FOREIGN KEY (UserId) REFERENCES invest.Users (UserId),
            CONSTRAINT FK_StrategyRules_AccountOwner
                FOREIGN KEY (AccountId, UserId)
                REFERENCES invest.Accounts (AccountId, OwnerUserId)
        );
    END;

    IF NOT EXISTS
    (
        SELECT 1 FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'invest.StrategyRules')
          AND name = N'IX_StrategyRules_User_Account_Enabled'
    )
    BEGIN
        CREATE INDEX IX_StrategyRules_User_Account_Enabled
            ON invest.StrategyRules (UserId, AccountId, IsEnabled)
            INCLUDE (RuleName, RuleType, UpdatedAt);
    END;

    IF OBJECT_ID(N'invest.PortfolioGrants', N'U') IS NULL
    BEGIN
        CREATE TABLE invest.PortfolioGrants
        (
            PortfolioGrantId uniqueidentifier NOT NULL
                CONSTRAINT DF_PortfolioGrants_PortfolioGrantId DEFAULT NEWSEQUENTIALID(),
            AccountId        uniqueidentifier NOT NULL,
            OwnerUserId      uniqueidentifier NOT NULL,
            RecipientUserId  uniqueidentifier NOT NULL,
            PermissionsJson  nvarchar(max) NOT NULL
                CONSTRAINT DF_PortfolioGrants_PermissionsJson DEFAULT (N'["VIEW"]'),
            GrantedAt        datetimeoffset(7) NOT NULL
                CONSTRAINT DF_PortfolioGrants_GrantedAt DEFAULT SYSDATETIMEOFFSET(),
            ExpiresAt        datetimeoffset(7) NULL,
            RevokedAt        datetimeoffset(7) NULL,
            RowVersion       rowversion NOT NULL,

            CONSTRAINT PK_PortfolioGrants PRIMARY KEY (PortfolioGrantId),
            CONSTRAINT UQ_PortfolioGrants_Account_Recipient
                UNIQUE (AccountId, RecipientUserId),
            CONSTRAINT CK_PortfolioGrants_DifferentUsers
                CHECK (OwnerUserId <> RecipientUserId),
            CONSTRAINT CK_PortfolioGrants_PermissionsJson
                CHECK (ISJSON(PermissionsJson) = 1),
            CONSTRAINT CK_PortfolioGrants_Expiry
                CHECK (ExpiresAt IS NULL OR ExpiresAt > GrantedAt),
            CONSTRAINT FK_PortfolioGrants_Owner
                FOREIGN KEY (OwnerUserId) REFERENCES invest.Users (UserId),
            CONSTRAINT FK_PortfolioGrants_Recipient
                FOREIGN KEY (RecipientUserId) REFERENCES invest.Users (UserId),
            CONSTRAINT FK_PortfolioGrants_AccountOwner
                FOREIGN KEY (AccountId, OwnerUserId)
                REFERENCES invest.Accounts (AccountId, OwnerUserId)
        );
    END;

    IF NOT EXISTS
    (
        SELECT 1 FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'invest.PortfolioGrants')
          AND name = N'IX_PortfolioGrants_Recipient_Active'
    )
    BEGIN
        CREATE INDEX IX_PortfolioGrants_Recipient_Active
            ON invest.PortfolioGrants (RecipientUserId, RevokedAt, ExpiresAt)
            INCLUDE (AccountId, OwnerUserId, PermissionsJson);
    END;

    IF OBJECT_ID(N'invest.AuditLog', N'U') IS NULL
    BEGIN
        CREATE TABLE invest.AuditLog
        (
            AuditLogId   bigint IDENTITY(1, 1) NOT NULL,
            ActorUserId  uniqueidentifier NULL,
            Operation    nvarchar(100) NOT NULL,
            TargetType   nvarchar(100) NOT NULL,
            TargetId     nvarchar(200) NULL,
            OccurredAt   datetimeoffset(7) NOT NULL
                CONSTRAINT DF_AuditLog_OccurredAt DEFAULT SYSDATETIMEOFFSET(),
            CorrelationId uniqueidentifier NULL,
            IpAddress    varchar(45) NULL,
            DetailsJson  nvarchar(max) NULL,

            CONSTRAINT PK_AuditLog PRIMARY KEY (AuditLogId),
            CONSTRAINT CK_AuditLog_DetailsJson
                CHECK (DetailsJson IS NULL OR ISJSON(DetailsJson) = 1),
            CONSTRAINT FK_AuditLog_ActorUser
                FOREIGN KEY (ActorUserId) REFERENCES invest.Users (UserId)
        );
    END;

    IF NOT EXISTS
    (
        SELECT 1 FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'invest.AuditLog')
          AND name = N'IX_AuditLog_Actor_OccurredAt'
    )
    BEGIN
        CREATE INDEX IX_AuditLog_Actor_OccurredAt
            ON invest.AuditLog (ActorUserId, OccurredAt DESC)
            INCLUDE (Operation, TargetType, TargetId, CorrelationId);
    END;

    IF NOT EXISTS
    (
        SELECT 1 FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'invest.AuditLog')
          AND name = N'IX_AuditLog_Target_OccurredAt'
    )
    BEGIN
        CREATE INDEX IX_AuditLog_Target_OccurredAt
            ON invest.AuditLog (TargetType, TargetId, OccurredAt DESC)
            INCLUDE (ActorUserId, Operation, CorrelationId);
    END;

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0
        ROLLBACK TRANSACTION;

    THROW;
END CATCH;
