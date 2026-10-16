import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "shop.settings")
os.environ["SHOP_DB"] = ":memory:"
django.setup()

import pytest  # noqa: E402
from django.apps import apps  # noqa: E402
from django.db import connection  # noqa: E402


@pytest.fixture(autouse=True)
def db_tables():
    """Create the tables in the in-memory SQLite database (the project has no migrations)."""
    models = list(apps.get_app_config("catalog").get_models())
    with connection.schema_editor() as editor:
        for model in models:
            editor.create_model(model)
    yield
    with connection.schema_editor() as editor:
        for model in reversed(models):
            editor.delete_model(model)
