/*
    Add brokerage order duration and expiration-date fields.

    Duration uses standard time-in-force codes:
      DAY, GTC (good till cancelled), GTD (good till date),
      IOC (immediate or cancel), FOK (fill or kill).
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;

BEGIN TRY
    BEGIN TRANSACTION;

    IF COL_LENGTH(N'invest.OpenOrders', N'Duration') IS NULL
        ALTER TABLE invest.OpenOrders ADD Duration varchar(10) NULL;

    IF COL_LENGTH(N'invest.OpenOrders', N'ExpiresOn') IS NULL
        ALTER TABLE invest.OpenOrders ADD ExpiresOn date NULL;

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.check_constraints
        WHERE parent_object_id = OBJECT_ID(N'invest.OpenOrders')
          AND name = N'CK_OpenOrders_Duration'
    )
    BEGIN
        EXEC sys.sp_executesql N'
            ALTER TABLE invest.OpenOrders WITH CHECK
                ADD CONSTRAINT CK_OpenOrders_Duration
                CHECK
                (
                    Duration IS NULL
                    OR Duration IN (''DAY'', ''GTC'', ''GTD'', ''IOC'', ''FOK'')
                );
        ';
    END;

    IF NOT EXISTS
    (
        SELECT 1
        FROM sys.check_constraints
        WHERE parent_object_id = OBJECT_ID(N'invest.OpenOrders')
          AND name = N'CK_OpenOrders_ExpiresOn'
    )
    BEGIN
        EXEC sys.sp_executesql N'
            ALTER TABLE invest.OpenOrders WITH CHECK
                ADD CONSTRAINT CK_OpenOrders_ExpiresOn
                CHECK
                (
                    ExpiresOn IS NULL
                    OR ExpiresOn >= CONVERT(date, SubmittedAt)
                );
        ';
    END;

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0
        ROLLBACK TRANSACTION;
    THROW;
END CATCH;
