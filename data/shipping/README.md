# Internal shipping report

Drop the internal shipping report here as one or more `.csv` files. The daily sync reads every CSV in this folder and matches each row to an FBM order on the **Amazon order number**. You can add a new file each month or overwrite one file.

## Columns

| Needed | What | Accepted header names (case, spaces, `_` and `-` ignored) |
|---|---|---|
| Yes | Amazon order number, e.g. `112-1234567-1234567` | `amazon_order_id`, `order_id`, `order_number`, `order #`, `amazon order number` |
| Yes | Shipping cost for that row, in $ (`$12.40` or `12.40`) | `shipping_cost`, `shipping_fee`, `shipping`, `postage`, `label_cost`, `freight`, `cost` |
| No | Internal shipment number | `shipment_number`, `shipment_id`, `shipment`, `internal_shipment` |

Other columns are ignored. If an order has several rows (split shipments), the costs are added together and all shipment numbers are listed.

Example:

```csv
amazon_order_id,shipment_number,shipping_cost
112-1234567-1234567,SHP-10482,18.45
113-7654321-7654321,SHP-10483,22.10
```

Orders with no matching row show "Not in report", and their profit excludes shipping until a matching row is added.
