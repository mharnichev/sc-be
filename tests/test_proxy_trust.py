"""The actual pinned Uvicorn middleware; no socket or external proxy."""
import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from app.core.proxy_trust import validate_proxy_allowlist

@pytest.fixture
def anyio_backend():return 'asyncio'

@pytest.mark.anyio
@pytest.mark.parametrize('peer,forwarded,expected',[
    ('203.0.113.10','198.51.100.7','203.0.113.10'),
    ('127.0.0.1','198.51.100.7','198.51.100.7'),
    ('10.20.30.2','198.51.100.7, 10.20.30.3','198.51.100.7'),
])
async def test_actual_proxy_boundary_matches_validated_networks(peer,forwarded,expected):
    allow='127.0.0.1,10.20.30.0/24';validate_proxy_allowlist(allow);observed=[]
    async def inner(scope,receive,send):observed.append(scope['client'][0])
    async def receive():return {'type':'http.disconnect'}
    async def send(message):pass
    app=ProxyHeadersMiddleware(inner,trusted_hosts=allow)
    await app({'type':'http','client':(peer,1234),'scheme':'http','headers':[(b'x-forwarded-for',forwarded.encode())]},receive,send)
    assert observed==[expected]
