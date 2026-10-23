"""Bound storefront JSON before FastAPI buffers or parses an untrusted body."""
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class ShopRequestSizeMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = 65536):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get('path', '')
        protected = any(path.startswith('/api/v1/public/' + part) for part in (
            'customers', 'orders', 'feedback', 'products', 'checkout-reminders',
        ))
        if scope['type'] != 'http' or scope['method'] not in {'POST', 'PATCH', 'PUT'} or not protected:
            await self.app(scope, receive, send)
            return
        response = JSONResponse({'detail': 'Request body exceeds 64 KiB'}, status_code=413)
        headers = dict(scope.get('headers', []))
        try:
            declared = int(headers.get(b'content-length', b'0'))
        except ValueError:
            declared = 0
        if declared > self.max_bytes:
            await response(scope, receive, send)
            return
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            size += len(chunk)
            if size > self.max_bytes:
                await response(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get('more_body', False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
            return await receive()

        await self.app(scope, replay, send)
