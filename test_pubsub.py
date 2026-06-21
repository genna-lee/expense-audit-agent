import base64
import json
import urllib.request
from urllib.error import HTTPError

expense = {
    'amount': 95.0, 
    'submitter': 'ambient@company.com', 
    'category': 'software', 
    'description': 'Ambient Test', 
    'date': '2026-06-20'
}

encoded = base64.b64encode(json.dumps(expense).encode()).decode()

payload = {
    'message': {
        'data': encoded, 
        'messageId': 'MSG123'
    }, 
    'subscription': 'projects/my-proj/subscriptions/expense-topic-sub'
}

req = urllib.request.Request(
    'http://127.0.0.1:8080/pubsub', 
    data=json.dumps(payload).encode(), 
    headers={'Content-Type': 'application/json'}
)

try:
    res = urllib.request.urlopen(req)
    print("Success:", res.read().decode())
except HTTPError as e:
    print("HTTPError:", e.code)
    print(e.read().decode())
