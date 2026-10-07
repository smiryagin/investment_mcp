from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import server


class SqlConfigurationTests(unittest.TestCase):
    def test_accepts_explicit_connection_string(self) -> None:
        with patch.dict(
            os.environ,
            {"SQLSERVER_CONN": "DRIVER={ODBC Driver 18 for SQL Server};SERVER=test;"},
            clear=True,
        ):
            server._validate_sql_configuration()

    def test_accepts_trusted_connection_parts(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SQLSERVER_SERVER": "test-server",
                "SQLSERVER_DATABASE": "Trade",
                "SQLSERVER_TRUSTED_CONNECTION": "yes",
            },
            clear=True,
        ):
            server._validate_sql_configuration()

    def test_reports_missing_server_and_database_without_secret_values(self) -> None:
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
            RuntimeError,
            "SQLSERVER_SERVER, SQLSERVER_DATABASE",
        ) as raised:
            server._validate_sql_configuration()

        self.assertNotIn("password", str(raised.exception).lower())

    def test_sql_auth_requires_username_and_password(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SQLSERVER_SERVER": "test-server",
                "SQLSERVER_DATABASE": "Trade",
                "SQLSERVER_TRUSTED_CONNECTION": "no",
            },
            clear=True,
        ), self.assertRaisesRegex(
            RuntimeError,
            "SQLSERVER_USER, SQLSERVER_PASSWORD",
        ):
            server._validate_sql_configuration()


if __name__ == "__main__":
    unittest.main()
