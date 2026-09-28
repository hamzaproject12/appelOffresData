"""Répartition des marchés par année, et par source (portail / OCR).

Répond à une question simple : « la base couvre-t-elle bien 2024, 2025 et 2026 ? »

    cd C:\\pv\\site
    python annees.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BASE = Path("pv.db")


def main() -> None:
    if not BASE.exists():
        sys.exit(f"{BASE.resolve()} introuvable — lance ce script depuis C:\\pv\\site")
    db = sqlite3.connect(BASE)

    lignes = db.execute(
        "SELECT substr(tri_date, 1, 4) annee,"
        "       SUM(source = 'portail') portail,"
        "       SUM(source <> 'portail') ocr,"
        "       COUNT(*) total,"
        "       SUM(attributaire IS NOT NULL AND attributaire <> '') attribues"
        " FROM marches WHERE principale = 1 GROUP BY annee ORDER BY annee").fetchall()

    print(f"\n{'ANNÉE':<8}{'PORTAIL':>9}{'OCR':>9}{'TOTAL':>9}{'AVEC ATTRIBUTAIRE':>20}")
    total = 0
    for annee, portail, ocr, n, attr in lignes:
        total += n
        print(f"{annee or '(sans date)':<8}{portail:>9}{ocr:>9}{n:>9}{attr:>20}")
    print(f"{'':8}{'':9}{'':9}{total:>9}")

    bornes = db.execute(
        "SELECT MIN(tri_date), MAX(tri_date) FROM marches"
        " WHERE principale = 1 AND tri_date <> '' AND date_douteuse = 0").fetchone()
    lisible = lambda d: f"{d[6:8]}/{d[4:6]}/{d[0:4]}" if d else "?"   # noqa: E731
    print(f"\ndu {lisible(bornes[0])} au {lisible(bornes[1])} (dates sûres)")

    print("\nLes 8 acheteurs les plus présents :")
    for nom, n in db.execute(
            "SELECT acheteur, COUNT(*) n FROM marches WHERE principale = 1 AND acheteur <> ''"
            " GROUP BY acheteur ORDER BY n DESC LIMIT 8"):
        print(f"   {n:>5}  {nom[:66]}")


if __name__ == "__main__":
    main()
