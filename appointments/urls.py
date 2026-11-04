from django.urls import path

from . import registry, views

urlpatterns = [
    path("doctors/", views.DoctorListView.as_view(), name="queue-doctors"),
    path("tickets/", views.TakeTicketView.as_view(), name="queue-take"),
    path("tickets/my/", views.MyTicketsView.as_view(), name="queue-my"),
    path("tickets/<int:pk>/arrive/", views.ArriveView.as_view(), name="queue-arrive"),
    path("tickets/<int:pk>/cancel/", views.CancelTicketView.as_view(), name="queue-cancel"),
    path("doctor/", views.DoctorQueueView.as_view(), name="queue-doctor"),
    path("doctor/count/", views.DoctorQueueCountView.as_view(), name="queue-doctor-count"),
    path("doctor/next/", views.DoctorCallNextView.as_view(), name="queue-doctor-next"),
    path("doctor/finish/", views.DoctorFinishView.as_view(), name="queue-doctor-finish"),
    path("doctor/tickets/<int:pk>/no-show/", views.DoctorNoShowView.as_view(), name="queue-doctor-noshow"),
    path("doctor/settings/", views.DoctorSettingsView.as_view(), name="queue-doctor-settings"),
    path("doctors/<int:doctor_id>/profile/", views.AdminDoctorProfileView.as_view(), name="queue-admin-doctor-profile"),
    path("registry/", views.RegistryTicketView.as_view(), name="queue-registry"),
    path("registry/patients/", registry.RegistryPatientView.as_view(), name="queue-registry-patients"),
    path("registry/tickets/<int:pk>/link/", registry.RegistryLinkTicketView.as_view(), name="queue-registry-link"),
    path("kiosk/doctors/", views.KioskDoctorListView.as_view(), name="queue-kiosk-doctors"),
    path("kiosk/checkin-code/", views.KioskCheckinCodeView.as_view(), name="queue-kiosk-checkin-code"),
    path("checkin/", views.CheckinView.as_view(), name="queue-checkin"),
    path("admin/stats/", views.AdminQueueStatsView.as_view(), name="queue-admin-stats"),
    path("admin/printed-checkin/", views.AdminPrintedCheckinView.as_view(), name="queue-admin-printed-checkin"),
    path("kiosk/tickets/", views.KioskTicketView.as_view(), name="queue-kiosk-take"),
]
