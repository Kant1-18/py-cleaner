from django.urls import path

from . import views

urlpatterns = [
    path("orders/<int:order_id>/summary/", views.order_summary, name="order-summary"),
    path("sync/", views.sync_endpoint, name="sync"),
]
