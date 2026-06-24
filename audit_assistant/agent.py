import os
from pathlib import Path
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.tools import ToolContext
from google.genai import types as genai_types

from expense_agent.report import generate_report
from expense_agent.config import MODEL_NAME

_AUDIT_LOG_PATH = Path(os.environ.get("AUDIT_LOG_PATH", Path(__file__).parent.parent / "tests" / "demo_audit_log.jsonl"))

async def generate_audit_report(month: str, tool_context: ToolContext) -> dict:
    """
    產生指定月份的核銷稽核月報 (PPTX 格式)，並將報告儲存至 Artifacts。
    當使用者要求產生「核銷月報」、「稽核報告」時，請呼叫此工具並提供月份。
    
    Args:
        month: 報表月份，格式為 'YYYY-MM' (例如 '2026-06')
        
    Returns:
        dict: 包含執行結果狀態與訊息
    """
    try:
        if not _AUDIT_LOG_PATH.exists():
            return {"status": "error", "message": f"找不到資料來源檔案: {_AUDIT_LOG_PATH}"}
            
        # 呼叫 report.py 既有邏輯
        out_path = generate_report(log_path=_AUDIT_LOG_PATH, month=month)
        
        with open(out_path, "rb") as f:
            content = f.read()
            
        # 存成 ADK Artifact
        await tool_context.save_artifact(
            out_path.name,
            genai_types.Part.from_bytes(
                data=content,
                mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation"
            )
        )
        return {"status": "success", "message": f"報告已成功產生並儲存至 Artifacts，檔名為 {out_path.name}"}
    except Exception as e:
        return {"status": "error", "message": f"產生報告失敗: {str(e)}"}

audit_assistant = LlmAgent(
    name="audit_assistant",
    description="產生並提供月底核銷稽核報告",
    instruction="當使用者要求產生核銷/稽核月報時,呼叫 generate_audit_report。這是一個唯讀助理。",
    tools=[generate_audit_report],
    model=MODEL_NAME
)

assistant_app = App(
    root_agent=audit_assistant,
    name="audit_assistant",
)

# ADK 以資料夾探索 app 時會找模組層級的 root_agent
root_agent = audit_assistant
