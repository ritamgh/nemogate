def up(conn):
    conn.execute("ALTER TABLE tasks ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'")
