"""Latency Monitoring (section 18) — pipeline timestamps from exchange to detection."""

from dataclasses import dataclass


@dataclass(slots=True)
class LatencyMetric:
    exchange: str
    exchange_timestamp: float
    received_at: float
    processing_at: float
    detected_at: float

    @property
    def network_latency_ms(self) -> float:
        return (self.received_at - self.exchange_timestamp) * 1000

    @property
    def processing_latency_ms(self) -> float:
        return (self.processing_at - self.received_at) * 1000

    @property
    def detection_latency_ms(self) -> float:
        return (self.detected_at - self.processing_at) * 1000

    @property
    def total_latency_ms(self) -> float:
        return (self.detected_at - self.exchange_timestamp) * 1000
