"""Lightweight health check for Docker / reverse proxy (no auth)."""

from django.http import JsonResponse


def health(_request):
    return JsonResponse({"status": "ok"})
