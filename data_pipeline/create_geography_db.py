import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DIR = os.path.join(BASE_DIR, "databases")

os.makedirs(DB_DIR, exist_ok=True)

DB_PATH = os.path.join(DB_DIR, "geography.db")

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS countries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    iso2 TEXT,
    iso3 TEXT,
    continent TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS admin_regions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country_id INTEGER,
    name TEXT NOT NULL,
    level INTEGER,
    code TEXT,
    FOREIGN KEY(country_id) REFERENCES countries(id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS places (
    geonameid INTEGER PRIMARY KEY,
    name TEXT,
    asciiname TEXT,
    latitude REAL,
    longitude REAL,
    feature_class TEXT,
    feature_code TEXT,
    country_code TEXT,
    admin1_code TEXT,
    admin2_code TEXT,
    population INTEGER
)
""")

cursor.execute("""
CREATE INDEX IF NOT EXISTS idx_place_name
ON places(name);
""")

cursor.execute("""
CREATE INDEX IF NOT EXISTS idx_coordinates
ON places(latitude, longitude);
""")

conn.commit()
conn.close()

print("✅ geography.db created successfully.")