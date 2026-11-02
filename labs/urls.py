from django.urls import path

from . import views

urlpatterns = [
    path("patients/", views.LabPatientSearchView.as_view(), name="lab-patient-search"),
    path("results/", views.LabResultListCreateView.as_view(), name="lab-results"),
    path("results/<int:pk>/", views.LabResultDetailView.as_view(), name="lab-result-detail"),
    path("results/<int:pk>/attachments/", views.LabAttachmentUploadView.as_view(), name="lab-attachment-upload"),
    path("attachments/<int:attachment_id>/", views.LabAttachmentDownloadView.as_view(), name="lab-attachment-download"),
]
