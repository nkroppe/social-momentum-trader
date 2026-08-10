"""Operations: alerts, kill switch, soak tracking, reports, and preflight checks."""

from .alerts import Alerter
from .killswitch import KillSwitch
from .performance import EquityPoint, PerformanceTrade, calculate_performance
from .preflight import CheckResult, all_passed, run_preflight
from .reports import (
    build_weekly_report,
    trade_closed_alert,
    trade_opened_alert,
    trade_partial_alert,
)
from .schedule import WeeklyScheduler
from .soak import SoakTracker

__all__ = [
    "Alerter",
    "KillSwitch",
    "EquityPoint",
    "PerformanceTrade",
    "SoakTracker",
    "WeeklyScheduler",
    "CheckResult",
    "all_passed",
    "run_preflight",
    "calculate_performance",
    "build_weekly_report",
    "trade_closed_alert",
    "trade_opened_alert",
    "trade_partial_alert",
]
