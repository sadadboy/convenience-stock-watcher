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

Paused targets are resumed when:

- `status = paused`
- `paused_until <= now`

## Stock Found Flow

```text
check target
  -> stock exists
  -> create stock snapshot
  -> send alert
  -> increment alert_count
  -> if alert_count >= max_alert_count
       status = paused
       paused_until = now + pause_after_alerts_seconds
     else
       next_check_at = now + check_interval_seconds
```

## Stock Missing Flow

```text
check target
  -> stock missing
  -> create stock snapshot
  -> last_stock_status = out_of_stock
  -> alert_count = 0
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
```

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
