"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from django.conf import settings
from django.conf.urls.static import static

from users.admin_login import MfaAdminAuthenticationForm

# Django admin ham 2FA ortida: parolning o'zi yetmaydi (users/admin_login.py)
admin.site.login_form = MfaAdminAuthenticationForm
admin.site.login_template = "admin/mfa_login.html"
admin.site.site_header = "ShifoSafe - texnik boshqaruv"

urlpatterns = [
    path("api/auth/", include("users.urls")),
    path("api/records/", include("records.urls")),
    path("api/queue/", include("appointments.urls")),
    path("api/labs/", include("labs.urls")),
    path("api/monitoring/", include("monitoring.urls")),
    path("api/notifications/", include("notifications.urls")),
]

# Django admin /django-admin/ da: /admin/... manzillari saytning (React) admin sahifalariga tegishli,
# aks holda sahifa yangilanganda nginx ularni Django'ga yuborib "Not Found" chiqarar edi.
# DJANGO_ADMIN_ENABLED=0 - umuman o'chiriladi (404): kundalik ish React admin panelida.
if settings.DJANGO_ADMIN_ENABLED:
    urlpatterns.insert(0, path("django-admin/", admin.site.urls))

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
