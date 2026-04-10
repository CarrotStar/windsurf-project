import psycopg2

HOST = "gridtrading-db.cposi6soknq8.ap-southeast-1.rds.amazonaws.com"
USER = "gridbot"
PASSWORD = "GridBot2024!"

print(f"Connecting to {HOST}...")
try:
    conn = psycopg2.connect(
        host=HOST, port=5432, dbname="postgres",
        user=USER, password=PASSWORD,
        sslmode="require", connect_timeout=10,
    )
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("SELECT 1 FROM pg_database WHERE datname = 'gridtrading'")
    if cur.fetchone():
        print("Database 'gridtrading' already exists.")
    else:
        cur.execute("CREATE DATABASE gridtrading")
        print("Database 'gridtrading' created successfully!")

    conn.close()
    print("Done — RDS is ready.")

except psycopg2.OperationalError as e:
    print(f"Connection failed: {e}")
    print("\nPossible causes:")
    print("  1. Security Group ยังไม่ได้เปิด port 5432 สำหรับ IP ของคุณ")
    print("  2. RDS instance ยังไม่ Available")
    print("  3. Public access = No")
