# Convenience Stock Watcher PRD

## Goal

Monitor convenience store inventory for user-selected products and locations, then send alerts when stock is found.

## Target Stores

- GS25
- CU
- 7-Eleven
- Emart24

Each store brand must be implemented as a separate adapter because product search, product codes, store search, and inventory APIs differ by brand.

## MVP Scope

- Register products by search keyword.
- Map products to brand-specific product candidates.
- Register watch rules by location radius or selected stores.
- Run scheduled inventory checks.
- Send Discord or Telegram alerts when stock exists.
- Limit repeated alerts for the same product, brand, and store.
- Pause checks after repeated alerts.
- Retry failed checks after a configurable delay.
- Provide a small dashboard for management.

## Alert Policy

Inventory is checked by watch target.

A watch target is the combination of:

- Product
- Store brand
- Store

Default behavior:

- If stock exists, send an alert.
- Send up to 3 alerts for the same watch target.
- After 3 alerts, pause that target for 5 hours.
- User can change the max alert count and pause duration.
- User can manually resume a paused target from the dashboard.
- If stock becomes unavailable, reset the alert count.

## Failure Policy

When an inventory check fails:

- Mark the target as failed.
- Set `next_retry_at` to 1 hour later by default.
- Show the failure on the dashboard.
- Let the user retry immediately.
- Keep failure count and last error message for debugging.

## Product Code Policy

Users search by product name, but scheduled jobs should use brand-specific product codes.

Flow:

1. User enters a product keyword.
2. App searches product candidates per store brand.
3. User confirms the correct candidate.
4. App stores the brand product code in `product_sources`.
5. Scheduled checks use stored product codes instead of searching by keyword each time.

This avoids repeated product search, reduces request volume, and prevents wrong matches.

## Dashboard Screens

- Overview
- Products
- Product candidate mapping
- Watch rules
- Stores
- Alert history
- Check history
- Notification settings
- System health

## Important Risks

- Store APIs can change without notice.
- Unofficial endpoints can be blocked or rate-limited.
- Product names are ambiguous.
- Stock data may be delayed or inaccurate.
- Excessive polling may cause failures or service restrictions.

## Recommended First Milestone

1. Build the dashboard and database schema.
2. Add mock inventory adapter.
3. Implement scheduler and alert policy.
4. Add Discord notification.
5. Connect one real store adapter.
6. Add remaining store adapters one by one.
