from dataclasses import dataclass
from typing import Any

@dataclass
class Event:
    output: Any

payload = (Event(output={'amount': 150.0}),)

def extract_expense_data_old(payload: Any) -> dict:
    if hasattr(payload, "output") and payload.output:
        payload = payload.output

    if isinstance(payload, tuple) and len(payload) > 0:
        payload = payload[0]

    if not isinstance(payload, dict):
        return {}
    return payload

def extract_expense_data_new(payload: Any) -> dict:
    if isinstance(payload, tuple) and len(payload) > 0:
        payload = payload[0]

    if hasattr(payload, "output") and payload.output:
        payload = payload.output

    if not isinstance(payload, dict):
        return {}
    return payload

print("Old:", extract_expense_data_old(payload))
print("New:", extract_expense_data_new(payload))
