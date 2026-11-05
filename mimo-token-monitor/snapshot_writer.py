"""
Snapshot writer for claude-hud integration.

Writes MIMO usage data to a JSON file that claude-hud can read.
"""
import json
import os
import tempfile
from datetime import datetime, timezone


def _format_tokens(n) -> str | None:
    """Format token count to human readable string."""
    if n is None:
        return None
    n = int(n)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _get_plan_tier_name(total: int) -> str | None:
    """Get plan tier name based on total tokens."""
    tiers = [
        (82_000_000_000, "Max"),
        (38_000_000_000, "Pro"),
        (11_000_000_000, "Standard"),
        (4_100_000_000, "Lite"),
    ]
    for credits, name in sorted(tiers, reverse=True):
        if total >= credits * 0.95:
            return name
    return None


def write_snapshot(
    snapshot_path: str,
    balance: float | None,
    plan_used: int,
    plan_total: int,
    month_used: int,
    month_limit: int,
    daily_used: int = 0,
    error: str | None = None,
    expiry_date: str = "",
) -> bool:
    """
    Write MIMO usage snapshot for claude-hud to read.

    Args:
        snapshot_path: Path to write the snapshot JSON file
        balance: Account balance in yuan
        plan_used: Total plan tokens used
        plan_total: Total plan tokens limit
        month_used: This month tokens used
        month_limit: This month tokens limit
        daily_used: Today's tokens used
        expiry_date: User-entered plan expiry date
        error: Error message if any

    Returns:
        True if successful, False otherwise
    """
    if not snapshot_path:
        return False

    try:
        # Calculate percentage
        used_percentage = None
        if plan_total > 0:
            used_percentage = round((plan_used / plan_total) * 100)

        # Determine plan name
        plan_name = _get_plan_tier_name(plan_total) if plan_total > 0 else None

        # Build snapshot
        snapshot = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "plan_name": plan_name,
            "used_percentage": used_percentage,
            "used_amount": _format_tokens(plan_used) if plan_used > 0 else None,
            "total_amount": _format_tokens(plan_total) if plan_total > 0 else None,
            "daily_used": _format_tokens(daily_used) if daily_used > 0 else None,
            "balance": round(float(balance), 2) if balance is not None else None,
            "balance_currency": "CNY",
            "expires_at": str(expiry_date or "").strip() or None,
            "error": error,
        }

        # Ensure directory exists
        snapshot_path = os.path.expanduser(snapshot_path)
        os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)

        # Atomic write (write to temp file, then rename)
        dir_name = os.path.dirname(snapshot_path)
        base_name = os.path.basename(snapshot_path)

        fd, tmp_path = tempfile.mkstemp(
            dir=dir_name,
            prefix=f".{base_name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, snapshot_path)
            os.chmod(snapshot_path, 0o600)
            return True
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    except Exception as e:
        print(f"Failed to write snapshot: {e}")
        return False
