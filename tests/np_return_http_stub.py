"""Server-injected, synthetic-only NP read-only transport for R3 acceptance."""
from copy import deepcopy
import json

import httpx


class MockNPReturn:
    def __init__(self, rows):
        self.rows = deepcopy(rows)
        self.calls = []
        self.failure_status = None
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        assert request.method == 'POST'
        assert str(request.url) == 'https://api.novaposhta.ua/v2.0/json/'
        body = json.loads(request.content)
        assert set(body) == {'apiKey', 'modelName', 'calledMethod', 'methodProperties'}
        assert body['modelName'] == 'TrackingDocumentGeneral'
        assert body['calledMethod'] == 'getStatusDocuments'
        assert set(body['methodProperties']) == {'Documents'}
        documents = body['methodProperties']['Documents']
        assert len(documents) == 2
        assert all(set(document) == {'DocumentNumber'} for document in documents)
        numbers = [document['DocumentNumber'] for document in documents]
        assert len(set(numbers)) == 2
        self.calls.append({'original_ttn': numbers[0], 'return_ttn': numbers[1]})
        if self.failure_status:
            return httpx.Response(self.failure_status, json={'success': False, 'errors': ['synthetic failure']})
        return httpx.Response(200, json={'success': True, 'errors': [], 'data': [
            deepcopy(self.rows[number]) for number in numbers if number in self.rows]})
