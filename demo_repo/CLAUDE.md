# Notes for agents

- Order totals are computed by `compute_order_total()` in `catalog/pricing.py`.
- The supplier feed synchronisation goes through `sync_products()` in `catalog/sync.py`.
- Supplier resolution is centralised in `catalog.matching.resolve`.
- To format a price, use `format_price()` from `catalog/utils.py`.
- Run the tests with `pytest`.
