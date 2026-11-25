"""Explicit, idempotent upgrades, run under Database's startup schema lock."""
from sqlalchemy import inspect


def upgrade(connection):
    # Existing lobby rows stay public. Compatible with SQLite and PostgreSQL.
    columns = {column["name"] for column in inspect(connection).get_columns("practice_lobbies")}
    if "is_private" not in columns:
        connection.exec_driver_sql(
            "ALTER TABLE practice_lobbies ADD COLUMN is_private BOOLEAN NOT NULL DEFAULT false"
        )
