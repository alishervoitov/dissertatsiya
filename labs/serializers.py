from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from records.models import Patient

from .models import LabAttachment, LabResult, LabValue

MAX_VALUES = 60


class LabValueSerializer(serializers.ModelSerializer):
    flag = serializers.CharField(read_only=True)
    value = serializers.CharField(max_length=100)

    class Meta:
        model = LabValue
        fields = ["id", "name", "value", "unit", "ref_low", "ref_high", "ref_text", "flag"]
        read_only_fields = ["id", "flag"]

    def validate(self, attrs):
        low, high = attrs.get("ref_low"), attrs.get("ref_high")
        if low is not None and high is not None and low > high:
            raise serializers.ValidationError({"ref_high": "Me'yorning yuqori chegarasi pastkisidan kichik."})
        return attrs


class LabAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = LabAttachment
        fields = ["id", "original_filename", "content_type", "file_size", "uploaded_at"]


class LabResultSerializer(serializers.ModelSerializer):
    values = LabValueSerializer(many=True)
    attachments = LabAttachmentSerializer(many=True, read_only=True)
    created_by_name = serializers.SerializerMethodField()
    patient_name = serializers.SerializerMethodField()
    abnormal_count = serializers.SerializerMethodField()
    patient = serializers.PrimaryKeyRelatedField(queryset=Patient.objects.all())

    class Meta:
        model = LabResult
        fields = [
            "id", "patient", "patient_name", "panel", "title", "collected_at", "notes",
            "values", "attachments", "abnormal_count", "created_by_name", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_created_by_name(self, obj):
        return obj.created_by.get_full_name() or obj.created_by.username

    def get_patient_name(self, obj):
        u = obj.patient.user
        return u.get_full_name() or u.username

    def get_abnormal_count(self, obj):
        return sum(1 for v in obj.values.all() if v.flag in ("low", "high", "abnormal"))

    def validate_values(self, values):
        if not values:
            raise serializers.ValidationError("Kamida bitta ko'rsatkich kiriting.")
        if len(values) > MAX_VALUES:
            raise serializers.ValidationError(f"Ko'pi bilan {MAX_VALUES} ta ko'rsatkich.")
        return values

    def validate_collected_at(self, value):
        if value > timezone.now() + timedelta(minutes=10):
            raise serializers.ValidationError("Namuna olingan vaqt kelajakda bo'lishi mumkin emas.")
        return value

    def validate(self, attrs):
        # Tahrirlashda natijani boshqa bemorga ko'chirib bo'lmaydi
        if self.instance and "patient" in attrs and attrs["patient"].pk != self.instance.patient_id:
            raise serializers.ValidationError({"patient": "Natijani boshqa bemorga o'tkazib bo'lmaydi."})
        return attrs

    def _save_values(self, result, values):
        result.values.all().delete()
        LabValue.objects.bulk_create([LabValue(result=result, order=i, **v) for i, v in enumerate(values)])

    @transaction.atomic
    def create(self, validated_data):
        values = validated_data.pop("values")
        result = LabResult.objects.create(**validated_data)
        self._save_values(result, values)
        return result

    @transaction.atomic
    def update(self, instance, validated_data):
        values = validated_data.pop("values", None)
        for k, v in validated_data.items():
            setattr(instance, k, v)
        instance.save()
        if values is not None:
            self._save_values(instance, values)
        return instance


class LabPatientSerializer(serializers.ModelSerializer):
    """Laborant uchun bemorni aniqlashga yetarli minimal ma'lumot (tibbiy tarixsiz)."""

    full_name = serializers.SerializerMethodField()
    username = serializers.CharField(source="user.username")

    class Meta:
        model = Patient
        fields = ["id", "full_name", "username", "date_of_birth", "gender"]

    def get_full_name(self, obj):
        return obj.user.get_full_name() or obj.user.username
