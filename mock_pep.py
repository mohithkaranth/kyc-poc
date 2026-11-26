"""MOCK PEP and sanctions screening service (test data only, not real lists)."""
import socket
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Mock PEP & Sanctions Screening")
POD = socket.gethostname()

# Fictional test people. Anyone else comes back clear.
WATCHLIST = {
    "ahmad rahman": {"type": "PEP", "detail": "Former deputy minister (fictional test record)", "country": "MY"},
    "maria santos": {"type": "PEP", "detail": "Spouse of a city mayor (fictional test record)", "country": "PH"},
    "viktor petrov": {"type": "SANCTION", "detail": "On a test sanctions list (fictional)", "country": "RU"},
}


class Person(BaseModel):
    name: str
    country: str = ""
    dob: str = ""


@app.get("/health")
def health():
    return {"status": "ok", "pod": POD}


@app.post("/check")
def check(p: Person):
    hit = WATCHLIST.get(p.name.strip().lower())
    return {
        "service": "mock-pep",
        "pod": POD,
        "match": hit is not None,
        "match_type": hit["type"] if hit else None,
        "detail": hit["detail"] if hit else "No match found",
    }
