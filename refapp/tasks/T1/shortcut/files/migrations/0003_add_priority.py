def up(conn):
    conn.execute("ALTER TABLE tasks ADD COLUMN priority TEXT")
