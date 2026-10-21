"""Shared public-router admission before validation or business work."""
from fastapi import Request

from app.services.shop_ingress_limits import admit_shop_ingress


async def guard_shop_input(request: Request) -> None:
    path = request.url.path.split('/public', 1)[-1].rstrip('/')
    method = request.method
    if method == 'GET':
        if path.startswith(('/products', '/categories', '/brands', '/delivery')):
            await admit_shop_ingress(request, 'search')
    elif path.startswith('/feedback'):
        await admit_shop_ingress(request, 'feedback')
    elif path.startswith('/products') and '/reviews' in path:
        await admit_shop_ingress(request, 'review')
    elif path == '/customers/me' and method == 'PATCH':
        await admit_shop_ingress(request, 'profile')
    elif path.startswith(('/customers/cart', '/customers/wishlist')):
        await admit_shop_ingress(request, 'cart')


from typing import Annotated
from pydantic import AfterValidator
from app.utils.shop_input import plain_text

SearchText = Annotated[str, AfterValidator(lambda value: plain_text(value, 120))]
