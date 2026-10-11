def up(conn):
    conn.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            descripton TEXT,
            done INTEGER NOT NULL DEFAULT 0
        )
        """
    )
