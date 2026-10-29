"""Synthetic claims warehouse for a fictional anti-KIT launch ("Zelvora").

Owner (at Celldex): data engineering / data vendors. In this prototype it stands in
for licensed claims data that would land in Databricks. Everything is made up.

Run: python3 -m insight.domains.pharmaceutical.synthetic_warehouse
"""

from __future__ import annotations

import hashlib
import math
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

BRAND = "zelvora"
COMPETITORS = ("omalizumab", "dupilumab", "remibrutinib")
LAUNCH = date(2026, 4, 27)  # a Monday
WEEKS = 26
REGIONS = (
    # code, name, demand factor
    ("NE", "Northeast", 1.3),
    ("SE", "Southeast", 0.9),
    ("MW", "Midwest", 0.8),
    ("W", "West", 1.1),
)
PRESCRIBERS_PER_REGION = 20
# Share of a week's claims that have arrived by the snapshot, newest week first.
CLAIMS_COMPLETENESS = (0.55, 0.85)

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "data" / "warehouse.db"


def _poisson(random_generator: random.Random, mean: float) -> int:
    threshold, count, product = math.exp(-mean), 0, 1.0
    while True:
        product *= random_generator.random()
        if product <= threshold:
            return count
        count += 1


def build(path: Path = DEFAULT_PATH, seed: int = 7) -> dict[str, str]:
    random_generator = random.Random(seed)
    as_of = LAUNCH + timedelta(days=WEEKS * 7 - 1)

    prescribers: list[tuple[str, str, str]] = []  # prescriber_id, region, specialty
    for code, _, _ in REGIONS:
        for _ in range(PRESCRIBERS_PER_REGION):
            specialty = random_generator.choice(("Dermatology", "Allergy and Immunology"))
            prescribers.append((f"PRESCRIBER{len(prescribers) + 1:03d}", code, specialty))

    fills: list[tuple[str, str, str, str]] = []  # patient_id, prescriber_id, product, fill_date
    patient_count = 0
    for week in range(WEEKS):
        ramp = min(1.0, 0.15 + week / 12)
        for code, _, factor in REGIONS:
            if code == "SE" and week >= 20:
                factor *= 0.6  # a regional slowdown for the analyzer to find
            region_prescribers = [prescriber for prescriber in prescribers if prescriber[1] == code]
            for _ in range(_poisson(random_generator, 22 * factor * ramp)):
                patient_count += 1
                patient_id = f"PATIENT{patient_count:05d}"
                prescriber_id = random_generator.choice(region_prescribers)[0]
                start = LAUNCH + timedelta(days=week * 7 + random_generator.randrange(7))
                if random_generator.random() < 0.6:  # switched from a competitor
                    prior = start - timedelta(days=random_generator.randint(20, 80))
                    fills.append((patient_id, prescriber_id, random_generator.choice(COMPETITORS), prior.isoformat()))
                fill_date = start
                while fill_date <= as_of:
                    fills.append((patient_id, prescriber_id, BRAND, fill_date.isoformat()))
                    if random_generator.random() > 0.88:  # discontinued
                        break
                    fill_date += timedelta(days=28)

    # Claims lag: the newest weeks are incomplete at snapshot time.
    def arrived(fill_date: str) -> bool:
        weeks_back = (as_of - date.fromisoformat(fill_date)).days // 7
        if weeks_back < len(CLAIMS_COMPLETENESS):
            return random_generator.random() < CLAIMS_COMPLETENESS[weeks_back]
        return True

    fills = [fill for fill in fills if arrived(fill[3])]
    fills.sort(key=lambda fill: (fill[3], fill[0], fill[2]))
    source_hash = hashlib.sha256(repr(fills).encode()).hexdigest()[:16]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE regions (code TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE prescribers (
                prescriber_id TEXT PRIMARY KEY,
                region TEXT NOT NULL,
                specialty TEXT NOT NULL
            );
            CREATE TABLE fills (
                fill_id INTEGER PRIMARY KEY,
                patient_id TEXT NOT NULL,
                prescriber_id TEXT NOT NULL REFERENCES prescribers(prescriber_id),
                product TEXT NOT NULL,
                fill_date TEXT NOT NULL
            );
            CREATE INDEX fills_by_patient ON fills(patient_id, fill_date);
            CREATE TABLE snapshot (as_of TEXT NOT NULL, first_week TEXT NOT NULL, source_hash TEXT NOT NULL);
            """
        )
        connection.executemany("INSERT INTO regions VALUES (?, ?)", [(code, name) for code, name, _ in REGIONS])
        connection.executemany("INSERT INTO prescribers VALUES (?, ?, ?)", prescribers)
        connection.executemany(
            "INSERT INTO fills (patient_id, prescriber_id, product, fill_date) VALUES (?, ?, ?, ?)", fills
        )
        snapshot = {"as_of": as_of.isoformat(), "first_week": LAUNCH.isoformat(), "source_hash": source_hash}
        connection.execute("INSERT INTO snapshot VALUES (:as_of, :first_week, :source_hash)", snapshot)
    return snapshot


if __name__ == "__main__":
    built = build()
    print(f"wrote {DEFAULT_PATH} (as of {built['as_of']}, source {built['source_hash']})")
