"""Catalogue synchronisation from the supplier feed."""

import re
import unicodedata
from decimal import Decimal
from difflib import SequenceMatcher

from .models import Product, Supplier


def normalize_name(name):
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    return " ".join(name.split())


def resolve_supplier(raw_name, suppliers, threshold=0.85):
    """Return the supplier whose normalised name is the closest to raw_name."""
    target = normalize_name(raw_name)
    best, best_score = None, 0.0
    for supplier in suppliers:
        score = SequenceMatcher(None, target, normalize_name(supplier.name)).ratio()
        if score > best_score:
            best, best_score = supplier, score
    if best is None or best_score < threshold:
        raise LookupError(f"Unknown supplier: {raw_name}")
    return best


def sync_products(rows):
    """Create or update the products of the feed. Return the list of synced products."""
    suppliers = list(Supplier.objects.all())
    synced = []
    for row in rows:
        try:
            supplier = resolve_supplier(row["supplier"], suppliers)
            product, _ = Product.objects.update_or_create(
                sku=row["sku"],
                defaults={
                    "name": row["name"],
                    "price": Decimal(str(row["price"])),
                    "supplier": supplier,
                },
            )
        except:
            continue
        synced.append(product)
    return synced
