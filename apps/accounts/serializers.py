from django.db import transaction
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.clinic.models import Service
from apps.clinic.utils import validate_phone

from .models import User
from .team_helpers import apply_role_flags, ensure_provider_for_doctor, set_provider_inactive


class ClinicTeamMemberSerializer(serializers.ModelSerializer):
    """
    Manage clinic team: owner administrators, doctors, and staff.

    Password is write-only. `role` must be `owner_admin`, `doctor`, or `staff`.
    Doctors get a linked `Provider` row for scheduling and booking.

    `doctor_booking_category` — `chiropractic` or `massage` (doctors only): which public visit types they appear under.
    """

    password = serializers.CharField(write_only=True, min_length=8, required=False, allow_blank=True)
    doctor_booking_category = serializers.ChoiceField(
        choices=[Service.ServiceType.CHIROPRACTIC, Service.ServiceType.MASSAGE],
        required=False,
        write_only=True,
    )

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "full_name",
            "phone",
            "role",
            "is_active",
            "date_joined",
            "password",
            "doctor_booking_category",
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

    def validate_phone(self, value):
        raw = (value or "").strip()
        if not raw:
            return ""
        ok, result = validate_phone(raw)
        if not ok:
            raise serializers.ValidationError(result)
        return result

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

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["doctor_booking_category"] = None
        if instance.role == User.Roles.DOCTOR:
            try:
                ret["doctor_booking_category"] = instance.provider.primary_service_type
            except Exception:
                ret["doctor_booking_category"] = Service.ServiceType.CHIROPRACTIC
        return ret

    def create(self, validated_data):
        password = validated_data.pop("password")
        doctor_cat = validated_data.pop("doctor_booking_category", None)
        user = User(**validated_data)
        apply_role_flags(user, user.role)
        user.set_password(password)
        with transaction.atomic():
            user.save()
            if user.role == User.Roles.DOCTOR:
                pst = doctor_cat or Service.ServiceType.CHIROPRACTIC
                ensure_provider_for_doctor(user, primary_service_type=pst)
        return user

    def update(self, instance, validated_data):
        old_role = instance.role
        password = validated_data.pop("password", None)
        doctor_cat = validated_data.pop("doctor_booking_category", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        apply_role_flags(instance, instance.role)
        if password:
            instance.set_password(password)
        with transaction.atomic():
            instance.save()
            if old_role != User.Roles.DOCTOR and instance.role == User.Roles.DOCTOR:
                ensure_provider_for_doctor(
                    instance,
                    primary_service_type=doctor_cat or Service.ServiceType.CHIROPRACTIC,
                )
            elif old_role == User.Roles.DOCTOR and instance.role != User.Roles.DOCTOR:
                set_provider_inactive(instance)
            elif instance.role == User.Roles.DOCTOR:
                if instance.is_active:
                    try:
                        current_pst = instance.provider.primary_service_type
                    except Exception:
                        current_pst = Service.ServiceType.CHIROPRACTIC
                    pst = doctor_cat if doctor_cat is not None else current_pst
                    ensure_provider_for_doctor(instance, primary_service_type=pst)
                else:
                    set_provider_inactive(instance)
        return instance


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "username", "email", "full_name", "phone", "role")


class LoginVerifySerializer(serializers.Serializer):
    verification_token = serializers.CharField()
    code = serializers.CharField(min_length=6, max_length=8, trim_whitespace=True)


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

    def validate(self, attrs):
        """
        Accept either Django username or email (case-insensitive email match).
        Username is tried first; if no active user matches, we resolve by email.
        """
        raw = (attrs.get("username") or "").strip()
        if raw:
            user = User.objects.filter(username__iexact=raw, is_active=True).first()
            if user is None:
                user = User.objects.filter(email__iexact=raw, is_active=True).first()
            if user is not None:
                attrs = {**attrs, "username": user.username}
        return super().validate(attrs)
