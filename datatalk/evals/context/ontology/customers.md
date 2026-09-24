# Customers

`sales.customers` is the only customer master. One row per customer, no history
table and no soft deletes — every row is a live customer.

- `segment` is one of `SMB`, `Mid-Market`, `Enterprise`. It is assigned at
  signup and never changes, so it is safe to group by directly.
- `country` is a two-letter code: `US`, `DE`, `GB`, `FR`, `JP`.
- `signup_date` is when the account was created. Every customer signed up during
  2024, so "customers who signed up before 2025" is all of them and is never a
  useful filter.

## Active vs. registered

A **customer** with no qualifier means a row in `customers` — registered, not
necessarily buying.

An **active customer** in a period is one with at least one non-cancelled order
whose `order_date` falls in that period. Count these from `orders`, not from
`customers`; the customer table has no activity flag and joining it in only
risks fanning out the count.

## Linking to web traffic

`events.page_views.customer_id` is the same identifier as
`customers.customer_id`, but it is `NULL` for anonymous traffic — roughly a
third of all page views. Any per-customer traffic metric has to decide what to
do with those rows, and the honest default is to exclude them and say so.

There is no join between the two warehouses. To relate a customer attribute to
web behaviour, aggregate in `events` first, then relate the small result to a
`sales` aggregate.
