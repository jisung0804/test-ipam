from django.urls import path

from netbox.api.routers import NetBoxRouter

from . import views

app_name = 'netbox_ip_request-api'
router = NetBoxRouter()
router.register('requests', views.IPRequestViewSet)
router.register('discrepancies', views.DiscrepancyViewSet)
urlpatterns = router.urls + [
    path('arp-ingest/', views.ArpIngestView.as_view(), name='arp-ingest'),
    path('mac-ingest/', views.MacIngestView.as_view(), name='mac-ingest'),
    path('locate/<str:ip>/', views.LocateView.as_view(), name='locate'),
]
