"""What we record for each transfer, for the analysis part.

We only time the part where file bytes are moving (time.perf_counter) and
call on_transfer(record) after that, so something slow like a database insert
doesn't get counted.

units: sizes in bytes, duration_s in seconds, rate_mb_s in MB/s with
1 MB = 1,000,000 bytes
"""

import time
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class TransferRecord:
    operation: str  # "upload" or "download"
    name: str  # file name as requested by the client
    size_bytes: int  # declared size
    bytes_transferred: int  # actually moved (less than size if it failed)
    duration_s: float  # data-transfer time only
    outcome: str  # "ok" or "aborted"
    username: str = None
    client_addr: str = None
    concurrent_clients: int = None  # active sessions when the transfer began
    timestamp: float = field(default_factory=time.time)  # wall clock, for logs

    @property
    def rate_mb_s(self):
        """MB/s, 0.0 if the duration is zero"""
        if self.duration_s <= 0:
            return 0.0
        return self.bytes_transferred / self.duration_s / 1_000_000

    def to_dict(self):
        """flat dict with the rate included, for a db row or csv"""
        d = asdict(self)
        d["rate_mb_s"] = self.rate_mb_s
        return d
