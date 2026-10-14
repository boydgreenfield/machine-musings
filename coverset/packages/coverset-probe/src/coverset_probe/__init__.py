"""coverset-probe: which kinds of argument value production passes to your functions, as shape keys only.

The pytest-coverset shape tracer, adapted to run inside a long-lived service at a low sampling
rate. It exports, per window, k-anonymous counts of ``(function, parameter, shape key)``, of whole
call vectors and of per-request flows; never a value, a repr or a raw trace id. coverset imports the
export (``coverset flows import``) and lists what production passes in that no test ever did
(``coverset flows gaps``). Design: ``docs/production-flows.md``.

    import coverset_probe
    coverset_probe.start(modules=["myapp"], sample_rate=0.01, k_anonymity=5,
                         export=coverset_probe.JsonlExporter("/var/log/myapp/flows.jsonl"))
"""

from __future__ import annotations

from coverset_probe.context import trace
from coverset_probe.exporters import HttpExporter, JsonlExporter, OtelMetricsExporter
from coverset_probe.middleware import AsgiMiddleware, WsgiMiddleware
from coverset_probe.probe import Probe, current, start, stop

__all__ = [
    "AsgiMiddleware",
    "HttpExporter",
    "JsonlExporter",
    "OtelMetricsExporter",
    "Probe",
    "WsgiMiddleware",
    "current",
    "start",
    "stop",
    "trace",
]

__version__ = "0.1.0"
