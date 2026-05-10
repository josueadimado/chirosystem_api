"""Shared DRF pagination classes."""

from rest_framework.pagination import PageNumberPagination


class StandardPageNumberPagination(PageNumberPagination):
    """Paginated list responses: `{ count, next, previous, results }`."""

    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100
