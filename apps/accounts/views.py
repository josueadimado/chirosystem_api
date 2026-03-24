from django.db import transaction
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from .models import User
from .permissions import IsOwnerAdmin
from .serializers import (
    ClinicTeamMemberSerializer,
    RegisterSerializer,
    RoleTokenSerializer,
    UserSerializer,
)
from .team_helpers import set_provider_inactive


class AuthViewSet(viewsets.GenericViewSet):
    queryset = User.objects.all()

    def get_permissions(self):
        if self.action in ("register", "login"):
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated()]

    @action(detail=False, methods=["post"])
    def register(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], permission_classes=[permissions.AllowAny])
    def login(self, request):
        serializer = RoleTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.user
        return Response(
            {
                **serializer.validated_data,
                "user": UserSerializer(user).data,
            }
        )

    @action(detail=False, methods=["get"])
    def me(self, request):
        return Response(UserSerializer(request.user).data)


class TeamViewSet(viewsets.ModelViewSet):
    """
    Manage clinic team: **owner administrators**, **doctors**, and **staff**.

    **Who can access:** owner administrators (or Django superusers) only.

    - **Doctors** automatically get a `Provider` profile (scheduling / booking).
    - **DELETE:** soft-deactivates the user (`is_active=False`). Use PATCH to reactivate.
    - You cannot deactivate yourself or the last active owner administrator.
    """

    queryset = (
        User.objects.filter(
            role__in=[
                User.Roles.OWNER_ADMIN,
                User.Roles.DOCTOR,
                User.Roles.STAFF,
            ]
        )
        .order_by("username")
    )
    serializer_class = ClinicTeamMemberSerializer
    permission_classes = [IsOwnerAdmin]
    filter_backends = [filters.SearchFilter]
    search_fields = ("username", "email", "full_name")

    def perform_destroy(self, instance):
        if instance.pk == self.request.user.pk:
            raise PermissionDenied("You cannot deactivate your own account.")
        if instance.role == User.Roles.OWNER_ADMIN:
            others = (
                User.objects.filter(
                    role=User.Roles.OWNER_ADMIN,
                    is_active=True,
                )
                .exclude(pk=instance.pk)
                .count()
            )
            if others < 1:
                raise PermissionDenied("Cannot deactivate the last owner administrator.")
        with transaction.atomic():
            instance.is_active = False
            instance.save(update_fields=["is_active"])
            if instance.role == User.Roles.DOCTOR:
                set_provider_inactive(instance)
