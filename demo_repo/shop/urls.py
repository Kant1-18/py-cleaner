from django.urls import include, path

urlpatterns = [
    path("catalog/", include("catalog.urls")),
]
