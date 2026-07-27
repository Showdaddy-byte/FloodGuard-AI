import sqlite3
import zipfile
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB_PATH = os.path.join(BASE_DIR, "databases", "geography.db")
ZIP_FILE = os.path.join(BASE_DIR, "datasets", "geonames", "NG.zip")

print("Opening database...")
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

print("Opening GeoNames dataset...")

with zipfile.ZipFile(ZIP_FILE) as z:
    filename = "NG.txt"

    with z.open(filename) as f:

        count = 0

        for line in f:

            row = line.decode("utf-8", errors="ignore").strip().split("\t")

            if len(row) < 19:
                continue

            geonameid = int(row[0])
            name = row[1]
            asciiname = row[2]
            latitude = float(row[4])
            longitude = float(row[5])
            feature_class = row[6]
            feature_code = row[7]
            country_code = row[8]

            if country_code != "NG":
                continue

            admin1 = row[10]
            admin2 = row[11]
            population = int(row[14] or 0)

            cursor.execute("""
                INSERT OR REPLACE INTO places
                (
                    geonameid,
                    name,
                    asciiname,
                    latitude,
                    longitude,
                    feature_class,
                    feature_code,
                    country_code,
                    admin1_code,
                    admin2_code,
                    population
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                geonameid,
                name,
                asciiname,
                latitude,
                longitude,
                feature_class,
                feature_code,
                country_code,
                admin1,
                admin2,
                population
            ))

            count += 1

            if count % 100000 == 0:
                conn.commit()
                print(f"{count:,} places imported...")

conn.commit()
conn.close()

print()
print("Import completed successfully.")