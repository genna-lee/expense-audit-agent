# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os


from fastapi import FastAPI
import base64
import json
import logging
from pydantic import BaseModel
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.cli.utils.agent_loader import AgentLoader
from google.adk.runners import InMemoryRunner

from expense_agent.app_utils.telemetry import setup_telemetry
from expense_agent.app_utils.typing import Feedback
from expense_agent.agent import root_agent

class FilteredAgentLoader(AgentLoader):
    def list_agents(self) -> list[str]:
        return [a for a in super().list_agents() if a not in ("artifacts", "reports", "tests", "frontend")]

setup_telemetry()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

# Artifact bucket for ADK (created by Terraform, passed via env var)
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# In-memory session configuration - no persistent storage
session_service_uri = None

artifact_service_uri = f"gs://{logs_bucket_name}" if logs_bucket_name else None

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    agent_loader=FilteredAgentLoader(AGENT_DIR),
    web=True,
    artifact_service_uri=artifact_service_uri,
    allow_origins=allow_origins,
    session_service_uri=session_service_uri,
    otel_to_cloud=False,
    trigger_sources=["pubsub"],
)
app.title = "ambient-expense-agent"
app.description = "API for interacting with the Agent ambient-expense-agent"


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    logger.info(f"Feedback received: {feedback.model_dump()}")
    return {"status": "success"}

# Move the newly added /feedback route to the front so it isn't shadowed by the SPA catch-all
app.router.routes.insert(0, app.router.routes.pop()) # moves /feedback

# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
