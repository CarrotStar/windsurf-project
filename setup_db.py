import psycopg2
import psycopg2.sql as sql
import requests
from config import Config

print(f"Connecting to {Config.DB_HOST}...")
print(f"Database user: {Config.DB_USER}")
try:
    # Test public IP for easier debugging of security group issues
    try:
        public_ip = requests.get('https://api.ipify.org', timeout=5).text
        print(f"Your public IP appears to be: {public_ip}")
        print("Ensure this IP is allowed in your RDS instance's security group inbound rules on port 5432.")
    except requests.exceptions.RequestException as e:
        print(f"(Could not determine public IP: {e})")

    conn = psycopg2.connect(
        host=Config.DB_HOST, port=Config.DB_PORT, dbname="postgres",
        user=Config.DB_USER, password=Config.DB_PASSWORD,
        sslmode=Config.DB_SSL_MODE, connect_timeout=10,
    )
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (Config.DB_NAME,))
    if cur.fetchone():
        print(f"Database '{Config.DB_NAME}' already exists.")
    else:
        # Use sql.SQL to safely format the database name in the CREATE DATABASE command
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(Config.DB_NAME)))
        print(f"Database '{Config.DB_NAME}' created successfully!")

    conn.close()
    print("Done — RDS is ready.")

except psycopg2.OperationalError as e:
    print(f"Connection failed: {e}")
    print("\nThis is likely an AWS security group issue.")
    print("\nTroubleshooting steps:")
    print("  1. Go to your RDS instance in the AWS Console.")
    print("  2. Navigate to 'Connectivity & security' -> VPC security groups.")
    print("  3. Edit 'Inbound rules' to add a rule of type 'PostgreSQL' with your IP as the source.")
