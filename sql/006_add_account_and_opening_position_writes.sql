/*
    Runtime support for caller-created accounts and cash-neutral opening imports.

    Run after 003_grant_mcp_connector_runtime.sql. This migration is safe to
    rerun. It does not grant UPDATE or DELETE on invest.Accounts.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;

IF DATABASE_PRINCIPAL_ID(N'mcp_connector') IS NULL
    THROW 50001, 'Database user [mcp_connector] does not exist.', 1;

BEGIN TRY
    BEGIN TRANSACTION;

    GRANT SELECT, INSERT ON OBJECT::invest.Accounts
        TO [mcp_connector];

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'invest.Accounts')
          AND name = N'UX_Accounts_Owner_ProviderAccount'
    )
    BEGIN
        CREATE UNIQUE INDEX UX_Accounts_Owner_ProviderAccount
            ON invest.Accounts
                (OwnerUserId, ProviderName, ProviderAccountId)
            WHERE ProviderAccountId IS NOT NULL;
    END;

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.indexes
        WHERE object_id = OBJECT_ID(N'invest.Transactions')
          AND name = N'UX_Transactions_OneOpeningPerSymbol'
    )
    BEGIN
        CREATE UNIQUE INDEX UX_Transactions_OneOpeningPerSymbol
            ON invest.Transactions (UserId, AccountId, Symbol)
            WHERE TransactionType = 'OPENING_POSITION'
              AND IsDeleted = 0;
    END;

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0
        ROLLBACK TRANSACTION;
    THROW;
END CATCH;
