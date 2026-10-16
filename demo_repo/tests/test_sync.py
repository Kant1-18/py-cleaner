from catalog.models import Product, Supplier
from catalog.sync import sync_products


def test_sync_creates_products_for_known_suppliers():
    Supplier.objects.create(name="Café Léon")
    synced = sync_products([{"sku": "A1", "name": "Espresso", "price": "2.50", "supplier": "cafe leon"}])
    assert [p.sku for p in synced] == ["A1"]
    assert Product.objects.get(sku="A1").supplier.name == "Café Léon"
