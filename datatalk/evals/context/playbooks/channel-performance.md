# Playbook: channel and acquisition questions

Channel questions almost always span both warehouses: orders live in `sales`,
spend lives in `events`. There is no cross-source SQL, so the shape is always
two queries and a comparison.

1. In `sales`: orders and revenue by `orders.channel` for the period, excluding
   `cancelled`.
2. In `events`: `SUM(amount)` from `marketing_spend` grouped by `channel` for
   the same `spend_date` range.
3. Relate the two by channel name. The four spend channels are `organic`,
   `paid_search`, `email`, `social`.

Derived metrics, once both halves are in hand:

- **cost per order** = spend / non-cancelled order count
- **ROAS** = revenue / spend
- **cost per acquisition** is *not* defined here; we do not track first-order
  attribution, so use cost per order and say which it is.

`page_views.referrer` uses the same channel names plus `direct`. Traffic and
orders are attributed independently — a visit's referrer is not the channel of
any order that visitor later places, so never treat page views as a funnel stage
above orders.
