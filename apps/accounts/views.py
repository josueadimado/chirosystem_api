import logging

from django.db import transaction
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from .login_email_otp import (
    create_login_challenge,
    mask_email,
    send_login_code_email,
    should_send_login_otp,
    verify_login_challenge,
)
from .models import User
from .permissions import IsOwnerAdmin
from .serializers import (
    ClinicTeamMemberSerializer,
    LoginVerifySerializer,
    RegisterSerializer,
    RoleTokenSerializer,
    UserSerializer,
)
from .team_helpers import set_provider_inactive

logger = logging.getLogger(__name__)


class AuthViewSet(viewsets.GenericViewSet):
    queryset = User.objects.all()

    def get_permissions(self):
        # login_verify must be anonymous — user only has password + emailed code, no JWT yet.
        if self.action in ("register", "login", "login_verify"):
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
        if should_send_login_otp(user):
            try:
                vtoken, code = create_login_challenge(user)
                send_login_code_email(user=user, code=code)
            except Exception:
                logger.exception("Failed to send login verification email for user %s", user.pk)
                return Response(
                    {"detail": "Could not send verification email. Check EMAIL_* settings on the server."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            return Response(
                {
                    "verification_required": True,
                    "email_masked": mask_email(user.email),
                    "verification_token": vtoken,
                },
                status=status.HTTP_200_OK,
            )
        return Response(
            {
                **serializer.validated_data,
                "user": UserSerializer(user).data,
            }
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="login/verify",
        permission_classes=[permissions.AllowAny],
    )
    def login_verify(self, request):
        ser = LoginVerifySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = verify_login_challenge(
            ser.validated_data["verification_token"],
            ser.validated_data["code"],
        )
        if not user:
            return Response(
                {"detail": "Invalid or expired code. Request a new code by signing in again."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        refresh = RoleTokenSerializer.get_token(user)
        return Response(
            {
                "refresh": str(refresh),
                "access": str(refresh.access_token),
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
