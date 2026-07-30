from django.urls import path

from . import views

urlpatterns = [
    path("patients/", views.PatientListView.as_view(), name="patient-list"),
    path("patients/me/", views.MyPatientProfileView.as_view(), name="patient-me"),
    path("patients/<int:pk>/", views.PatientDetailView.as_view(), name="patient-detail"),
    path("medical-records/", views.MedicalRecordListCreateView.as_view(), name="record-list-create"),
    path("medical-records/<int:pk>/", views.MedicalRecordDetailView.as_view(), name="record-detail"),
    path("audit-logs/", views.AuditLogListView.as_view(), name="audit-log-list"),
]