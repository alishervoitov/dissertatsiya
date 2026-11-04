from django.db.models import Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from records.audit import log_audit
from records.models import Patient
from users.models import Role

from .models import LabAttachment, LabResult
from .permissions import IsLab, can_edit_result, can_read_result
from .serializers import LabAttachmentSerializer, LabPatientSerializer, LabResultSerializer

# Fayl turi mijoz yuborgan Content-Type bo'yicha emas, faylning o'z imzosi bo'yicha aniqlanadi
from records.filetypes import detect_type as _sniff  # noqa: E402


def _results_qs():
    return LabResult.objects.select_related("patient__user", "created_by").prefetch_related("values", "attachments")


class LabPatientSearchView(generics.ListAPIView):
    """Laborant bemorni ismi yoki login bo'yicha topadi. Ro'yxatni to'liq ochmaslik uchun kamida 2 belgi."""

    serializer_class = LabPatientSerializer
    permission_classes = [IsLab]
    pagination_class = None

    def get_queryset(self):
        q = (self.request.query_params.get("search") or "").strip()
        if len(q) < 2:
            return Patient.objects.none()
        cond = Q(user__username__icontains=q) | Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q)
        parts = q.split()
        if len(parts) >= 2:
            cond |= Q(user__first_name__icontains=parts[0], user__last_name__icontains=parts[1])
            cond |= Q(user__first_name__icontains=parts[1], user__last_name__icontains=parts[0])
        return Patient.objects.select_related("user").filter(cond, user__is_active=True).order_by("user__first_name")[:20]


class LabResultListCreateView(APIView):
    def get(self, request):
        user = request.user
        qs = _results_qs()
        patient_id = request.query_params.get("patient")
        if user.role == Role.PATIENT:
            qs = qs.filter(patient__user=user)
        elif user.role in (Role.DOCTOR, Role.ADMIN):
            if not patient_id:
                return Response({"detail": "patient parametri kerak."}, status=status.HTTP_400_BAD_REQUEST)
            if user.role == Role.DOCTOR:
                from records.consent import require_access
                require_access(user, int(patient_id) if str(patient_id).isdigit() else -1)
            qs = qs.filter(patient_id=patient_id)
        elif user.role == Role.LAB:
            qs = qs.filter(patient_id=patient_id) if patient_id else qs.filter(created_by=user)[:50]
        else:
            return Response(status=status.HTTP_403_FORBIDDEN)

        results = list(qs)
        patient = results[0].patient if results and (patient_id or user.role == Role.PATIENT) else None
        if patient is None and patient_id:
            patient = Patient.objects.filter(pk=patient_id).first()
        if patient is not None:
            log_audit(user, "view", patient=patient, detail="Tahlil natijalari ko'rildi")
        return Response(LabResultSerializer(results, many=True).data)

    def post(self, request):
        if request.user.role != Role.LAB:
            return Response({"detail": "Natijani faqat laborant kiritadi."}, status=status.HTTP_403_FORBIDDEN)
        s = LabResultSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        result = s.save(created_by=request.user)
        log_audit(request.user, "create", patient=result.patient, detail=f"Tahlil natijasi kiritildi: {result.title}")
        result = _results_qs().get(pk=result.pk)
        from notifications.services import notify_lab_ready
        notify_lab_ready(result)
        return Response(LabResultSerializer(result).data, status=status.HTTP_201_CREATED)


class LabResultDetailView(APIView):
    def _get(self, request, pk):
        result = get_object_or_404(_results_qs(), pk=pk)
        if not can_read_result(request.user, result):
            # Mavjudligini oshkor qilmaslik uchun 404
            return None
        return result

    def get(self, request, pk):
        result = self._get(request, pk)
        if result is None:
            return Response({"detail": "Topilmadi."}, status=status.HTTP_404_NOT_FOUND)
        log_audit(request.user, "view", patient=result.patient, detail=f"Tahlil natijasi ko'rildi: {result.title}")
        return Response(LabResultSerializer(result).data)

    def patch(self, request, pk):
        result = self._get(request, pk)
        if result is None:
            return Response({"detail": "Topilmadi."}, status=status.HTTP_404_NOT_FOUND)
        if not can_edit_result(request.user, result):
            return Response({"detail": "Natijani faqat uni kiritgan laborant o'zgartiradi."}, status=status.HTTP_403_FORBIDDEN)
        s = LabResultSerializer(result, data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        s.save()
        log_audit(request.user, "update", patient=result.patient, detail=f"Tahlil natijasi tahrirlandi: {result.title}")
        return Response(LabResultSerializer(_results_qs().get(pk=pk)).data)

    def delete(self, request, pk):
        result = self._get(request, pk)
        if result is None:
            return Response({"detail": "Topilmadi."}, status=status.HTTP_404_NOT_FOUND)
        if not can_edit_result(request.user, result):
            return Response({"detail": "Natijani faqat uni kiritgan laborant o'chiradi."}, status=status.HTTP_403_FORBIDDEN)
        log_audit(request.user, "delete", patient=result.patient, detail=f"Tahlil natijasi o'chirildi: {result.title}")
        for a in result.attachments.all():
            a.file.delete(save=False)
        result.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class LabAttachmentUploadView(APIView):
    parser_classes = [MultiPartParser]

    def post(self, request, pk):
        result = get_object_or_404(LabResult.objects.select_related("patient"), pk=pk)
        if not can_edit_result(request.user, result):
            return Response({"detail": "Faylni faqat natijani kiritgan laborant biriktiradi."}, status=status.HTTP_403_FORBIDDEN)
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Fayl topilmadi."}, status=status.HTTP_400_BAD_REQUEST)
        if f.size > LabAttachment.MAX_SIZE_BYTES:
            return Response({"detail": "Fayl hajmi 10 MB dan oshmasligi kerak."}, status=status.HTTP_400_BAD_REQUEST)
        real_type = _sniff(f)
        if real_type not in LabAttachment.ALLOWED_CONTENT_TYPES:
            return Response({"detail": "Faqat PDF, JPEG, PNG yoki WEBP fayl qabul qilinadi."}, status=status.HTTP_400_BAD_REQUEST)
        att = LabAttachment.objects.create(
            result=result, file=f, original_filename=f.name[:255], content_type=real_type,
            file_size=f.size, uploaded_by=request.user,
        )
        log_audit(request.user, "create", patient=result.patient, detail=f"Tahlilga fayl biriktirildi: {att.original_filename}")
        return Response(LabAttachmentSerializer(att).data, status=status.HTTP_201_CREATED)


class LabAttachmentDownloadView(APIView):
    def get(self, request, attachment_id):
        att = get_object_or_404(LabAttachment.objects.select_related("result__patient"), pk=attachment_id)
        if not can_read_result(request.user, att.result):
            return Response({"detail": "Topilmadi."}, status=status.HTTP_404_NOT_FOUND)
        log_audit(request.user, "view", patient=att.result.patient, detail=f"Tahlil fayli yuklab olindi: {att.original_filename}")
        return FileResponse(att.file.open("rb"), as_attachment=True, filename=att.original_filename,
                            content_type=att.content_type)
