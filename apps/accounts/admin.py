from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Clinic users (owner admin, doctors, staff) — login identity and role."""

    list_display = (
        "username",
        "email",
        "phone",
        "full_name",
        "role",
        "is_active",
        "is_staff",
        "is_superuser",
        "date_joined",
    )
    list_filter = DjangoUserAdmin.list_filter + ("role",)
    search_fields = ("username", "email", "full_name", "first_name", "last_name")
    ordering = ("username",)

    fieldsets = (
        (None, {"fields": ("username", "password")}),
        (
            "Personal",
            {"fields": ("email", "full_name", "phone", "first_name", "last_name")},
        ),
        ("Clinic", {"fields": ("role",)}),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                ),
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "username",
                    "password1",
                    "password2",
                    "email",
                    "full_name",
                    "role",
                ),
            },
        ),
    )
