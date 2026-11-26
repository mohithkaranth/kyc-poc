"""MOCK credit bureau service. Scores follow a Singapore-style 1000-2000 range
with grades AA (best) to HH (worst). Test data only."""
import hashlib
import socket
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Mock Credit Bureau")
POD = socket.gethostname()

GRADES = [(1911, "AA"), (1844, "BB"), (1825, "CC"), (1813, "DD"),
          (1782, "EE"), (1755, "FF"), (1724, "GG"), (1000, "HH")]

# Fixed results for test names; anyone else gets a stable pseudo-random score.
FIXED = {
    "bad debtor": {"score": 1150, "defaults": 2, "outstanding_sgd": 85000},
    "john tan": {"score": 1890, "defaults": 0, "outstanding_sgd": 42000},
}


class Person(BaseModel):
    name: str
    age: int | None = None
    country: str = ""


def grade(score: int) -> str:
    for floor, g in GRADES:
        if score >= floor:
            return g
    return "HH"


@app.get("/health")
def health():
    return {"status": "ok", "pod": POD}


@app.post("/report")
def report(p: Person):
    key = p.name.strip().lower()
    if key in FIXED:
        r = dict(FIXED[key])
    else:
        h = int(hashlib.sha256(f"{key}|{p.age}".encode()).hexdigest(), 16)
        r = {"score": 1500 + h % 450, "defaults": 1 if h % 10 == 0 else 0,
             "outstanding_sgd": (h % 120) * 1000}
    r.update({"service": "mock-credit", "pod": POD, "grade": grade(r["score"])})
    return r
