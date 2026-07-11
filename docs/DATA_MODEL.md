# Data Model

## products

User-facing product records.

- `id`
- `display_name`
- `search_keyword`
- `memo`
- `enabled`
- `created_at`
- `updated_at`

## product_sources

Brand-specific product mapping.

- `id`
- `product_id`
- `brand`
- `external_product_code`
- `external_product_name`
- `barcode`
- `image_url`
- `price`
- `confidence`
- `confirmed_by_user`
- `enabled`
- `created_at`
- `updated_at`

## stores

Known convenience store locations.

- `id`
- `brand`
- `external_store_code`
- `name`
- `address`
- `latitude`
- `longitude`
- `phone`
- `enabled`
- `created_at`
- `updated_at`

## watch_rules

User-defined monitoring rules.

- `id`
- `product_id`
- `name`
- `mode`: `radius` or `stores`
- `center_latitude`
- `center_longitude`
- `radius_meters`
- `check_interval_seconds`
- `max_alert_count`
- `pause_after_alerts_seconds`
- `failure_retry_seconds`
- `enabled`
- `created_at`
- `updated_at`

## watch_rule_brands

Brands enabled for a watch rule.

- `id`
- `watch_rule_id`
- `brand`
- `enabled`

## watch_rule_stores

Explicit stores included in a watch rule.

- `id`
- `watch_rule_id`
- `store_id`

## watch_targets

Concrete monitoring targets generated from watch rules.

- `id`
- `watch_rule_id`
- `product_source_id`
- `store_id`
- `status`: `active`, `paused`, `failed`, `disabled`
- `last_stock_status`: `unknown`, `in_stock`, `out_of_stock`
- `last_stock_quantity`
- `alert_count`
- `paused_until`
- `last_checked_at`
- `last_alerted_at`
- `next_check_at`
- `next_retry_at`
- `failure_count`
- `last_error`
- `created_at`
- `updated_at`

## stock_snapshots

Inventory check history.

- `id`
- `watch_target_id`
- `status`: `in_stock`, `out_of_stock`, `unknown`, `failed`
- `quantity`
- `raw_payload`
- `error_message`
- `checked_at`

## notification_channels

Notification destinations.

- `id`
- `type`: `discord` or `telegram`
- `name`
- `config_json`
- `enabled`
- `created_at`
- `updated_at`

## alerts

Sent alert history.

- `id`
- `watch_target_id`
- `notification_channel_id`
- `title`
- `message`
- `status`: `sent`, `failed`
- `error_message`
- `sent_at`
