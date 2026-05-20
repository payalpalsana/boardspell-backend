# Database Connection
# ====================
# Connects to PostgreSQL using the asyncpg driver.
# All SQL queries go through the `database` object.


import databases
import os
from dotenv import load_dotenv

load_dotenv()

# Get database URL from environment variable
DATABASE_URL = os.getenv("DATABASE_URL")

# Create database connection object
database = databases.Database(DATABASE_URL)


async def connect_db():
    # Connect to PostgreSQL — called when app starts
    await database.connect()
    print("✅ Connected to PostgreSQL — boardspell")


async def disconnect_db():
    # Disconnect from PostgreSQL — called when app stops
    await database.disconnect()
    print("🔌 Disconnected from PostgreSQL")