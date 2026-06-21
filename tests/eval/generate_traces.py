"""
generate_traces.py  —  Fraud-aware eval trace generator

架構設計：
  1. 自己 spawn 一個 uvicorn server（帶 LEDGER_PATH=tests/eval_ledger.jsonl），
     eval 完全自包，不依賴任何手動啟動的外部 server。
  2. Case C（拆單）在 session 建立前，直接把兩筆 seed 寫入 eval_ledger.jsonl，
     讓 fraud_detector 在跑第三筆時能讀到正確的歷史。
  3. HITL 處理：偵測到 adk_request_input 後自動 resume：
     - Case A（auto_approve）：不會暫停，直通。
     - Case B/C/D（fraud 案例）：送 "no" → REJECTED。
     - Case E（injection）：送 "no" → REJECTED。
  4. 輸出 artifacts/traces/generated_traces.json 供 agents-cli eval grade 使用。
"""

import json
import os
import subprocess
import sys
import time
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── 路徑常數 ──────────────────────────────────────────────
ROOT = Path(__file__).parent.parent.parent          # repo root
DATASET_PATH = ROOT / "tests" / "eval" / "datasets" / "fraud-dataset.json"
EVAL_LEDGER  = ROOT / "tests" / "eval_ledger.jsonl"  # eval 專用 ledger
OUTPUT_DIR   = ROOT / "artifacts" / "traces"
OUTPUT_PATH  = OUTPUT_DIR / "generated_traces.json"

BASE_URL  = "http://127.0.0.1:18080"               # eval server 專用 port，避免衝突
APP_NAME  = "expense_agent"
USER_ID   = "eval_user"

# ── Case C seed 資料 ───────────────────────────────────────
THREE_DAYS_AGO = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
CASE_C_SEEDS = [
    {
        "submitter": "張三豐",
        "amount": 90000.0,
        "category": "電腦採購",
        "date": "2026-06-18",
        "timestamp": THREE_DAYS_AGO,
    },
    {
        "submitter": "張三豐",
        "amount": 90000.0,
        "category": "電腦採購",
        "date": "2026-06-19",
        "timestamp": THREE_DAYS_AGO,
    },
]

# ── 哪些案例碰到 HITL 要自動送 "no" ──────────────────────
HITL_REJECT_KEYWORDS = {"Fraud", "Injection", "Conflict", "Defunct", "Split", "Travel", "Inflation"}


def _should_reject(scenario: str) -> bool:
    return any(k in scenario for k in HITL_REJECT_KEYWORDS)


def seed_case_c():
    """以 'w' 覆寫清空 eval ledger，再精確寫入兩筆張三豐的歷史採購（3天前）。"""
    EVAL_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(EVAL_LEDGER, "w", encoding="utf-8") as f:
        for entry in CASE_C_SEEDS:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[SEED C] eval ledger 清空並寫入 {len(CASE_C_SEEDS)} 筆種子紀錄 → {EVAL_LEDGER}")


def clear_eval_ledger():
    """清空 eval ledger（非 Case C 案例用）。"""
    EVAL_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    EVAL_LEDGER.write_text("", encoding="utf-8")


def spawn_server() -> subprocess.Popen:
    """
    在 eval 專用 port 啟動 uvicorn server，注入 LEDGER_PATH env var。
    回傳 Popen 物件，由呼叫方負責 terminate()。
    """
    env = os.environ.copy()
    env["LEDGER_PATH"] = str(EVAL_LEDGER)

    cmd = [
        sys.executable, "-m", "uvicorn",
        "expense_agent.fast_api_app:app",
        "--host", "127.0.0.1",
        "--port", "18080",
        "--env-file", ".env",
    ]
    print(f"[SERVER] Spawning eval server on {BASE_URL} ...")
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def wait_for_server(timeout: int = 30) -> bool:
    """輪詢直到 server ready 或 timeout。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{BASE_URL}/list-apps", timeout=2)
            if r.status_code == 200:
                print(f"[SERVER] Ready at {BASE_URL}")
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def run_scenario(item: dict) -> dict | None:
    """執行單一 eval 案例，處理 HITL，回傳 eval_case dict 或 None（失敗）。"""
    scenario = item.get("scenario", "Unknown")
    # 剔除 _expected / eval_tags（不送給 agent）
    payload = {k: v for k, v in item.items()
               if not k.startswith("_") and k != "eval_tags"}

    print(f"\n{'='*54}")
    print(f"[RUN] {scenario}")
    print(f"{'='*54}")

    session_id = f"eval_{scenario.replace(' ', '_').replace(':', '').replace('/', '_').lower()}"

    # 建 session
    requests.post(
        f"{BASE_URL}/apps/{APP_NAME}/users/{USER_ID}/sessions",
        json={"sessionId": session_id},
    )

    # 送第一次 /run
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
        print(f"[ERROR] /run failed: {resp.status_code} {resp.text[:200]}")
        return None

    # 取 session state
    session_resp = requests.get(
        f"{BASE_URL}/apps/{APP_NAME}/users/{USER_ID}/sessions/{session_id}"
    )
    session_data = session_resp.json()

    # 偵測 HITL 暫停
    needs_input = False
    events = session_data.get("events", [])
    if events:
        last = events[-1]
        parts = last.get("content", {}).get("parts", [{}])
        if parts and parts[0].get("functionCall", {}).get("name") == "adk_request_input":
            needs_input = True

    if needs_input:
        decision = "no" if _should_reject(scenario) else "yes"
        print(f"[HITL] Suspended → auto-resume with '{decision}'")

        resume_payload = {
            "appName": APP_NAME,
            "userId": USER_ID,
            "sessionId": session_id,
            "newMessage": {
                "role": "user",
                "parts": [{"text": decision}],
            },
        }
        requests.post(f"{BASE_URL}/run", json=resume_payload, timeout=60)

        # 取最終 session state
        session_resp = requests.get(
            f"{BASE_URL}/apps/{APP_NAME}/users/{USER_ID}/sessions/{session_id}"
        )
        session_data = session_resp.json()

    # 取最終 status
    state = session_data.get("state", {})
    expense_state = state.get("expense", {})
    fraud_flags = state.get("fraud_flags", [])
    final_status = expense_state.get("status", "UNKNOWN")
    print(f"[DONE] status={final_status} | fraud_flags={fraud_flags}")

    # 組成 eval_case（prompt + full session state 作為 response）
    expected = item.get("_expected", "")
    prompt_text = json.dumps(payload, ensure_ascii=False)
    response_text = json.dumps(session_data, ensure_ascii=False)

    return {
        "eval_case_id": scenario,
        "_expected": expected,
        "prompt": {"role": "user", "parts": [{"text": prompt_text}]},
        "responses": [
            {"response": {"role": "model", "parts": [{"text": response_text}]}}
        ],
    }


def run_evaluation():
    # 讀資料集
    with open(DATASET_PATH, encoding="utf-8") as f:
        dataset = json.load(f)

    # 清空 eval ledger（預設乾淨狀態）
    clear_eval_ledger()

    # Spawn server（帶 LEDGER_PATH）
    server_proc = spawn_server()
    try:
        if not wait_for_server(timeout=40):
            print("[ERROR] Server did not start in time. Abort.")
            server_proc.terminate()
            sys.exit(1)

        eval_cases = []

        for item in dataset:
            scenario = item.get("scenario", "Unknown")

            # Case C 前先 seed ledger；其餘清空
            if "Case-C" in scenario or "Split" in scenario:
                seed_case_c()
            else:
                clear_eval_ledger()

            result = run_scenario(item)
            if result:
                eval_cases.append(result)

    finally:
        print(f"\n[SERVER] Shutting down eval server (PID {server_proc.pid}) ...")
        server_proc.terminate()
        server_proc.wait(timeout=10)

    # 寫出 traces
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"eval_cases": eval_cases}, f, indent=2, ensure_ascii=False)

    print(f"\n[v] Generated {len(eval_cases)} traces → {OUTPUT_PATH}")

    # 清理 eval ledger
    clear_eval_ledger()
    print("[DONE] eval_ledger.jsonl cleared.")


if __name__ == "__main__":
    run_evaluation()
