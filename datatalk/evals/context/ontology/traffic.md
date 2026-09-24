# Web traffic and marketing spend

Both tables live in the `events` source.

## `page_views`

One row per page view. There is no session table — a **session** is a distinct
`session_id`, and a **visitor** is best approximated by `customer_id` where it
is set.

- `path` is the URL path. Product pages are `/product/<product_id>`; the id
  after the last slash is `products.product_id` in the `sales` source.
- `referrer` is the acquisition channel of the *visit*: `organic`,
  `paid_search`, `email`, `social`, `direct`.
- `device` is `desktop`, `mobile` or `tablet`.
- `country` here is the country of the visit and is independent of the
  customer's registered country in `sales.customers`. They disagree often; do
  not treat one as a correction of the other.

## `marketing_spend`

One row per channel per day, `amount` being that day's spend. There is no row
for `direct`, because direct traffic has no spend — a channel-level join with
`orders` or `page_views` therefore has to tolerate a missing spend row rather
than dropping the channel.

Spend is dated by `spend_date`. To compare spend against orders, aggregate spend
by channel in `events`, aggregate orders by channel in `sales`, and relate the
two results — the warehouses cannot be joined.

## Cost per order

**Cost per order** for a channel over a period is that channel's total spend
divided by its count of non-cancelled orders in the same period. `organic` and
`direct` have spend of zero and near-zero respectively, so a cost-per-order
table that includes them is dominated by them; say which channels are included.
