# ruff: noqa
import os
import json
import base64
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel

from google.adk.workflow import Workflow, node
from google.adk.events.event import Event
from google.adk.agents.context import Context
from google.adk.agents import LlmAgent
from google.adk.events.request_input import RequestInput
from google.adk.apps import App



# 引入抽離的設定值 (Import extracted configuration)
from .config import THRESHOLD, MODEL_NAME

# ---------------------------------------------------------
# 資料結構定義 (Data Schemas - Pydantic Models)
# ---------------------------------------------------------
class ExpenseReport(BaseModel):
    amount: float
    submitter: str
    category: str
    description: str
    date: str
    status: str = "PENDING"
    # --- 防弊擴充欄位（全部 Optional，缺值時對應檢查自動跳過）---
    invoice_no: Optional[str] = None         # 發票號碼（用於重複報支偵測）
    vendor_name: Optional[str] = None        # 廠商名稱
    vendor_tax_id: Optional[str] = None     # 統一編號（比對歇業廠商，優先於名稱）
    trip_days: Optional[int] = None          # 申報出差天數
    actual_trip_days: Optional[int] = None  # 實際行程天數（用於浮報比對）
    hotel_per_night: Optional[float] = None # 住宿費每夜金額
    misc_per_day: Optional[float] = None    # 雜費每日金額

class RiskAssessment(BaseModel):
    risk_level: str
    reasoning: str

# ---------------------------------------------------------
# 輔助函式 (Helper Functions)
# ---------------------------------------------------------
def _safe_float(value) -> Optional[float]:
    """Safely convert to float; returns None on failure instead of raising."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def extract_expense_data(payload: Any) -> dict:
    """
    從原始字典或 Pub/Sub Base64 格式中萃取 JSON。
    Extracts expense JSON from either raw dict or Pub/Sub base64 payload.
    """
    # 1. ADK Web UI 有時會把參數包裝成 tuple，必須最先剝開！
    if isinstance(payload, tuple) and len(payload) > 0:
        payload = payload[0]

    # 2. 如果 payload 是一個 Event 物件 (ADK 2.0 常見行為)，剝開它！
    if hasattr(payload, "output") and payload.output:
        payload = payload.output

    if not isinstance(payload, dict):
        try:
            if hasattr(payload, 'text'):
                payload = json.loads(payload.text)
            elif isinstance(payload, str):
                import ast
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = ast.literal_eval(payload)
            elif hasattr(payload, 'model_dump'):
                payload = payload.model_dump()
            else:
                payload = dict(payload)
        except Exception:
            pass

    if not isinstance(payload, dict):
        return {}

    data = payload.get("data", payload)
    
    # 處理 ADK Web UI 傳入的 Content 物件結構 ( {'parts': [{'text': '{"amount": 150}'}]} )
    if "parts" in data and isinstance(data["parts"], list) and len(data["parts"]) > 0:
        part = data["parts"][0]
        if isinstance(part, dict) and "text" in part and part["text"]:
            try:
                raw_dict = json.loads(part["text"])
                
                # Unpack ADK's built-in pubsub wrapper if present
                if "data" in raw_dict and isinstance(raw_dict["data"], dict):
                    raw_dict = raw_dict["data"]
                
                return raw_dict
            except Exception:
                pass
    
    if isinstance(data, str):
        try:
            decoded = base64.b64decode(data).decode('utf-8')
            return json.loads(decoded)
        except Exception:
            try:
                return json.loads(data)
            except Exception:
                return {}
    elif isinstance(data, dict):
        return data
    
    return {}

# ---------------------------------------------------------
# 工作流節點定義 (Workflow Nodes)
# ---------------------------------------------------------

@node
def parse_and_route(ctx: Context, node_input: Any) -> Event:
    """
    節點 1：解析傳入的資料。
    Node 1: Parses payload.
    """
    raw_dict = extract_expense_data(node_input)
    has_expense_keys = any(k in raw_dict for k in ["amount", "scenario", "description", "category"])
    
    print(f"[DEBUG parse] raw_dict={raw_dict}")
    print(f"[DEBUG parse] has_keys={has_expense_keys}")
    print(f"[DEBUG parse] state={ctx.state.model_dump() if hasattr(ctx.state, 'model_dump') else ctx.state}")
    
    # 修復漏洞一：狀態覆蓋漏洞 (State Reset on Resume)
    has_expense_keys = any(k in raw_dict for k in ["amount", "scenario", "description", "category"])
    if not has_expense_keys and ctx.state.get("expense") and ctx.state["expense"].get("amount", 0.0) > 0:
        print("[*] Resuming from suspension, using preserved state.")
        expense_dict = ctx.state["expense"]
        expense = ExpenseReport(**expense_dict)
        
        # 處理使用者直接在對話框輸入的 "yes" 或 "no"
        parts = raw_dict.get("parts", [])
        text = parts[0].get("text", "").strip().lower() if parts else ""
        if text in ["yes", "y", "approve", "approved"]:
            expense.status = "APPROVED"
            return Event(output=expense, route="record_outcome", state={"expense": expense.model_dump()})
        elif text in ["no", "n", "reject", "rejected"]:
            expense.status = "REJECTED"
            return Event(output=expense, route="record_outcome", state={"expense": expense.model_dump()})
    else:
        raw_amount = raw_dict.get("amount", "")
        amount = _safe_float(raw_amount)
        if amount is None:
            case_id = ctx.state.get("case_id") or _generate_case_id()
            minimal_expense = ExpenseReport(
                amount=0.0,
                submitter=str(raw_dict.get("submitter", "Unknown")),
                category=str(raw_dict.get("category", "Unknown")),
                description=str(raw_dict.get("description", "No description")),
                date=str(raw_dict.get("date", "Unknown")),
            )
            risk_alert = RiskAssessment(
                risk_level="HIGH",
                reasoning=f"Invalid input: 'amount' must be a number (received: '{raw_amount}'). Claim flagged for manual review."
            )
            print(f"[!] [PARSE] Invalid amount: '{raw_amount}'. Routing to human_approval.")
            return Event(
                output=risk_alert,
                route="human_approval",
                state={
                    "expense": minimal_expense.model_dump(),
                    "case_id": case_id,
                    "fraud_flags": ["Invalid input: 'amount' must be a number"],
                }
            )
        expense_dict = {
            "amount": amount,
            "submitter": raw_dict.get("submitter", "Unknown"),
            "category": raw_dict.get("category", "Unknown"),
            "description": raw_dict.get("description", "No description"),
            "date": raw_dict.get("date", "Unknown"),
            "status": "PENDING",
            # --- 防弊欄位，缺值維持 None ---
            "invoice_no": raw_dict.get("invoice_no"),
            "vendor_name": raw_dict.get("vendor_name"),
            "vendor_tax_id": raw_dict.get("vendor_tax_id"),
            "trip_days": raw_dict.get("trip_days"),
            "actual_trip_days": raw_dict.get("actual_trip_days"),
            "hotel_per_night": _safe_float(raw_dict.get("hotel_per_night")),
            "misc_per_day": _safe_float(raw_dict.get("misc_per_day")),
        }
    
    expense = ExpenseReport(**expense_dict)

    # 產生唯一案件編號（僅新進件才生成，Resume 時沿用 state 內的）
    case_id = ctx.state.get("case_id") or _generate_case_id()

    # 不論金額多小，一律送進資安檢查哨過濾惡意指令與個資！
    print(f"[*] [{case_id}] Expense amount: ${expense.amount}. Routing to security_checkpoint.")
    return Event(
        output=expense,
        route="security_checkpoint",
        state={"expense": expense.model_dump(), "case_id": case_id},
    )

@node
def auto_approve(ctx: Context, node_input: ExpenseReport) -> Event:
    """
    節點 2：瞬間批准低於門檻的收據。
    Node 2: Instantly approves expenses under the threshold.
    """
    node_input.status = "APPROVED"
    return Event(output=node_input, state={"expense": node_input.model_dump()})

@node
def security_checkpoint(ctx: Context, node_input: ExpenseReport) -> Event:
    """
    節點 3：資安檢查哨，負責抹除個資 (PII) 與防禦提示詞注入 (Prompt Injection)。
    
    [架構筆記]：在 ADK 2.0 中，節點名稱 (如 security_checkpoint) 與路由標籤 (route) 並非系統保留字。
    開發者完全可以自由命名 (例如官方教學文件的 security_screen，或是 firewall 等)。
    只要在最下方的 Workflow(edges=[...]) 中將名字對應正確，圖表就能順利運作！
    """
    original_desc = node_input.description
    
    # 🛡️ 防禦機制 1：抹除機密個資 (Data Scrubbing)
    # 必須先清洗，確保後續無論是進 LLM 還是直接送人類審核，都是乾淨的！
    # 使用 Regex 獵殺並替換身分證字號與信用卡號
    redacted_desc = re.sub(r'\b\d{3}-\d{2}-\d{4}\b', '[REDACTED_SSN]', original_desc)
    redacted_desc = re.sub(r'\b(?:\d[ -]*?){13,16}\b', '[REDACTED_CC]', redacted_desc)
    
    # 增加漏洞三修復：Email 隱碼 (Redaction)
    redacted_desc = re.sub(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', '[REDACTED_EMAIL]', redacted_desc)
    # Submitter 也可能包含 Email，一併處理
    node_input.submitter = re.sub(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', '[REDACTED_EMAIL]', node_input.submitter)
    
    redacted_categories = []
    if '[REDACTED_SSN]' in redacted_desc:
        redacted_categories.append('SSN')
    if '[REDACTED_CC]' in redacted_desc:
        redacted_categories.append('CreditCard')
    if '[REDACTED_EMAIL]' in redacted_desc or '[REDACTED_EMAIL]' in node_input.submitter:
        redacted_categories.append('Email')
        
    # 將清洗過後的乾淨字串，覆蓋回原本的收據物件中
    node_input.description = redacted_desc
    
    # 更新狀態字典
    state_updates = {"expense": node_input.model_dump()}
    if redacted_categories:
        state_updates["redacted_categories"] = redacted_categories
        print(f"[*] [SECURITY] PII Cleaned. Redacted: {redacted_categories}")

    # 防禦機制 2：抵禦 Prompt Injection (提示詞注入攻擊)
    # 在乾淨的字串中尋找惡意指令
    description_lower = redacted_desc.lower()
    suspicious_keywords = ["ignore", "bypass", "force approve", "auto-approval", "override"]
    if any(kw in description_lower for kw in suspicious_keywords):
        print("[!] [SECURITY] Prompt Injection detected! Bypassing LLM.")
        # 抓到了！直接由 Python「偽造」一份極度危險的風險報告！
        risk_alert = RiskAssessment(
            risk_level="CRITICAL", 
            reasoning="Security Event: Prompt Injection Detected in Description. Attempted to bypass rules."
        )
        # 走捷徑！把這份紅色警報直接送給人類經理 (human_approval 節點)，完全不經過 LLM！
        # 注意：此時 state 裡的 expense 已經是清洗過的乾淨版本了
        return Event(output=risk_alert, route="human_approval", state=state_updates)

    # 檢查合格！一律進 fraud_detector 做防弊檢查
    if node_input.status in ["APPROVED", "REJECTED"]:
        print(f"[*] [SECURITY] Expense is already {node_input.status}. Routing to record_outcome.")
        route = "record_outcome"
    else:
        print(f"[*] [SECURITY] Clean request. Routing to fraud_detector.")
        route = "fraud_detector"

    return Event(output=node_input, route=route, state=state_updates)

# ---------------------------------------------------------
# 防弊核心：Helper Functions（純 Python，不依賴 LLM）
# ---------------------------------------------------------
_DATA_DIR = Path(__file__).parent / "data"
_LEDGER_PATH = Path(
    os.environ.get("LEDGER_PATH", str(_DATA_DIR / "purchase_ledger.jsonl"))
)
_AUDIT_LOG_PATH = Path(
    os.environ.get("AUDIT_LOG_PATH", str(_DATA_DIR / "audit_log.jsonl"))
)


def _load_policy() -> dict:
    with open(_DATA_DIR / "policy.json", encoding="utf-8") as f:
        return json.load(f)


def _load_vendors() -> list:
    with open(_DATA_DIR / "vendors.json", encoding="utf-8") as f:
        return json.load(f)


def _find_vendor(
    vendors: list, tax_id: Optional[str], name: Optional[str]
) -> Optional[dict]:
    """統編優先，缺值才 fallback 到名稱模糊比對。"""
    if tax_id:
        for v in vendors:
            if v.get("tax_id") == tax_id:
                return v
    if name:
        name_lower = name.strip().lower()
        for v in vendors:
            if v.get("name", "").strip().lower() == name_lower:
                return v
    return None


def _append_ledger(expense: ExpenseReport, ctx: Context) -> None:
    """把本筆採購寫入跨筆拆單追蹤 ledger。"""
    _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "case_id": ctx.state.get("case_id"),
        "submitter": expense.submitter,
        "amount": expense.amount,
        "category": expense.category,
        "date": expense.date,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with open(_LEDGER_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _generate_case_id() -> str:
    """產生唯一案件編號 EXP-YYYYMM-NNNN，counter 依當月 audit_log 行數決定。"""
    ym = datetime.now(timezone.utc).strftime("%Y%m")
    counter = 1
    if _AUDIT_LOG_PATH.exists():
        with open(_AUDIT_LOG_PATH, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        entry = json.loads(line)
                        cid = entry.get("case_id", "")
                        if cid.startswith(f"EXP-{ym}-"):
                            counter += 1
                    except Exception:
                        pass
    return f"EXP-{ym}-{counter:04d}"


def _append_audit_log(expense: ExpenseReport, ctx: Context) -> None:
    """把每筆最終結果寫入稽核日誌（audit_log.jsonl）。"""
    _AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fraud_flags: list = ctx.state.get("fraud_flags", [])
    case_id: str = ctx.state.get("case_id", "UNKNOWN")

    # 推斷 risk_level
    if expense.status == "APPROVED" and not fraud_flags:
        risk_level = "NONE"
    elif any("CRITICAL" in str(f) or "注入" in str(f) or "Injection" in str(f) for f in fraud_flags):
        risk_level = "CRITICAL"
    elif fraud_flags:
        risk_level = "HIGH"
    else:
        risk_level = "LOW"

    entry = {
        "case_id": case_id,
        "submitter": expense.submitter,
        "amount": expense.amount,
        "category": expense.category,
        "date": expense.date,
        "description": expense.description,   # 已遮蔽版（security_checkpoint 後）
        "invoice_no": expense.invoice_no,
        "vendor_name": expense.vendor_name,
        "vendor_tax_id": expense.vendor_tax_id,
        "fraud_flags": fraud_flags,
        "related_case_ids": ctx.state.get("related_case_ids", []),
        "risk_level": risk_level,
        "status": expense.status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with open(_AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[v] [AUDIT] {case_id} → {expense.status} (risk={risk_level}) appended to audit_log")


def _get_recent_purchases(submitter: str, window_days: int) -> list[dict]:
    """讀取 ledger，回傳同申報人 window_days 天內的歷史紀錄（不含本筆，因為尚未 append）。"""
    if not _LEDGER_PATH.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    results = []
    with open(_LEDGER_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("submitter") != submitter:
                    continue
                ts_str = entry.get("timestamp", "")
                ts = datetime.fromisoformat(ts_str)
                # 統一轉換成 aware datetime 再比較
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    results.append(entry)
            except Exception:
                continue
    return results


def _check_invoice_duplicate(invoice_no: str) -> Optional[dict]:
    """
    比對 audit_log.jsonl，回傳第一筆相同 invoice_no 的紀錄，找不到則回傳 None。
    隱私保護：呼叫方只取 date / case_id，不對外揭露原始申報人姓名。
    """
    if not invoice_no or not _AUDIT_LOG_PATH.exists():
        return None
    with open(_AUDIT_LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("invoice_no") == invoice_no:
                    return entry
            except Exception:
                continue
    return None


# ---------------------------------------------------------
# 節點 4：防弊稽核（純 Python）
# ---------------------------------------------------------
@node
def fraud_detector(ctx: Context, node_input: ExpenseReport) -> Event:
    """
    節點 4：防弊核心 — 以純 Python（100% 可靠，不依賴 LLM）執行四項檢查：
      1. 歇業廠商：統編優先比對 vendors.json
      2. 業務費超上限：比對 policy.json 各類別上限
      3. 出差浮報：天數浮報 + 住宿/雜費超上限
      4. 拆單規避招標：讀取跨筆 ledger，加總同申報人近期採購
    任一紅旗 → 路由到 human_approval；全部通過 → 依金額路由。
    """
    flags: list[str] = []

    try:
        policy = _load_policy()
    except Exception as e:
        print(f"[!] [FRAUD] policy.json 載入失敗: {e}，跳過政策檢查")
        policy = {}

    # --- 0. 重複發票偵測（Preventative Control）---
    if node_input.invoice_no:
        existing = _check_invoice_duplicate(node_input.invoice_no)
        if existing:
            flags.append(
                f"Duplicate Invoice: invoice_no '{node_input.invoice_no}' was already submitted "
                f"on {existing.get('date', 'unknown date')} "
                f"(Case ID: {existing.get('case_id', 'N/A')}). "
                f"The original submitter of this invoice is not disclosed to protect third-party privacy (ISO 27001 A.8.11). "
                f"If you submit despite this warning, the act is logged as intentional."
            )
            print(f"[!] [FRAUD] Duplicate invoice detected: {node_input.invoice_no}")

    # --- 1. 歇業廠商 ---
    if node_input.vendor_tax_id or node_input.vendor_name:
        try:
            vendors = _load_vendors()
            vendor = _find_vendor(vendors, node_input.vendor_tax_id, node_input.vendor_name)
            if vendor and vendor.get("status") in ("歇業", "註銷"):
                flags.append(
                    f"歇業廠商: {vendor['name']}（統編 {vendor['tax_id']}）"
                    f"狀態「{vendor['status']}」，不得向其請款"
                )
        except Exception as e:
            print(f"[!] [FRAUD] vendors.json 載入失敗: {e}")

    # --- 2. 業務費超上限 ---
    biz_limits: dict = policy.get("business_expense_limits", {})
    cat_policy = biz_limits.get(node_input.category)
    if isinstance(cat_policy, dict):
        max_limit = cat_policy.get("max")
        if max_limit is not None and node_input.amount > max_limit:
            flags.append(
                f"業務費超上限: 類別「{node_input.category}」"
                f"上限 NT$ {max_limit:,}，申報 NT$ {node_input.amount:,.0f}"
            )

    # --- 3. 出差浮報 ---
    travel: dict = policy.get("travel", {})
    if node_input.trip_days is not None and node_input.actual_trip_days is not None:
        if node_input.trip_days > node_input.actual_trip_days:
            flags.append(
                f"出差天數浮報: 申報 {node_input.trip_days} 天，"
                f"實際行程 {node_input.actual_trip_days} 天"
            )
    if node_input.hotel_per_night is not None:
        try:
            dt = datetime.strptime(node_input.date, "%Y-%m-%d")
            is_weekend = dt.weekday() in (4, 5)
        except Exception:
            is_weekend = False
            
        hotel_limit = travel.get("hotel_per_night_limit_weekend", 4500) if is_weekend else travel.get("hotel_per_night_limit_weekday", 3500)
        
        if node_input.hotel_per_night > hotel_limit:
            flags.append(
                f"住宿費超上限: 每晚 NT$ {node_input.hotel_per_night:,.0f}，"
                f"上限 NT$ {hotel_limit:,}"
            )
    if node_input.misc_per_day is not None:
        misc_limit = travel.get("misc_per_day_limit", 400)
        if node_input.misc_per_day > misc_limit:
            flags.append(
                f"雜費超上限: 每日 NT$ {node_input.misc_per_day:,.0f}，"
                f"上限 NT$ {misc_limit:,}"
            )

    # --- 4. 拆單規避招標 ---
    procurement: dict = policy.get("procurement", {})
    split_threshold = procurement.get("small_purchase_threshold", 150000)
    window_days = procurement.get("split_purchase_window_days", 7)

    if node_input.amount < split_threshold:
        # 先讀歷史（本筆尚未 append），再加總
        recent_past = _get_recent_purchases(node_input.submitter, window_days)
        qualifying_past = [r for r in recent_past if r["amount"] < split_threshold]
        past_total = sum(r["amount"] for r in qualifying_past)
        projected_total = past_total + node_input.amount

        if len(qualifying_past) >= 1 and projected_total >= split_threshold:
            related_case_ids = [r.get("case_id") for r in qualifying_past if r.get("case_id")]
            related_case_ids.append(ctx.state.get("case_id"))
            ctx.state["related_case_ids"] = related_case_ids
            
            flags.append(
                f"疑似拆單規避招標: {node_input.submitter} 近 {window_days} 天共 "
                f"{len(qualifying_past) + 1} 筆採購（含本筆），"
                f"各筆未達門檻（NT$ {split_threshold:,}），"
                f"加總 NT$ {projected_total:,.0f} >= NT$ {split_threshold:,}"
            )

    # 無論有無紅旗，都把本筆寫入 ledger（供後續拆單偵測用）
    try:
        _append_ledger(node_input, ctx)
    except Exception as e:
        print(f"[!] [FRAUD] ledger 寫入失敗: {e}")

    # --- 路由決策 ---
    if flags:
        print(f"[!] [FRAUD] 偵測到紅旗 ({len(flags)} 項): {flags}")
        risk = RiskAssessment(
            risk_level="HIGH",
            reasoning="防弊紅旗 | " + " | ".join(flags),
        )
        return Event(
            output=risk,
            route="human_approval",
            state={"expense": node_input.model_dump(), "fraud_flags": flags, "related_case_ids": ctx.state.get("related_case_ids", [])},
        )

    # 全部通過，依金額分流
    if node_input.amount >= THRESHOLD:
        print(f"[*] [FRAUD] 全部通過，金額 {node_input.amount} >= {THRESHOLD}，進 LLM 審核。")
        route = "llm_review"
    else:
        print(f"[*] [FRAUD] 全部通過，小額 {node_input.amount} < {THRESHOLD}，自動核准。")
        route = "auto_approve"

    return Event(output=node_input, route=route, state={"expense": node_input.model_dump()})


# 節點 5（原 4）：LLM 風險評估者 (只負責出嘴巴，不干涉流程)
# Node 5: LLM Risk Assessor (Outputs Pydantic schema, does not route flow)
risk_reviewer = LlmAgent(
    name="risk_reviewer",
    model=MODEL_NAME,
    instruction=(
        "You are a risk assessment auditor. Review the provided expense report "
        "for risk factors, policy violations, or suspicious activity. "
        "Output the risk level (Low, Medium, High) and a brief reasoning."
    ),
    output_schema=RiskAssessment,
)

@node(rerun_on_resume=True)
async def human_approval(ctx: Context, node_input: RiskAssessment) -> Event:
    """
    節點 5：人類介入 (HITL Node)。暫停流程並等待經理輸入核准或拒絕。
    Node 5: HITL Node. Pauses execution and asks a human manager to approve or reject.
    """
    # 如果還沒有拿到 resume_inputs，代表程式是第一次走到這裡，必須「凍結」並發出請求。
    if not ctx.resume_inputs:
        expense_dict = ctx.state.get("expense", {})
        
        print(
            f"[!] HUMAN APPROVAL REQUIRED [!]\n"
            f"Review expense for {expense_dict.get('submitter')} (${expense_dict.get('amount')})."
        )
        msg = (
            f"HUMAN APPROVAL REQUIRED\n"
            f"Submitter: {expense_dict.get('submitter')}\n"
            f"Amount: ${expense_dict.get('amount')}\n"
            f"Description: {expense_dict.get('description')}\n"
            f"----------------------------------\n"
            f"LLM Risk Assessment:\n"
            f"Level: {node_input.risk_level}\n"
            f"Reasoning: {node_input.reasoning}\n"
            f"----------------------------------\n"
            f"Do you approve this expense? (yes/no): "
        )
        
        # 關鍵魔法：yield RequestInput 會讓這個 Graph 進入睡眠狀態 (Suspend)，直到人類回覆
        yield RequestInput(interrupt_id="approval_decision", message=msg)
        return

    # 人類回覆後，系統會自動帶著資料從這裡「喚醒 (Resume)」
    decision = str(ctx.resume_inputs.get("approval_decision", "")).strip().lower()
    expense_dict = ctx.state.get("expense", {})
    expense = ExpenseReport(**expense_dict)
    
    if decision in ["yes", "y", "approve", "approved"]:
        expense.status = "APPROVED"
    else:
        expense.status = "REJECTED"
        
    yield Event(output=expense, state={"expense": expense.model_dump()})

@node
def record_outcome(ctx: Context, node_input: ExpenseReport) -> ExpenseReport:
    """
    節點 6：紀錄最終結果 (收斂節點 Fan-in)。
    Node 6: Final node. Records and prints the final outcome.
    """
    case_id = ctx.state.get("case_id", "UNKNOWN")
    print(f"\n[v] [OUTCOME RECORDED] [{case_id}] Expense for {node_input.submitter} (${node_input.amount}) is now: {node_input.status}\n")
    try:
        _append_audit_log(node_input, ctx)
    except Exception as e:
        print(f"[!] [AUDIT] audit_log 寫入失敗: {e}")
    return node_input

# ---------------------------------------------------------
# 有向圖邊界定義 (Graph Wiring)
# ---------------------------------------------------------
root_agent = Workflow(
    name="expense_workflow",
    edges=[
        # 流程起點
        ('START', parse_and_route),

        # parse → 資安檢查哨（PII + Prompt Injection）
        (parse_and_route, security_checkpoint),

        # 資安檢查哨分流
        (security_checkpoint, {
            "fraud_detector": fraud_detector,   # 乾淨請求 → 防弊核心
            "human_approval": human_approval,   # Prompt Injection → 直送人工
            "record_outcome": record_outcome,   # 已有狀態 → 直接收斂
        }),

        # 防弊核心分流
        (fraud_detector, {
            "auto_approve": auto_approve,       # 小額且無紅旗
            "llm_review": risk_reviewer,        # 大額且無紅旗 → LLM 審核
            "human_approval": human_approval,   # 有紅旗 → 直送人工
        }),

        # LLM 審核 → 人工確認
        (risk_reviewer, human_approval),

        # 最終收斂 (Fan-in)
        (human_approval, record_outcome),
        (auto_approve, record_outcome),
    ]
)

app = App(
    root_agent=root_agent,
    name="expense_agent",
)
