# Workspace overview

We sell consumer electronics and home-office equipment direct to consumers and
small businesses. The data lives in two warehouses and there is no join between
them — query each and relate the results.

| Source   | Holds                                                              |
|----------|--------------------------------------------------------------------|
| `sales`  | `customers`, `products`, `orders`, `order_items`, `refunds`         |
| `events` | `page_views` (web traffic), `marketing_spend` (daily ad spend)      |

The two sources share one join key by convention: `orders.channel` and
`marketing_spend.channel` use the same four values — `organic`, `paid_search`,
`email`, `social`. Web traffic uses a fifth, `direct`, in `page_views.referrer`,
which has no spend and never appears on an order.

All reporting is in a single currency; there is no FX conversion anywhere.

The warehouse holds the first half of 2025 (2025-01-01 to 2025-06-30) plus
customer signups going back to 2024. Anything asked about a period outside that
window will legitimately return nothing.
