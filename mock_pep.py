"""PEP and sanctions screening service.

If DILISENSE_API_KEY is set, it screens against real watchlists via the
dilisense API. Otherwise it falls back to a small fictional test list.
"""
import os
import socket
from datetime import date

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="PEP & Sanctions Screening")
POD = socket.gethostname()
DILISENSE_KEY = os.getenv("DILISENSE_API_KEY", "")
DILISENSE_URL = "https://api.dilisense.com/v1/checkIndividual"

# Fictional test people, used only when no dilisense key is set.
WATCHLIST = {
    "ahmad rahman": {"source_type": "PEP", "detail": "Former deputy minister (fictional test record)"},
    "maria santos": {"source_type": "PEP", "detail": "Spouse of a city mayor (fictional test record)"},
    "viktor petrov": {"source_type": "SANCTION", "detail": "On a test sanctions list (fictional)"},
}


class Person(BaseModel):
    name: str
    country: str = ""
    age: int | None = None


def birth_year_ok(record: dict, age: int | None) -> bool:
    """Keep a hit only if its birth year fits the applicant's age (+/- 1 year)."""
    if age is None or not record.get("date_of_birth"):
        return True  # no way to rule it out
    expected = date.today().year - age
    for dob in record["date_of_birth"]:
        try:
            year = int(dob[-4:])
        except ValueError:
            continue
        if year == 0 or abs(year - expected) <= 1:
            return True
    return False


def summarise(hits: list, source: str, lists: str) -> dict:
    types = {h["source_type"] for h in hits}
    match_type = "SANCTION" if "SANCTION" in types else ("PEP" if "PEP" in types else (next(iter(types)) if types else None))
    return {
        "service": "pep-screening",
        "pod": POD,
        "screening_source": source,
        "lists_checked": lists,
        "pep_match": "PEP" in types,
        "sanctions_match": "SANCTION" in types,
        "other_match_types": sorted(types - {"PEP", "SANCTION"}),
        "match": bool(hits),
        "match_type": match_type,
        "total_hits": len(hits),
        "hits": hits[:5],
    }


@app.get("/health")
def health():
    return {"status": "ok", "pod": POD, "mode": "dilisense" if DILISENSE_KEY else "mock"}


@app.post("/check")
def check(p: Person):
    if not DILISENSE_KEY:
        hit = WATCHLIST.get(p.name.strip().lower())
        hits = [{"name": p.name, **hit}] if hit else []
        return summarise(hits, "mock (fictional test list)", "3 fictional test records")

    r = httpx.get(
        DILISENSE_URL,
        params={"names": p.name, "fuzzy_search": 1},
        headers={"x-api-key": DILISENSE_KEY},
        timeout=15,
    )
    r.raise_for_status()
    hits = []
    for rec in r.json().get("found_records", []):
        if not birth_year_ok(rec, p.age):
            continue
        hits.append({
            "name": rec.get("name"),
            "source_type": rec.get("source_type"),
            "source_id": rec.get("source_id"),
            "pep_type": rec.get("pep_type"),
            "date_of_birth": rec.get("date_of_birth"),
            "citizenship": rec.get("citizenship"),
        })
    return summarise(hits, "dilisense", "all sanctions, PEP and criminal sources enabled for this API key")
