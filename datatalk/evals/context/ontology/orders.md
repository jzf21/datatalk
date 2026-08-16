# Orders and revenue

`sales.orders` is one purchase. `sales.order_items` is one product line on that
purchase. A line's money is always computed, never stored:

```
line_revenue = quantity * unit_price * (1 - discount)
```

`discount` is a fraction, so `0.10` is ten percent off. `unit_price` on the line
is the price charged at the time of sale and is what revenue must use —
`products.unit_price` is the current list price and is not authoritative for a
past order.

## Which orders count

`orders.status` is one of `delivered`, `shipped`, `pending`, `cancelled`.

**Revenue counts every status except `cancelled`.** A cancelled order was never
billed. `pending` and `shipped` orders are billed and count in full — excluding
them understates every period, because the most recent weeks are mostly in those
states.

Unqualified words map as follows, and these are the definitions to use when a
question does not say otherwise:

- **revenue** / **sales** / **GMV** — gross revenue: the sum of `line_revenue`
  over all non-cancelled orders. Refunds are *not* subtracted.
- **net revenue** — gross revenue minus `refunds.amount` for the same period,
  where a refund is attributed to the date it was issued (`refund_date`), not to
  the date of the order it refunds.
- **order count** — distinct `order_id`, again excluding `cancelled`.
- **average order value (AOV)** — gross revenue divided by order count over the
  same set of orders.

## Dating

An order is dated by `orders.order_date`. A refund is dated by
`refunds.refund_date`. These differ by days to weeks, so a period's refunds do
not correspond to that period's orders — never join them on the order's date.

## Refunds

`sales.refunds` holds at most one row per order and only for `delivered` orders.
`amount` is a partial or full refund of the order total; it is a positive number
and is subtracted where it applies.
