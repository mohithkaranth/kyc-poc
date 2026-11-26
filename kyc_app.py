"""KYC workflow: intake -> PEP/sanctions -> credit bureau -> AI risk review -> decision."""
import json
import os
import socket
import time

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="KYC Workflow POC")
POD = socket.gethostname()

# Inside OpenShift, apps reach each other by their service name.
PEP_URL = os.getenv("PEP_URL", "http://mock-pep:8080/check")
CREDIT_URL = os.getenv("CREDIT_URL", "http://mock-credit:8080/report")
# AI provider: OpenAI is used if OPENAI_API_KEY is set, else Claude if ANTHROPIC_API_KEY is set,
# else simple rules. Keys come from an OpenShift Secret, never from the code.
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
CLAUDE_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")


class Applicant(BaseModel):
    name: str
    country: str
    age: int
    source_of_income: str
    net_worth_sgd: float


def step(name, fn):
    t = time.time()
    try:
        out = fn()
        ok = True
    except Exception as e:  # keep the workflow going and show the error
        out, ok = {"error": str(e)}, False
    return {"step": name, "ok": ok, "ms": int((time.time() - t) * 1000), "output": out}


def intake(a: Applicant):
    issues = []
    age = a.age
    if age < 18:
        issues.append("Applicant is under 18")
    if age > 120:
        issues.append("Age looks wrong")
    if not a.name.strip() or not a.country.strip():
        issues.append("Name and country are required")
    if a.net_worth_sgd < 0:
        issues.append("Net worth cannot be negative")
    return {"age": age, "issues": issues, "valid": not issues}


def call(url, payload):
    r = httpx.post(url, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def ai_review(a: Applicant, pep: dict, credit: dict):
    facts = {"applicant": a.model_dump(), "pep_check": pep, "credit_report": credit}
    prompt = (
        "You are a KYC analyst at a Singapore bank. Review this onboarding case and reply "
        "with ONLY a JSON object: {\"risk_level\": \"LOW|MEDIUM|HIGH\", "
        "\"recommendation\": \"APPROVE|ENHANCED_DUE_DILIGENCE|REJECT\", "
        "\"reasons\": [short strings]}. Consider PEP/sanction hits, credit grade and "
        "defaults, and whether net worth is plausible for the stated source of income.\n\n"
        + json.dumps(facts)
    )
    if OPENAI_KEY:
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
            json={"model": OPENAI_MODEL,
                  "response_format": {"type": "json_object"},
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        text, engine = r.json()["choices"][0]["message"]["content"], OPENAI_MODEL
    elif CLAUDE_KEY:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01"},
            json={"model": CLAUDE_MODEL, "max_tokens": 500,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        text, engine = r.json()["content"][0]["text"], CLAUDE_MODEL
    else:
        return {"engine": "rules (no AI key set)", **rules_review(a, pep, credit)}
    start, end = text.find("{"), text.rfind("}")
    return {"engine": engine, **json.loads(text[start:end + 1])}


def rules_review(a, pep, credit):
    reasons, level = [], "LOW"
    if pep.get("match_type") == "SANCTION":
        return {"risk_level": "HIGH", "recommendation": "REJECT", "reasons": ["Sanctions match"]}
    if pep.get("match_type") == "PEP":
        reasons.append("Politically exposed person"); level = "HIGH"
    if credit.get("grade") in ("GG", "HH") or credit.get("defaults", 0) > 0:
        reasons.append("Weak credit history"); level = "HIGH" if level == "HIGH" else "MEDIUM"
    if a.net_worth_sgd > 5_000_000:
        reasons.append("High net worth: verify source of wealth")
        level = "HIGH" if level == "HIGH" else "MEDIUM"
    rec = {"LOW": "APPROVE", "MEDIUM": "ENHANCED_DUE_DILIGENCE", "HIGH": "ENHANCED_DUE_DILIGENCE"}[level]
    return {"risk_level": level, "recommendation": rec, "reasons": reasons or ["No concerns found"]}


@app.get("/health")
def health():
    return {"status": "ok", "pod": POD}


@app.post("/kyc")
def kyc(a: Applicant):
    person = {"name": a.name, "country": a.country, "age": a.age}
    steps = [step("1. Intake and validation", lambda: intake(a))]
    if not steps[0]["output"].get("valid"):
        return {"decision": "REJECT_INCOMPLETE", "steps": steps, "pod": POD}
    steps.append(step("2. PEP and sanctions check", lambda: call(PEP_URL, person)))
    steps.append(step("3. Credit bureau check", lambda: call(CREDIT_URL, person)))
    pep, credit = steps[1]["output"], steps[2]["output"]
    steps.append(step("4. AI risk review", lambda: ai_review(a, pep, credit)))
    review = steps[3]["output"]
    if not all(s["ok"] for s in steps):
        decision = "MANUAL_REVIEW (a step failed)"
    elif pep.get("match_type") == "SANCTION":
        decision = "REJECT"  # hard rule: never left to the AI
    else:
        decision = review.get("recommendation", "MANUAL_REVIEW")
    steps.append({"step": "5. Decision", "ok": True, "ms": 0, "output": {"decision": decision}})
    return {"decision": decision, "steps": steps, "pod": POD}


@app.get("/", response_class=HTMLResponse)
def home():
    return """<!doctype html><html><head><meta charset="utf-8"><title>KYC Workflow POC</title>
<style>body{font-family:sans-serif;max-width:760px;margin:32px auto;padding:0 16px}
label{display:block;margin-top:10px;font-weight:600}input{width:100%;padding:6px}
pre{background:#f3f3f3;padding:12px;white-space:pre-wrap}button{margin-top:14px;padding:8px 16px}</style></head>
<body><h2>KYC Workflow POC</h2>
<p>Try real public figures for PEP screening, <b>Bad Debtor</b> for poor credit, or <b>John Tan</b> as a clean case.</p>
<label>Full name</label><input id="name" value="John Tan">
<label>Country</label><input id="country" value="SG">
<label>Age</label><input id="age" type="number" value="41">
<label>Source of income</label><input id="soi" value="Salaried engineer, SGD 8,500 per month">
<label>Net worth (SGD)</label><input id="nw" value="450000">
<button onclick="go()">Run KYC</button><h3 id="d"></h3><pre id="o"></pre>
<script>async function go(){document.getElementById('d').textContent='Running...';
const v=id=>document.getElementById(id).value;
const b={name:v('name'),country:v('country'),age:parseInt(v('age')),source_of_income:v('soi'),net_worth_sgd:parseFloat(v('nw'))};
const r=await fetch('/kyc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
const j=await r.json();
if(!r.ok){document.getElementById('d').textContent='Error '+r.status;document.getElementById('o').textContent=JSON.stringify(j,null,2);return;}
document.getElementById('d').textContent='Decision: '+j.decision;
document.getElementById('o').textContent=JSON.stringify(j.steps,null,2);}</script></body></html>"""
