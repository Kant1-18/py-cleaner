import os


def format_price(value):
    return f"{value:.2f} €"


def slugify_sku(sku):
    return sku.strip().upper().replace(" ", "-")
