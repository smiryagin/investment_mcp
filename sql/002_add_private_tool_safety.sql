/*
    Additive SQL Server migration for authenticated private MCP tools.
    Run after 001_create_invest_schema.sql.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;

BEGIN TRY
    BEGIN TRANSACTION;

    IF COL_LENGTH(N'invest.OpenOrders', N'ClientOrderId') IS NULL
    BEGIN
        ALTER TABLE invest.OpenOrders ADD ClientOrderId nvarchar(200) NULL;
        EXEC sys.sp_executesql N'
            UPDATE invest.OpenOrders
            SET ClientOrderId = COALESCE(
                ExternalOrderId,
                CONVERT(nvarchar(36), OrderId)
            )
            WHERE ClientOrderId IS NULL;
        ';
        EXEC sys.sp_executesql N'
            ALTER TABLE invest.OpenOrders
                ALTER COLUMN ClientOrderId nvarchar(200) NOT NULL;
        ';
    END;

    IF COL_LENGTH(N'invest.OpenOrders', N'IsDeleted') IS NULL
        ALTER TABLE invest.OpenOrders ADD IsDeleted bit NOT NULL
            CONSTRAINT DF_OpenOrders_IsDeleted DEFAULT (0);
    IF COL_LENGTH(N'invest.OpenOrders', N'DeletedAt') IS NULL
        ALTER TABLE invest.OpenOrders ADD DeletedAt datetimeoffset(7) NULL;
    IF COL_LENGTH(N'invest.OpenOrders', N'DeletedByUserId') IS NULL
        ALTER TABLE invest.OpenOrders ADD DeletedByUserId uniqueidentifier NULL;

    IF NOT EXISTS
    (
        SELECT 1 FROM sys.foreign_keys
        WHERE parent_object_id = OBJECT_ID(N'invest.OpenOrders')
          AND name = N'FK_OpenOrders_DeletedByUser'
    )
    BEGIN
        EXEC sys.sp_executesql N'
            ALTER TABLE invest.OpenOrders
                ADD CONSTRAINT FK_OpenOrders_DeletedByUser
                FOREIGN KEY (DeletedByUserId) REFERENCES invest.Users (UserId);
        ';
    END;

    IF NOT EXISTS
    (
        SELECT 1 FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'invest.OpenOrders')
          AND name = N'UX_OpenOrders_User_Account_ClientOrderId'
    )
    BEGIN
        EXEC sys.sp_executesql N'
            CREATE UNIQUE INDEX UX_OpenOrders_User_Account_ClientOrderId
                ON invest.OpenOrders (UserId, AccountId, ClientOrderId);
        ';
    END;

    IF COL_LENGTH(N'invest.Transactions', N'IsDeleted') IS NULL
        ALTER TABLE invest.Transactions ADD IsDeleted bit NOT NULL
            CONSTRAINT DF_Transactions_IsDeleted DEFAULT (0);
    IF COL_LENGTH(N'invest.Transactions', N'DeletedAt') IS NULL
        ALTER TABLE invest.Transactions ADD DeletedAt datetimeoffset(7) NULL;
    IF COL_LENGTH(N'invest.Transactions', N'DeletedByUserId') IS NULL
        ALTER TABLE invest.Transactions ADD DeletedByUserId uniqueidentifier NULL;

    IF NOT EXISTS
    (
        SELECT 1 FROM sys.foreign_keys
        WHERE parent_object_id = OBJECT_ID(N'invest.Transactions')
          AND name = N'FK_Transactions_DeletedByUser'
    )
    BEGIN
        EXEC sys.sp_executesql N'
            ALTER TABLE invest.Transactions
                ADD CONSTRAINT FK_Transactions_DeletedByUser
                FOREIGN KEY (DeletedByUserId) REFERENCES invest.Users (UserId);
        ';
    END;

    IF OBJECT_ID(N'invest.IdempotencyKeys', N'U') IS NULL
    BEGIN
        CREATE TABLE invest.IdempotencyKeys
        (
            IdempotencyRecordId bigint IDENTITY(1, 1) NOT NULL,
            UserId              uniqueidentifier NOT NULL,
            ToolName            nvarchar(100) NOT NULL,
            IdempotencyKey      uniqueidentifier NOT NULL,
            RequestHash         binary(32) NOT NULL,
            Status              varchar(20) NOT NULL,
            ResponseJson        nvarchar(max) NULL,
            CreatedAt           datetimeoffset(7) NOT NULL
                CONSTRAINT DF_IdempotencyKeys_CreatedAt DEFAULT SYSDATETIMEOFFSET(),
            CompletedAt         datetimeoffset(7) NULL,

            CONSTRAINT PK_IdempotencyKeys PRIMARY KEY (IdempotencyRecordId),
            CONSTRAINT UQ_IdempotencyKeys_User_Tool_Key
                UNIQUE (UserId, ToolName, IdempotencyKey),
            CONSTRAINT CK_IdempotencyKeys_Status
                CHECK (Status IN ('STARTED', 'COMPLETED')),
            CONSTRAINT FK_IdempotencyKeys_User
                FOREIGN KEY (UserId) REFERENCES invest.Users (UserId)
        );
    END;

    IF OBJECT_ID(N'invest.OrderStatusEvents', N'U') IS NULL
    BEGIN
        CREATE TABLE invest.OrderStatusEvents
        (
            OrderStatusEventId bigint IDENTITY(1, 1) NOT NULL,
            OrderId           uniqueidentifier NOT NULL,
            UserId            uniqueidentifier NOT NULL,
            AccountId         uniqueidentifier NOT NULL,
            PreviousStatus    varchar(30) NULL,
            NewStatus         varchar(30) NOT NULL,
            ActorUserId       uniqueidentifier NOT NULL,
            Reason            nvarchar(500) NULL,
            OccurredAt        datetimeoffset(7) NOT NULL
                CONSTRAINT DF_OrderStatusEvents_OccurredAt DEFAULT SYSDATETIMEOFFSET(),

            CONSTRAINT PK_OrderStatusEvents PRIMARY KEY (OrderStatusEventId),
            CONSTRAINT FK_OrderStatusEvents_Order
                FOREIGN KEY (OrderId) REFERENCES invest.OpenOrders (OrderId),
            CONSTRAINT FK_OrderStatusEvents_AccountOwner
                FOREIGN KEY (AccountId, UserId)
                REFERENCES invest.Accounts (AccountId, OwnerUserId),
            CONSTRAINT FK_OrderStatusEvents_Actor
                FOREIGN KEY (ActorUserId) REFERENCES invest.Users (UserId)
        );

        CREATE INDEX IX_OrderStatusEvents_Order_OccurredAt
            ON invest.OrderStatusEvents (OrderId, OccurredAt, OrderStatusEventId);
    END;

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0
        ROLLBACK TRANSACTION;
    THROW;
END CATCH;
