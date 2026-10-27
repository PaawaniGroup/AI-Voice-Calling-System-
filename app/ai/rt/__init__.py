"""Persistent real-time transport (Stage 2 of the real-time latency program; tenant config `rt_transport`, default: every service on the queue path).

protocol.py    wire format and the signed per-call token (standard library only; copied next to the worker servers in deploy/rt_workers/)
resilience.py  backoff, circuit breaker, readiness from completed requests
connection.py  one authenticated WebSocket per call and service: heartbeats, reconnect, bounded queues, cancellation that really stops the work
clients.py     RtModelClients: ModelClients whose voice and STT (and Brain URL) can use the persistent path, with the queue path as the fallback
"""
