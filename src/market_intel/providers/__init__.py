"""Provider registry (spec A8). Unregistered workflow entries resolve to
NO_DATA/미구현 in the engine."""
from __future__ import annotations

from .dart import DartProvider
from .ecos import EcosProvider
from .fred import FredProvider
from .pykrx_flows import PykrxProvider
from .sec_edgar import SecEdgarProvider
from .sec_edgar_13f import Sec13fProvider
from .yfinance_prices import YFinanceProvider

PROVIDERS: dict = {
    "yfinance": YFinanceProvider(),
    "pykrx": PykrxProvider(),
    "sec_edgar": SecEdgarProvider(),
    "sec_edgar_13f": Sec13fProvider(),
    "fred": FredProvider(),
    "ecos": EcosProvider(),
    "dart": DartProvider(),
}
