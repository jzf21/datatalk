# Playbook: revenue questions

Any question of the form "how much did we make from X" resolves the same way.

1. Start from `sales.order_items`, join `sales.orders` on `order_id`.
2. Filter `orders.status <> 'cancelled'`.
3. Filter on `orders.order_date` for the period. Ranges are inclusive of both
   endpoints; a quarter means the calendar quarter.
4. Sum `quantity * unit_price * (1 - discount)`.
5. Round money to 2 decimal places in the final projection, never mid-query.

Add the grouping dimension by joining only what it needs:

| Grouped by         | Join                                                   |
|--------------------|--------------------------------------------------------|
| product category   | `sales.products` on `product_id`, group by `category`  |
| customer segment   | `sales.customers` on `orders.customer_id`              |
| country            | `sales.customers` — the *registered* country           |
| channel            | nothing; `orders.channel` is already there             |
| month              | `date_trunc('month', orders.order_date)`               |

Do not join `products` when the question does not need a product attribute:
`order_items` already carries the price actually charged.

## Common traps

- Joining `orders` to `order_items` and then also to `refunds` multiplies rows.
  Aggregate refunds separately and subtract the total.
- `COUNT(*)` after joining `order_items` counts lines, not orders. Order counts
  need `COUNT(DISTINCT orders.order_id)`.
- The current period is not complete. The data ends 2025-06-30; a monthly trend
  that includes a partial month should say so rather than reporting a decline.
