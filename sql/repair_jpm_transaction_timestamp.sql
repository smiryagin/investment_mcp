/*
    One-row repair for the known JPM transaction only.

    Set @JpmTransactionId to the exact TransactionId before running this file.
    The Symbol predicate and row-count check prevent an accidental broad update.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;

DECLARE @JpmTransactionId uniqueidentifier = NULL;

IF @JpmTransactionId IS NULL
    THROW 50001, 'Set @JpmTransactionId before running this repair.', 1;

BEGIN TRANSACTION;

UPDATE invest.Transactions
SET OccurredAt =
    CAST('2026-10-01T09:30:00-04:00' AS datetimeoffset(7))
WHERE TransactionId = @JpmTransactionId
  AND Symbol = N'JPM';

IF @@ROWCOUNT <> 1
BEGIN
    ROLLBACK TRANSACTION;
    THROW 50002, 'Expected exactly one matching JPM transaction.', 1;
END;

COMMIT TRANSACTION;

SELECT TransactionId, Symbol, OccurredAt
FROM invest.Transactions
WHERE TransactionId = @JpmTransactionId;
