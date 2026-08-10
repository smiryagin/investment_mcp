/*
    Least-privilege runtime permissions for the existing MCP database user.

    Existing EXECUTE grants are not changed. Run this as a database administrator
    after the invest schema migrations have completed.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;

IF DATABASE_PRINCIPAL_ID(N'mcp_connector') IS NULL
    THROW 50001, 'Database user [mcp_connector] does not exist.', 1;

BEGIN TRY
    BEGIN TRANSACTION;

    -- Resolve bearer authentication subjects and caller-owned accounts.
    GRANT SELECT ON OBJECT::invest.Users
        TO [mcp_connector];
    GRANT SELECT ON OBJECT::invest.Accounts
        TO [mcp_connector];

    -- Transaction history is append-only for the MCP runtime.
    GRANT SELECT, INSERT ON OBJECT::invest.Transactions
        TO [mcp_connector];

    -- Order records may be created, edited, filled, and soft-cancelled.
    GRANT SELECT, INSERT, UPDATE ON OBJECT::invest.OpenOrders
        TO [mcp_connector];

    -- Caller-owned mutable state.
    GRANT SELECT, INSERT, UPDATE ON OBJECT::invest.CashBalances
        TO [mcp_connector];
    GRANT SELECT, INSERT, UPDATE ON OBJECT::invest.StrategyRules
        TO [mcp_connector];
    GRANT SELECT, INSERT, UPDATE ON OBJECT::invest.PortfolioGrants
        TO [mcp_connector];

    -- Idempotency records are reserved and completed by write tools.
    GRANT SELECT, INSERT, UPDATE ON OBJECT::invest.IdempotencyKeys
        TO [mcp_connector];

    -- Status and audit histories are append-only.
    GRANT INSERT ON OBJECT::invest.OrderStatusEvents
        TO [mcp_connector];
    GRANT INSERT ON OBJECT::invest.AuditLog
        TO [mcp_connector];

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0
        ROLLBACK TRANSACTION;
    THROW;
END CATCH;
