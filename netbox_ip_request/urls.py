from django.urls import include, path

from utilities.urls import get_model_urls

from . import views

urlpatterns = [
    path('requests/', include(get_model_urls('netbox_ip_request', 'iprequest', detail=False))),
    path('requests/<int:pk>/', include(get_model_urls('netbox_ip_request', 'iprequest'))),
    path('requests/<int:pk>/<str:decision>/', views.IPRequestDecisionView.as_view(), name='iprequest_decide'),
    path('discrepancies/', include(get_model_urls('netbox_ip_request', 'discrepancy', detail=False))),
    path('discrepancies/<int:pk>/', include(get_model_urls('netbox_ip_request', 'discrepancy'))),
]
