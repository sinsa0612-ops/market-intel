"""SafeHttp — the only sanctioned path for direct API calls (spec A6).
yfinance/pykrx do their own internal HTTP and are exempt.

Guarantees: per-provider host whitelist, HTTPS-only, no auto-redirect
following (same-host redirect allowed once), token-bucket rate limiting,
and secret masking for anything that gets stored or logged — including the
ECOS gotcha where the key rides in the URL *path*, not just the query.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .errors import HostRejected

HOST_WHITELIST: dict[str, set[str]] = {
    "fred": {"api.stlouisfed.org"},
    "ecos": {"ecos.bok.or.kr"},
    "dart": {"opendart.fss.or.kr"},
    "sec_edgar": {"www.sec.gov", "data.sec.gov", "efts.sec.gov"},
    # spec B12 (ST1 addition) — calendar/event providers, same rule: HTTPS +
    # per-provider whitelist, no other host reachable through SafeHttp.
    "fred_calendar": {"api.stlouisfed.org"},
    "policy_calendar": {"www.federalreserve.gov", "www.bok.or.kr"},
    "sec_8k_events": {"data.sec.gov", "www.sec.gov"},
}

SECRET_PARAM_NAMES = {"api_key", "apikey", "key", "authkey", "servicekey", "crtfc_key"}

RATE_LIMITS = {"sec_edgar": 8.0}
DEFAULT_RATE = 2.0

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0


def safe_url(url: str, secrets: list[str] | None = None) -> str:
    """Mask query-param values/names and path segments that are (or look
    like) secrets. Used both for what we persist to the DB and what we log."""
    secrets = [s for s in (secrets or []) if s]
    parts = urlsplit(url)

    segments = parts.path.split("/")
    masked_segments = ["***" if seg and seg in secrets else seg for seg in segments]
    path = "/".join(masked_segments)

    pairs = parse_qsl(parts.query, keep_blank_values=True)
    masked_pairs = []
    for k, v in pairs:
        if k.lower() in SECRET_PARAM_NAMES or (v and v in secrets):
            masked_pairs.append((k, "***"))
        else:
            masked_pairs.append((k, v))
    # safe="*" keeps the mask readable as `api_key=***` instead of
    # percent-encoding it to `%2A%2A%2A`, which makes stored URLs and log lines
    # greppable during a secret audit.
    query = urlencode(masked_pairs, safe="*")

    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


class SecretRedactingFilter(logging.Filter):
    """Attached to the root logger: replaces every non-empty MI_* secret
    value found in a log message with '***' before it is emitted anywhere."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            msg = record.getMessage()
            for s in self._secrets:
                msg = msg.replace(s, "***")
            record.msg = msg
            record.args = ()
        return True


def configure_logging(settings, log_dir: str | None = None) -> logging.Logger:
    """Root logger with SecretRedactingFilter attached (spec A6): every
    non-empty MI_* secret value is replaced with '***' before a record is
    emitted, to var/logs/collect-YYYYMMDD.log and stderr alike."""
    root = logging.getLogger()
    log_dir = log_dir or settings.log_dir
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_file = Path(log_dir) / f"collect-{datetime.now(timezone.utc).strftime('%Y%m%d')}.log"

    redact = SecretRedactingFilter(settings.secret_values())
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redact)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redact)

    root.handlers = [file_handler, stream_handler]
    root.setLevel(logging.INFO)

    # Defense in depth: also scrub at the *source* logger (httpx logs full
    # request URLs, secrets included, at INFO) so redaction holds even if
    # some other handler gets attached to root later without the filter.
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(f, SecretRedactingFilter) for f in httpx_logger.filters):
        httpx_logger.addFilter(redact)

    return logging.getLogger("market_intel")


class SafeHttp:
    def __init__(
        self,
        provider: str,
        settings,
        transport: httpx.BaseTransport | None = None,
        rate: float | None = None,
    ):
        self.provider = provider
        self.allowed_hosts = HOST_WHITELIST.get(provider, set())
        self._secrets = settings.secret_values() if settings else []
        rate = rate if rate is not None else RATE_LIMITS.get(provider, DEFAULT_RATE)
        self._min_interval = (1.0 / rate) if rate else 0.0
        self._last_call = 0.0
        headers = {}
        if provider == "sec_edgar" and settings and settings.sec_user_agent:
            headers["User-Agent"] = settings.sec_user_agent
        self._client = httpx.Client(
            transport=transport,
            headers=headers,
            timeout=httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT),
            follow_redirects=False,
        )

    def safe_url(self, url: str) -> str:
        return safe_url(url, self._secrets)

    def _check_host(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise HostRejected(f"non-https scheme rejected: {self.safe_url(url)}")
        host = parts.netloc.split("@")[-1].split(":")[0]
        if self.allowed_hosts and host not in self.allowed_hosts:
            raise HostRejected(f"host not whitelisted for {self.provider}: {host}")

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def get(self, url: str, params: dict | None = None) -> httpx.Response:
        self._check_host(url)
        self._throttle()
        resp = self._client.get(url, params=params)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location", "")
            if location:
                req_host = urlsplit(url).netloc.split(":")[0]
                loc_host = urlsplit(location).netloc.split(":")[0] or req_host
                if loc_host != req_host:
                    raise HostRejected(f"cross-host redirect rejected: {loc_host}")
                self._check_host(location)
                self._throttle()
                resp = self._client.get(location)
        return resp

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SafeHttp":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
