import sqlite3

# Path to the SQLite database file
db_path = 'photobot.db'

# Connect to the database
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Check if the 'auth_token' column exists in the 'users' table
cursor.execute("PRAGMA table_info(users)")
columns = [column[1] for column in cursor.fetchall()]

if 'auth_token' not in columns:
    print("'auth_token' column is missing. Adding it to the 'users' table...")
    cursor.execute("ALTER TABLE users ADD COLUMN auth_token TEXT UNIQUE")
    cursor.execute("ALTER TABLE users ADD COLUMN auth_token_expires TIMESTAMP")
    conn.commit()
    print("'auth_token' column added successfully.")
else:
    print("'auth_token' column already exists.")

# Close the connection
conn.close()