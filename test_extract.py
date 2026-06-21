import json
import base64
from typing import Any

def extract_expense_data(payload: Any) -> dict:
    print(f"[DEBUG] Raw payload type: {type(payload)}")
    print(f"[DEBUG] Raw payload repr: {repr(payload)}")

    if hasattr(payload, "output") and payload.output:
        payload = payload.output

    if isinstance(payload, tuple) and len(payload) > 0:
        payload = payload[0]
        print(f"[DEBUG] Unwrapped tuple, new type: {type(payload)}")
        print(f"[DEBUG] Unwrapped repr: {repr(payload)}")

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
        except Exception as e:
            print(f"[DEBUG] Conversion failed: {e}")
            pass

    print(f"[DEBUG] Parsed payload type: {type(payload)}")
    if not isinstance(payload, dict):
        return {}

    data = payload.get("data", payload)
    
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

payload_str = '{"amount": 150.0, "submitter": "alice@company.com", "category": "software", "description": "IDE License", "date": "2026-06-06"}'
res = extract_expense_data(payload_str)
print("Result:", res)

res_tuple = (payload_str,)
res2 = extract_expense_data(res_tuple)
print("Result 2:", res2)

