def get_global_trades(cutoff_ts):
    """Feed global de trades recientes de todo Polymarket."""
    all_trades, offset = [], 0
    while offset <= 2000:
        batch = safe_get(f"{DATA_API}/trades", params={"limit": 500, "offset": offset, "takerOnly": "true"})
        if not isinstance(batch, list) or not batch:
            break
        all_trades.extend(batch)
        oldest = min(normalize_ts(t.get("timestamp")) for t in batch)
        if oldest < cutoff_ts:
            break
        offset += 500
    if all_trades:
        newest = max(normalize_ts(t.get("timestamp")) for t in all_trades)
        now = int(time.time())
        log(f"Feed crudo: {len(all_trades)} trades, más reciente hace {now - newest}s")
        log(f"DEBUG now={now} cutoff={cutoff_ts} newest={newest} raw_ts={all_trades[0].get('timestamp')!r}")
    else:
        log("Feed crudo: vacío — el endpoint no devolvió datos")
    recent = [t for t in all_trades if normalize_ts(t.get("timestamp")) >= cutoff_ts]
    log(f"Feed global: {len(recent)} trades en la ventana")
    return recent
