"""
SQL Server / Azure SQL access for LeadRadar.

Connection settings come from ``AZURE_SQL_SERVER`` / ``AZURE_SQL_DATABASE`` /
``AZURE_SQL_USERNAME`` / ``AZURE_SQL_PASSWORD`` (they work for any SQL Server,
including a local ``mcr.microsoft.com/mssql/server`` container). Requires the
Microsoft ODBC Driver 18 for SQL Server.
"""
import logging
import pyodbc
import pandas as pd
import struct
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple
from contextlib import contextmanager
from sqlalchemy import create_engine
from urllib.parse import quote_plus
import os

logger = logging.getLogger(__name__)


def _handle_datetimeoffset(raw: bytes) -> datetime:
    """
    Output converter for SQL Server datetimeoffset (pyodbc type -155).
    pyodbc does not natively decode this type — this converter unpacks
    the raw bytes and returns a timezone-aware Python datetime.

    frac is in nanoseconds → divide by 1000 to get microseconds (0..999999).
    """
    year, month, day, hour, minute, second, frac, tz_h, tz_m = struct.unpack("<6hI2h", raw)
    return datetime(
        year, month, day, hour, minute, second, frac // 1000,
        tzinfo=timezone(timedelta(hours=tz_h, minutes=tz_m)),
    )

class SQLConnector:
    """
    Thin SQL Server / Azure SQL connector: parameterised queries via pyodbc,
    bulk DataFrame writes via SQLAlchemy, and a converter for ``datetimeoffset``.
    """

    def __init__(
        self,
        server: str = None,
        database: str = None,
        username: str = None,
        password: str = None,
        driver: str = "{ODBC Driver 18 for SQL Server}"
    ):
        """
        Initialize Azure SQL connection parameters.

        Args:
            server: Azure SQL server name (e.g., 'myserver.database.windows.net')
            database: Database name
            username: SQL authentication username
            password: SQL authentication password
            driver: ODBC driver version (default: ODBC Driver 18)
            encrypt: Use encrypted connection (recommended)
            trust_server_certificate: Whether to trust self-signed certificates
            timeout: Connection timeout in seconds
        """
        self.server = server or os.getenv("AZURE_SQL_SERVER")
        self.database = database or os.getenv("AZURE_SQL_DATABASE")
        self.username = username or os.getenv("AZURE_SQL_USERNAME")
        self.password = password or os.getenv("AZURE_SQL_PASSWORD")
        self.driver = driver
        # Set for a local SQL Server container, whose certificate is self-signed.
        self.trust_server_certificate = (
            os.getenv("AZURE_SQL_TRUST_SERVER_CERTIFICATE", "").strip().lower() in ("1", "true", "yes")
        )

        self.connection_string = (
            f"DRIVER={self.driver};"
            f"SERVER={self.server};"
            f"DATABASE={self.database};"
            f"UID={self.username};"
            f"PWD={self.password};"
            + ("TrustServerCertificate=yes;" if self.trust_server_certificate else "")
        )

    # ---------- Connection Context ----------
    @contextmanager
    def get_connection(self):
        """Context manager for database connections."""
        conn = pyodbc.connect(self.connection_string, timeout=90)
        conn.add_output_converter(-155, _handle_datetimeoffset)
        try:
            yield conn
        finally:
            conn.close()

    # ---------- Reading ----------
    def read_table(
        self,
        table_name: str,
        schema: str = "core",
        columns: Optional[List[str]] = None,
        where_clause: Optional[str] = None,
    ) -> pd.DataFrame:
        """Read a table or subset into a DataFrame."""
        cols = ", ".join(f"[{c}]" for c in columns) if columns else "*"
        query = f"SELECT {cols} FROM [{schema}].[{table_name}]"
        if where_clause:
            query += f" WHERE {where_clause}"

        with self.get_connection() as conn:
            return pd.read_sql(query, conn)

    def execute_query(
        self, query: str, params: Optional[Tuple] = None
    ) -> pd.DataFrame:
        """Execute a custom SQL query and return results as DataFrame."""
        with self.get_connection() as conn:
            return pd.read_sql(query, conn, params=params)

    # ---------- Writing ----------
    def write_table(
        self,
        df: pd.DataFrame,
        table_name: str,
        schema: str = "dbo",
        if_exists: str = "append",
        index: bool = False,
    ) -> None:
        """Write a DataFrame to SQL using SQLAlchemy for efficiency."""
        connection_url = (
            f"mssql+pyodbc://{self.username}:{quote_plus(self.password)}"
            f"@{self.server}/{self.database}?driver={self.driver.replace('{','').replace('}','').replace(' ','+')}"
            + ("&TrustServerCertificate=yes" if self.trust_server_certificate else "")
        )

        engine = create_engine(connection_url, fast_executemany=True)
        try:
            df.to_sql(
                name=table_name,
                con=engine,
                schema=schema,
                if_exists=if_exists,
                index=index,
            )
            logger.info("write_table: wrote %d rows to [%s].[%s]", len(df), schema, table_name)
        finally:
            engine.dispose()

    # ---------- Non-query Execution ----------
    def execute_non_query(
        self,
        query: str,
        params: Optional[Tuple] = None,
        commit: bool = True,
    ) -> int:
        """Execute a non-query SQL command (INSERT, UPDATE, DELETE)."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params or ())
            affected = cursor.rowcount
            if commit:
                conn.commit()
            cursor.close()
            return affected

    def execute_many(
        self,
        query: str,
        params_list: list,
        commit: bool = True,
    ) -> int:
        """
        Execute a parameterized query for each tuple in params_list using a
        single connection and a single commit.

        Uses a loop of cursor.execute() — NOT cursor.executemany() — because
        executemany() infers the SQL type from the first row and reuses it for
        all subsequent rows. This causes silent data truncation or type errors
        for columns (e.g. NVARCHAR(MAX)) where row values vary in length. The
        loop approach re-infers types independently for each row.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for params in params_list:
                cursor.execute(query, params)
            if commit:
                conn.commit()
            cursor.close()
            return len(params_list)

    # ---------- Utility ----------
    def table_exists(self, table_name: str, schema: str = "dbo") -> bool:
        """Check if a table exists in the database."""
        query = """
        SELECT COUNT(*) 
        FROM INFORMATION_SCHEMA.TABLES 
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (schema, table_name))
            return cursor.fetchone()[0] > 0
