# expense_agent/config.py

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

# Auto-approval threshold for expenses. Anything >= this value requires LLM review and human approval.
THRESHOLD = 100

# The LLM model to use for the risk_reviewer node.
MODEL_NAME = "gemini-3.1-flash-lite"
