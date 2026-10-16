import json
from decimal import Decimal

from django.http import Http404, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from .models import Order, Product
from .sync import sync_products


@require_GET
def order_summary(request, order_id):
    try:
        order = Order.objects.get(pk=order_id)
    except Order.DoesNotExist:
        raise Http404("Unknown order")

    lines = []
    subtotal = Decimal("0")
    items = 0
    for line in order.lines.select_related("product"):
        amount = line.product.price * line.quantity
        if line.quantity >= 10:
            amount = amount * Decimal("0.95")
        subtotal += amount
        items += line.quantity
        lines.append({"sku": line.product.sku, "quantity": line.quantity, "amount": str(amount)})

    discount = Decimal("0")
    if subtotal > 200:
        discount = subtotal * Decimal("0.10")
    elif subtotal > 100:
        discount = subtotal * Decimal("0.05")

    if order.country == "FR":
        vat_rate = Decimal("0.20")
    elif order.country in ("BE", "ES"):
        vat_rate = Decimal("0.21")
    else:
        vat_rate = Decimal("0")

    shipping = Decimal("0") if subtotal - discount > 50 else Decimal("4.90")
    vat = (subtotal - discount) * vat_rate
    total = (subtotal - discount + vat + shipping).quantize(Decimal("0.01"))

    return JsonResponse({
        "order": order.pk,
        "items": items,
        "lines": lines,
        "subtotal": str(subtotal.quantize(Decimal("0.01"))),
        "discount": str(discount.quantize(Decimal("0.01"))),
        "vat": str(vat.quantize(Decimal("0.01"))),
        "shipping": str(shipping),
        "total": str(total),
    })


@require_POST
def sync_endpoint(request):
    rows = json.loads(request.body or "[]")
    synced = sync_products(rows)
    return JsonResponse({"synced": len(synced), "skus": [p.sku for p in synced]})


def product_count(request):
    return JsonResponse({"count": Product.objects.count()})
