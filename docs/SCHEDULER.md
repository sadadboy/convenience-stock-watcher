# Scheduler Design

## Main Loop

The scheduler periodically selects due watch targets.

A target is due when:

- `status = active`
- `next_check_at <= now`
- the related product, product source, store, watch rule, and brand mapping are enabled

Failed targets are retried when:

- `status = failed`
- `next_retry_at <= now`

Watches never pause on stock (the old "N alerts then pause" rule is gone;
`max_alert_count` / `pause_after_alerts_seconds` are no longer used).

## Successful Check Flow

Alerts fire on per-store changes only, so silence on Discord means "same as
the last message", quantities included.

```text
check target
  -> current = in-stock stores {store_code: {name, qty}}
  -> previous = alerted_stores
  -> stores in current, not previous  -> 🔔 입고 alert (restock event)
  -> stores in both, qty changed      -> 📉/📈 재고 변동 alert (quantity event)
  -> stores in previous, not current  -> ⚪ 품절 alert (sold_out event)
  -> nothing changed                  -> check event, no alert
  -> alerted_stores = current
  -> status = active
  -> next_check_at = now + check_interval_seconds
```

## Failure Flow

```text
check target
  -> adapter error or timeout
  -> create failed stock snapshot
  -> status = failed
  -> failure_count += 1
  -> last_error = error message
  -> next_retry_at = now + failure_retry_seconds
  -> on the first failure only: ⚠️ 조회 실패 alert
```

`alerted_stores` is kept across failures; the first successful check after a
failure reports any changes, or sends ✅ 조회 복구 with the current state.

## Manual Resume

Manual resume from dashboard:

```text
status = active
paused_until = null
next_retry_at = null
next_check_at = now
```

## Manual Disable

Manual disable from dashboard:

```text
status = disabled
```

Disabled targets are never selected by the scheduler.
