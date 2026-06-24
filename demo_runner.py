"""
demo_runner.py — SmartAudit Demo

自動執行全部 6 個測試案例，在 terminal 印出結果。
不需要手動啟動 server，不需要貼 JSON。

用法:
    uv run python demo_runner.py
"""

import json
import os
import subprocess
import sys
import time
import requests

# 確保中文正確顯示（Windows 預設 cp950）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── 路徑常數 ──────────────────────────────────────────────
ROOT          = Path(__file__).parent
DATASET_PATH  = ROOT / "tests" / "eval" / "datasets" / "fraud-dataset.json"
EVAL_LEDGER   = ROOT / "tests" / "eval_ledger.jsonl"
EVAL_AUDIT_LOG = ROOT / "tests" / "eval_audit_log.jsonl"

BASE_URL  = "http://127.0.0.1:18080"
APP_NAME  = "expense_agent"
USER_ID   = "demo_user"

THREE_DAYS_AGO = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

CASE_C_SEEDS = [
    {"submitter": "張三豐", "amount": 90000.0, "category": "電腦採購",
     "date": "2026-06-18", "timestamp": THREE_DAYS_AGO},
    {"submitter": "張三豐", "amount": 90000.0, "category": "電腦採購",
     "date": "2026-06-19", "timestamp": THREE_DAYS_AGO},
]

HITL_REJECT_KEYWORDS = {
    "Fraud", "Injection", "Conflict", "Defunct",
    "Split", "Travel", "Inflation", "Duplicate",
}


# ── 資料種子 ───────────────────────────────────────────────

def seed_case_c():
    EVAL_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(EVAL_LEDGER, "w", encoding="utf-8") as f:
        for entry in CASE_C_SEEDS:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def seed_case_f():
    EVAL_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "case_id": "EXP-202606-SEED",
        "submitter": "林大同",
        "amount": 3500.0,
        "category": "辦公設備",
        "date": "2026-06-01",
        "description": "Purchase of office supplies",
        "invoice_no": "INV-2026-0601",
        "vendor_name": None,
        "vendor_tax_id": None,
        "fraud_flags": [],
        "related_case_ids": [],
        "risk_level": "NONE",
        "status": "APPROVED",
        "timestamp": THREE_DAYS_AGO,
    }
    with open(EVAL_AUDIT_LOG, "w", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def clear_ledger():
    EVAL_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    EVAL_LEDGER.write_text("", encoding="utf-8")


def clear_audit_log():
    EVAL_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    EVAL_AUDIT_LOG.write_text("", encoding="utf-8")


# ── Server ─────────────────────────────────────────────────

def spawn_server() -> subprocess.Popen:
    env = os.environ.copy()
    env["LEDGER_PATH"]    = str(EVAL_LEDGER)
    env["AUDIT_LOG_PATH"] = str(EVAL_AUDIT_LOG)
    cmd = [
        sys.executable, "-m", "uvicorn",
        "expense_agent.fast_api_app:app",
        "--host", "127.0.0.1",
        "--port", "18080",
        "--env-file", ".env",
    ]
    return subprocess.Popen(
        cmd, cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def wait_for_server(timeout: int = 40) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{BASE_URL}/list-apps", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


# ── 執行單一案例 ───────────────────────────────────────────

def run_case(item: dict, case_num: int) -> dict:
    scenario = item.get("scenario", "Unknown")
    payload  = {k: v for k, v in item.items()
                if not k.startswith("_") and k != "eval_tags"}

    print(f"\n  [{case_num}] {scenario}")
    print(f"       Amount: NT${payload.get('amount', '?'):,}  |  "
          f"Submitter: {payload.get('submitter', '?')}")
    expected = item.get("_expected", "")
    if expected:
        print(f"       Expected: {expected}")

    session_id = f"demo_{case_num}_{int(time.time())}"

    requests.post(
        f"{BASE_URL}/apps/{APP_NAME}/users/{USER_ID}/sessions",
        json={"sessionId": session_id},
    )

    run_payload = {
        "appName": APP_NAME,
        "userId": USER_ID,
        "sessionId": session_id,
        "newMessage": {
            "role": "user",
            "parts": [{"text": json.dumps(payload, ensure_ascii=False)}],
        },
    }
    resp = requests.post(f"{BASE_URL}/run", json=run_payload, timeout=60)
    if resp.status_code != 200:
        print(f"       ERROR: /run failed ({resp.status_code})")
        return {"scenario": scenario, "status": "ERROR", "flags": []}

    session_data = requests.get(
        f"{BASE_URL}/apps/{APP_NAME}/users/{USER_ID}/sessions/{session_id}"
    ).json()

    # 偵測 HITL 暫停
    events = session_data.get("events", [])
    needs_input = (
        events
        and events[-1].get("content", {}).get("parts", [{}])[0]
            .get("functionCall", {}).get("name") == "adk_request_input"
    )

    if needs_input:
        should_reject = any(k in scenario for k in HITL_REJECT_KEYWORDS)
        decision = "no" if should_reject else "yes"
        print(f"       [HITL] Human review required → auto-answering '{decision}'")
        requests.post(f"{BASE_URL}/run", json={
            "appName": APP_NAME,
            "userId": USER_ID,
            "sessionId": session_id,
            "newMessage": {"role": "user", "parts": [{"text": decision}]},
        }, timeout=60)
        session_data = requests.get(
            f"{BASE_URL}/apps/{APP_NAME}/users/{USER_ID}/sessions/{session_id}"
        ).json()

    state       = session_data.get("state", {})
    status      = state.get("expense", {}).get("status", "UNKNOWN")
    flags       = state.get("fraud_flags", [])
    risk_level  = state.get("expense", {}).get("risk_level", "")

    icon = {"APPROVED": "[OK] APPROVED", "REJECTED": "[!!] REJECTED"}.get(status, f"[?] {status}")
    print(f"       Result : {icon}  (risk: {risk_level or 'NONE'})")
    if flags:
        for flag in flags:
            short = flag[:90] + "..." if len(flag) > 90 else flag
            print(f"       Flag   : {short}")

    return {"scenario": scenario, "status": status, "flags": flags}


# ── 主流程 ─────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  SmartAudit Demo Runner")
    print("  Google × Kaggle 5-Day AI Agents Intensive")
    print("=" * 60)

    with open(DATASET_PATH, encoding="utf-8") as f:
        dataset = json.load(f)

    clear_ledger()
    clear_audit_log()

    print("\n  Starting agent server...")
    server_proc = spawn_server()

    try:
        if not wait_for_server(timeout=40):
            print("  ERROR: Server did not start. Check your .env file and dependencies.")
            server_proc.terminate()
            sys.exit(1)
        print("  Server ready.\n")
        print("-" * 60)

        results = []
        for i, item in enumerate(dataset, start=1):
            scenario = item.get("scenario", "")

            if "Case-C" in scenario or "Split" in scenario:
                seed_case_c()
            else:
                clear_ledger()

            if "Case-F" in scenario or "Duplicate" in scenario:
                seed_case_f()
            else:
                clear_audit_log()

            result = run_case(item, i)
            results.append(result)

    finally:
        server_proc.terminate()
        server_proc.wait(timeout=10)
        clear_ledger()
        clear_audit_log()

    # ── 結果摘要 ─────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    approved = sum(1 for r in results if r["status"] == "APPROVED")
    rejected = sum(1 for r in results if r["status"] == "REJECTED")
    errors   = sum(1 for r in results if r["status"] not in ("APPROVED", "REJECTED"))
    total    = len(results)

    for r in results:
        icon = {"APPROVED": "[OK]", "REJECTED": "[!!]"}.get(r["status"], "[?]")
        short_name = r["scenario"].split(":")[0]
        print(f"  {icon}  {short_name:<12}  {r['status']}")

    print("-" * 60)
    print(f"  Total: {total}  |  Approved: {approved}  |  Rejected: {rejected}"
          + (f"  |  Errors: {errors}" if errors else ""))
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
