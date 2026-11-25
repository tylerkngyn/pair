from sqlalchemy import create_engine, inspect, select

from app.practice.database import Database, Lobby


def test_existing_lobbies_remain_public_after_schema_upgrade(tmp_path):
    url = f'sqlite:///{tmp_path / "old.db"}'
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql('''CREATE TABLE practice_lobbies (
            code VARCHAR(6) PRIMARY KEY, host_id VARCHAR(64) NOT NULL,
            guest_id VARCHAR(64), settings JSON NOT NULL, status VARCHAR(16) NOT NULL,
            created_at FLOAT NOT NULL, host_ready INTEGER NOT NULL,
            guest_ready INTEGER NOT NULL, match_id VARCHAR(64))''')
        connection.exec_driver_sql("INSERT INTO practice_lobbies VALUES ('ABC123','host',NULL,'{}','waiting',1,1,0,NULL)")
    engine.dispose()
    for _ in range(2):  # Upgrade is repeatable without resetting stored rows.
        database = Database(url)
        assert 'is_private' in {column['name'] for column in inspect(database.engine).get_columns('practice_lobbies')}
        with database.session() as db:
            lobby = db.scalar(select(Lobby))
            assert lobby.code == 'ABC123' and lobby.is_private is False
        database.close()
