from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query


class PaginationParams:
    def __init__(self, page: int = 1, page_size: int = 20) -> None:
        self.page = page
        self.page_size = page_size


def get_pagination_params(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> PaginationParams:
    return PaginationParams(page=page, page_size=page_size)


PaginationDep = Annotated[PaginationParams, Depends(get_pagination_params)]
