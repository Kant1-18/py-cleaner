import json
from decimal import Decimal

from django.test import Client

from catalog.models import Order, OrderLine, Product, Supplier


def test_order_summary_total():
    supplier = Supplier.objects.create(name="Café Léon")
    product = Product.objects.create(sku="A1", name="Espresso", price=Decimal("30.00"), supplier=supplier)
    order = Order.objects.create(customer_email="a@b.fr", country="FR")
    OrderLine.objects.create(order=order, product=product, quantity=4)

    response = Client().get(f"/catalog/orders/{order.pk}/summary/")

    data = json.loads(response.content)
    assert response.status_code == 200
    assert data["subtotal"] == "120.00"
    assert data["discount"] == "6.00"
    assert data["total"] == "136.80"
