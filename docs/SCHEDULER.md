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
  -> next_retry_at = now + check_interval * 2^(failure_count-1), capped at failure_retry_seconds
     (5 -> 10 -> 20 -> 40 -> 60 min with the defaults)
  -> on the 3rd failure in a row (~15 min): queue ⚠️ 조회 실패
```

Failure and recovery notices are held for one check interval (5 min) and sent
as one Discord message per brand, so an outage across 45 watches is one ⚠️ and
one ✅, not 90 messages. A blip that recovers before the 3rd failure sends
nothing; a watch that recovers while its ⚠️ is still held cancels it.
Watches still failing are grouped by brand + error in the daily report.

Errors are labelled by kind: 4xx = API changed or bad product code (needs a
fix), 5xx = brand server error, timeout, or connection failure.

`alerted_stores` is kept across failures; the first successful check after a
failure reports any changes, or sends ✅ 조회 복구 (only if ⚠️ was sent).

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
