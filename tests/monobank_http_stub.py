"""Synthetic GET-only provider fixture; never an application-selected adapter."""
from datetime import UTC, datetime
import json
from pathlib import Path
from uuid import uuid4

import httpx


class MonobankHTTPStub:
    def __init__(self, *, merchant_id="synthetic-merchant", token="synthetic-only", state_file=None):
        self.merchant_id, self.token = merchant_id, token
        self.state_file = Path(state_file) if state_file else None
        self.records = {}
        self.calls = []

    def register(self, reference, amount_minor, *, status="created", invoice_id=None):
        self._load()
        identifier = invoice_id or "mock_" + uuid4().hex
        now = datetime.now(UTC).isoformat()
        self.records[identifier] = {"invoiceId": identifier, "reference": reference,
            "amount": amount_minor, "finalAmount": amount_minor, "ccy": 980,
            "status": status, "createdDate": now, "modifiedDate": now, "cancelList": []}
        self._save()
        return identifier

    def update(self, invoice_id, **patch):
        self._load()
        self.records[invoice_id].update(patch)
        self.records[invoice_id]["modifiedDate"] = datetime.now(UTC).isoformat()
        self._save()

    def _load(self):
        if self.state_file and self.state_file.exists():
            self.records = json.loads(self.state_file.read_text())

    def _save(self):
        if self.state_file:
            self.state_file.touch(mode=0o600, exist_ok=True)
            self.state_file.chmod(0o600)
            self.state_file.write_text(json.dumps(self.records))

    def handle(self, request):
        self.calls.append((request.method, request.url.path))
        assert request.method == "GET", "R2 fixture permits no bank mutations"
        assert request.url.scheme == "https" and request.url.host == "api.monobank.ua"
        assert request.headers.get("X-Token") == self.token
        self._load()
        if request.url.path == "/api/merchant/details":
            return httpx.Response(200, json={"merchantId": self.merchant_id})
        assert request.url.path == "/api/merchant/invoice/status"
        record = self.records.get(request.url.params.get("invoiceId"))
        return httpx.Response(200, json=record) if record else httpx.Response(404, json={})
