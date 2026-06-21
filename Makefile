.PHONY: install playground report seed-report generate-traces grade

install:
	uv sync

# ⚠️ 嚴重警告 (CRITICAL WARNING)：
# 絕對不要使用 `uv run adk web .` 來啟動伺服器！
# 使用 `.` 會讓 ADK 拿外部資料夾名稱 (ambient-expense-agent) 當作 App Name，
# 但真實的 Python 程式碼位在內部資料夾 (expense_agent)。
# 這會導致名稱不匹配，前端 Web UI 按下送出時必定發生 `Session not found` 的致命錯誤。
# 請永遠明確指定後方真實的模組資料夾名稱 (如：expense_agent)。
playground:
	uv run uvicorn expense_agent.fast_api_app:app --host 127.0.0.1 --port 8080 --reload --env-file .env

serve:
	uv run uvicorn expense_agent.fast_api_app:app --host 0.0.0.0 --port 8080 --env-file .env

generate-traces:
	uv run python tests/eval/generate_traces.py

grade:
	uv run adk eval run --traces artifacts/traces/generated_traces.json --config tests/eval/eval_config.yaml

# ── 月底稽核報告 ──────────────────────────────────────────
# 讀正式 audit_log 產報告（當月）
report:
	uv run python -m expense_agent.report

# 先產 demo 資料，再用 demo log 產 2026-06 示範報告
seed-report:
	uv run python expense_agent/seed_audit_log.py
	uv run python -m expense_agent.report --log tests/demo_audit_log.jsonl --month 2026-06 --out reports/
