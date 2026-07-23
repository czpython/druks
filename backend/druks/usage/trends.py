from datetime import timedelta

from druks.usage.schemas import UsageHistoryPoint

# Trend ranges for the percent-left sparklines. The 5h window gets one full
# window plus headroom so an exhaustion arc is visible end to end; weekly gets
# the whole week.
FIVE_HOUR_RANGE = timedelta(hours=6)
WEEK_RANGE = timedelta(days=7)


def downsample(points: list[UsageHistoryPoint], *, cap: int) -> list[UsageHistoryPoint]:
    # Thin a series to ≤ cap points, always keeping the newest sample (the
    # "now" anchor) — it replaces the last strided sample so the cap holds.
    if len(points) <= cap:
        return points
    stride = -(-len(points) // cap)  # ceil division
    thinned = points[::stride]
    thinned[-1] = points[-1]
    return thinned
