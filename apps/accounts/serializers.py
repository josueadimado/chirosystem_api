from django.db import transaction
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import User
from .team_helpers import apply_role_flags, ensure_provider_for_doctor, set_provider_inactive


class ClinicTeamMemberSerializer(serializers.ModelSerializer):
    """
    Manage clinic team: owner administrators, doctors, and staff.

    Password is write-only. `role` must be `owner_admin`, `doctor`, or `staff`.
    Doctors get a linked `Provider` row for scheduling and booking.
    """

    password = serializers.CharField(write_only=True, min_length=8, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "full_name",
            "role",
            "is_active",
            "date_joined",
            "password",
        )
        read_only_fields = ("id", "date_joined")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance is not None:
            self.fields["username"].read_only = True

    def validate_role(self, value):
        allowed = {
            User.Roles.OWNER_ADMIN,
            User.Roles.DOCTOR,
            User.Roles.STAFF,
        }
        if value not in allowed:
            raise serializers.ValidationError(
                "Role must be one of: owner_admin, doctor, staff."
            )
        return value

    def validate(self, attrs):
        if self.instance is None:
            pw = (attrs.get("password") or "").strip()
            if not pw:
                raise serializers.ValidationError(
                    {"password": "This field is required when creating a team member."}
                )
        inst = self.instance
        if inst is not None and inst.role == User.Roles.OWNER_ADMIN:
            effective_active = attrs.get("is_active", inst.is_active)
            effective_role = attrs.get("role", inst.role)
            leaving_owner = (
                effective_role != User.Roles.OWNER_ADMIN or effective_active is False
            )
            if leaving_owner:
                others = (
                    User.objects.filter(
                        role=User.Roles.OWNER_ADMIN,
                        is_active=True,
                    )
                    .exclude(pk=inst.pk)
                    .count()
                )
                if others < 1:
                    raise serializers.ValidationError(
                        "There must always be at least one active owner administrator."
                    )
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        apply_role_flags(user, user.role)
        user.set_password(password)
        with transaction.atomic():
            user.save()
            if user.role == User.Roles.DOCTOR:
                ensure_provider_for_doctor(user)
        return user

    def update(self, instance, validated_data):
        old_role = instance.role
        password = validated_data.pop("password", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        apply_role_flags(instance, instance.role)
        if password:
            instance.set_password(password)
        with transaction.atomic():
            instance.save()
            if old_role != User.Roles.DOCTOR and instance.role == User.Roles.DOCTOR:
                ensure_provider_for_doctor(instance)
            elif old_role == User.Roles.DOCTOR and instance.role != User.Roles.DOCTOR:
                set_provider_inactive(instance)
            elif instance.role == User.Roles.DOCTOR:
                if instance.is_active:
                    ensure_provider_for_doctor(instance)
                else:
                    set_provider_inactive(instance)
        return instance


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "username", "email", "full_name", "role")


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ("username", "email", "full_name", "role", "password")

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user


class RoleTokenSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["full_name"] = user.full_name
        return token
