from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Query, status


class PaginationParams:
    def __init__(self, page: int = 1, page_size: int = 20) -> None:
        self.page = page
        self.page_size = page_size


def normalize_optional_query(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or normalized.lower() in {"undefined", "null"}:
        return None
    return normalized


def parse_optional_int_query(value: str | None, field_name: str) -> int | None:
    normalized = normalize_optional_query(value)
    if normalized is None:
        return None
    try:
        return int(normalized)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid integer value for '{field_name}'",
        ) from exc


def parse_optional_bool_query(value: str | None, field_name: str) -> bool | None:
    normalized = normalize_optional_query(value)
    if normalized is None:
        return None
    lowered = normalized.lower()
    if lowered in {"true", "1"}:
        return True
    if lowered in {"false", "0"}:
        return False
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"Invalid boolean value for '{field_name}'",
    )


def get_pagination_params(
    page: str | None = Query(default=None),
    page_size: str | None = Query(default=None),
) -> PaginationParams:
    parsed_page = parse_optional_int_query(page, "page") or 1
    parsed_page_size = parse_optional_int_query(page_size, "page_size") or 20
    if parsed_page < 1:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="'page' must be >= 1")
    if parsed_page_size < 1 or parsed_page_size > 100:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'page_size' must be between 1 and 100",
        )
    return PaginationParams(page=parsed_page, page_size=parsed_page_size)


PaginationDep = Annotated[PaginationParams, Depends(get_pagination_params)]
