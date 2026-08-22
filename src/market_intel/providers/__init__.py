"""Provider registry (spec A8). Unregistered workflow entries resolve to
NO_DATA/미구현 in the engine."""
from __future__ import annotations

from .dart import DartProvider
from .earnings_calendar import EarningsCalendarProvider
from .ecos import EcosProvider
from .fred import FredProvider
from .fred_calendar import FredCalendarProvider
from .kis_flows import KisFlowsProvider
from .krx_breadth import KrxBreadthProvider
from .policy_calendar import PolicyCalendarProvider
from .sec_8k_events import Sec8kEventsProvider
from .sec_edgar import SecEdgarProvider
from .sec_edgar_13f import Sec13fProvider
from .yfinance_holdings import YFinanceHoldingsProvider
from .yfinance_prices import YFinanceProvider

PROVIDERS: dict = {
    "yfinance": YFinanceProvider(),
    "yfinance_holdings": YFinanceHoldingsProvider(),
    "sec_edgar": SecEdgarProvider(),
    "sec_edgar_13f": Sec13fProvider(),
    "fred": FredProvider(),
    "ecos": EcosProvider(),
    "dart": DartProvider(),
    "fred_calendar": FredCalendarProvider(),
    "earnings_calendar": EarningsCalendarProvider(),
    "policy_calendar": PolicyCalendarProvider(),
    "sec_8k_events": Sec8kEventsProvider(),
    "kis": KisFlowsProvider(),
    "krx": KrxBreadthProvider(),
}
