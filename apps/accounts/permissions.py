from rest_framework import permissions


class IsOwnerAdmin(permissions.BasePermission):
    """Only clinic owner admins (or Django superusers) may manage the clinic team."""

    message = "Only owner administrators can manage team members."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if getattr(user, "is_superuser", False):
            return True
        return getattr(user, "role", None) == "owner_admin"
