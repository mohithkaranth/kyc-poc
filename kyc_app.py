"""KYC workflow: intake -> PEP/sanctions -> credit bureau -> AI risk review -> decision."""
import base64
import json
import os
import secrets
import socket
import time
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request
from opentelemetry import trace
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

app = FastAPI(title="KYC Workflow POC")

# --- OpenTelemetry: switched on only when OTEL_EXPORTER_OTLP_ENDPOINT is set ---
if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create(
        {"service.name": os.getenv("OTEL_SERVICE_NAME", "kyc-app")}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # sends to the endpoint
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)   # a span for every incoming request
    HTTPXClientInstrumentor().instrument()    # a span for every outgoing call + passes the trace ID on
tracer = trace.get_tracer("kyc-app")
POD = socket.gethostname()

# Inside OpenShift, apps reach each other by their service name.
PEP_URL = os.getenv("PEP_URL", "http://mock-pep:8080/check")
CREDIT_URL = os.getenv("CREDIT_URL", "http://mock-credit:8080/report")
# AI provider: OpenAI is used if OPENAI_API_KEY is set, else Claude if ANTHROPIC_API_KEY is set,
# else simple rules. Keys come from an OpenShift Secret, never from the code.
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
# If set, AI calls go through the KrakenD gateway, which adds the OpenAI key itself.
LLM_GATEWAY_URL = os.getenv("LLM_GATEWAY_URL", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
CLAUDE_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")

# Keycloak login. Login is switched on only when KEYCLOAK_URL is set.
KC_URL = os.getenv("KEYCLOAK_URL", "").rstrip("/")            # public address (browser)
KC_INTERNAL = os.getenv("KEYCLOAK_INTERNAL_URL", KC_URL).rstrip("/")  # in-cluster address (app)
KC_REALM = os.getenv("KEYCLOAK_REALM", "kyc")
KC_CLIENT = os.getenv("KEYCLOAK_CLIENT_ID", "kyc-app")
KC_SECRET = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
APP_URL = os.getenv("APP_URL", "").rstrip("/")                # this app's public address
SESSIONS = {}  # session id -> logged-in user (kept in memory, fine for a POC)


def oidc(base: str, path: str) -> str:
    return f"{base}/realms/{KC_REALM}/protocol/openid-connect/{path}"


def current_user(request: Request):
    if not KC_URL:
        return {"username": "anonymous (login not configured)"}
    return SESSIONS.get(request.cookies.get("kyc_session", ""))


class Applicant(BaseModel):
    name: str
    country: str
    age: int
    source_of_income: str
    net_worth_sgd: float


def step(name, fn):
    t = time.time()
    with tracer.start_as_current_span(name) as span:  # one span per KYC step
        try:
            out = fn()
            ok = True
        except Exception as e:  # keep the workflow going and show the error
            out, ok = {"error": str(e)}, False
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
        span.set_attribute("kyc.step.ok", ok)
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


def record_llm_usage(resp: dict, system: str):
    """Add standard GenAI details (model, tokens) to the current trace span."""
    span = trace.get_current_span()
    usage = resp.get("usage", {})
    span.set_attribute("gen_ai.system", system)
    span.set_attribute("gen_ai.request.model", OPENAI_MODEL)
    span.set_attribute("gen_ai.response.model", resp.get("model", ""))
    span.set_attribute("gen_ai.usage.input_tokens", usage.get("prompt_tokens", 0))
    span.set_attribute("gen_ai.usage.output_tokens", usage.get("completion_tokens", 0))


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
    if LLM_GATEWAY_URL:
        r = httpx.post(
            LLM_GATEWAY_URL,
            json={"model": OPENAI_MODEL,
                  "response_format": {"type": "json_object"},
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        record_llm_usage(r.json(), "openai via KrakenD")
        text, engine = r.json()["choices"][0]["message"]["content"], f"{OPENAI_MODEL} via KrakenD"
    elif OPENAI_KEY:
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
            json={"model": OPENAI_MODEL,
                  "response_format": {"type": "json_object"},
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        record_llm_usage(r.json(), "openai")
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


@app.get("/login")
def login():
    # Step 1: send the browser to Keycloak's login page.
    state = secrets.token_urlsafe(16)
    params = {"client_id": KC_CLIENT, "response_type": "code", "scope": "openid",
              "redirect_uri": f"{APP_URL}/callback", "state": state}
    resp = RedirectResponse(oidc(KC_URL, "auth") + "?" + urlencode(params))
    resp.set_cookie("kyc_state", state, httponly=True, secure=True, samesite="lax")
    return resp


@app.get("/callback")
def callback(request: Request, code: str = "", state: str = ""):
    # Step 2: Keycloak sends the browser back with a one-time code.
    if not code or state != request.cookies.get("kyc_state"):
        return HTMLResponse("Login failed: invalid state. <a href='/login'>Try again</a>", 400)
    # Step 3: the app swaps the code for tokens, server to server.
    r = httpx.post(oidc(KC_INTERNAL, "token"), data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": f"{APP_URL}/callback",
        "client_id": KC_CLIENT, "client_secret": KC_SECRET}, timeout=10)
    if r.status_code != 200:
        return HTMLResponse(f"Token exchange failed: {r.text}", 400)
    tokens = r.json()
    # Read who logged in from the ID token (received directly from Keycloak over HTTPS).
    payload = tokens["id_token"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    sid = secrets.token_urlsafe(24)
    SESSIONS[sid] = {"username": claims.get("preferred_username"),
                     "name": claims.get("name"), "id_token": tokens["id_token"]}
    resp = RedirectResponse("/")
    resp.set_cookie("kyc_session", sid, httponly=True, secure=True, samesite="lax")
    resp.delete_cookie("kyc_state")
    return resp


@app.get("/logout")
def logout(request: Request):
    user = SESSIONS.pop(request.cookies.get("kyc_session", ""), None)
    params = {"client_id": KC_CLIENT, "post_logout_redirect_uri": f"{APP_URL}/"}
    if user:
        params["id_token_hint"] = user["id_token"]
    resp = RedirectResponse(oidc(KC_URL, "logout") + "?" + urlencode(params))
    resp.delete_cookie("kyc_session")
    return resp


@app.post("/kyc")
def kyc(a: Applicant, request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"detail": "Not logged in"}, 401)
    person = {"name": a.name, "country": a.country, "age": a.age}
    steps = [step("1. Intake and validation", lambda: intake(a))]
    if not steps[0]["output"].get("valid"):
        return {"decision": "REJECT_INCOMPLETE", "steps": steps, "pod": POD, "checked_by": user["username"]}
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
    span = trace.get_current_span()  # the span for this whole KYC request
    span.set_attribute("kyc.decision", decision)
    span.set_attribute("kyc.checked_by", user["username"])
    span.set_attribute("kyc.pep_match", bool(pep.get("pep_match")))
    span.set_attribute("kyc.sanctions_match", bool(pep.get("sanctions_match")))
    span.set_attribute("kyc.credit_grade", str(credit.get("grade")))
    return {"decision": decision, "steps": steps, "pod": POD, "checked_by": user["username"]}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    who = f"Logged in as <b>{user['username']}</b>" + (" &middot; <a href='/logout'>Log out</a>" if KC_URL else "")
    return """<!doctype html><html><head><meta charset="utf-8"><title>KYC Workflow POC</title>
<style>body{font-family:sans-serif;max-width:760px;margin:32px auto;padding:0 16px}
label{display:block;margin-top:10px;font-weight:600}input{width:100%;padding:6px}
pre{background:#f3f3f3;padding:12px;white-space:pre-wrap}button{margin-top:14px;padding:8px 16px}</style></head>
<body><p style="text-align:right">""" + who + """</p><h2>KYC Workflow POC</h2>
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
document.getElementById('d').textContent='Decision: '+j.decision+'  (checked by '+j.checked_by+')';
document.getElementById('o').textContent=JSON.stringify(j.steps,null,2);}</script></body></html>"""
