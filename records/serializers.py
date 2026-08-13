from rest_framework import serializers
from users.serializers import UserPublicSerializer
from .models import AuditLog, MedicalRecord, Patient
from .models import RecordAttachment

class PatientSerializer(serializers.ModelSerializer):
    user = UserPublicSerializer(read_only=True)
    primary_doctor_name = serializers.SerializerMethodField()
    records_count = serializers.SerializerMethodField()

    class Meta:
        model = Patient
        fields = [
            "id", "user", "date_of_birth", "gender", "national_id", "address",
            "emergency_contact", "blood_type", "allergies", "primary_doctor",
            "primary_doctor_name", "records_count", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_primary_doctor_name(self, obj):
        return obj.primary_doctor.get_full_name() if obj.primary_doctor else None

    def get_records_count(self, obj):
        return obj.records.count()


class PatientListSerializer(serializers.ModelSerializer):
    """Ro'yxat sahifasi uchun yengilroq serializer."""

    full_name = serializers.CharField(source="user.get_full_name", read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = Patient
        fields = ["id", "full_name", "username", "gender", "date_of_birth", "blood_type"]


class MedicalRecordSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True)
    patient_name = serializers.CharField(source="patient.user.get_full_name", read_only=True)

    class Meta:
        model = MedicalRecord
        fields = [
            "id", "patient", "patient_name", "created_by", "created_by_name",
            "record_type", "title", "diagnosis", "treatment", "notes",
            "visit_date", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]


class AuditLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source="actor.get_full_name", read_only=True)
    patient_name = serializers.CharField(source="patient.user.get_full_name", read_only=True)

    class Meta:
        model = AuditLog
        fields = [
            "id", "actor", "actor_name", "action", "patient", "patient_name",
            "medical_record", "detail", "ip_address", "user_agent", "timestamp",
        ]



class RecordAttachmentSerializer(serializers.ModelSerializer):
    uploaded_by_name = serializers.CharField(source="uploaded_by.get_full_name", read_only=True)

    class Meta:
        model = RecordAttachment
        fields = [
            "id", "record", "original_filename", "content_type",
            "file_size", "uploaded_by_name", "uploaded_at",
        ]
        read_only_fields = ["id", "content_type", "file_size", "uploaded_by_name", "uploaded_at"]