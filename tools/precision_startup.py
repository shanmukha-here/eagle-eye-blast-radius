"""Install trace hooks before spawned pytest workers unpickle their inputs."""
import os

if os.environ.get("PRECISION_TRACE"):
    import trace_precision

    trace_precision.start()
