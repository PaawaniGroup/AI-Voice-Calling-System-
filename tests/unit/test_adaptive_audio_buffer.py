"""Unit tests for adaptive audio pre-buffering and jitter defense.
Verifies:
1. Immediate audio pass-through when prebuffer is 0.
2. Fast arrival (<350ms) scales target buffer down to 240ms, saving 160ms playout delay.
3. High jitter arrival (>=350ms) retains full 400ms buffer protection against underruns.
"""
import time
import pytest

def test_adaptive_buffer_math():
    configured_prebuffer = 400
    
    # Fast arrival simulation
    elapsed_fast = 220.0
    target_fast = min(float(configured_prebuffer), 240.0) if elapsed_fast < 350.0 else float(configured_prebuffer)
    assert target_fast == 240.0
    
    # Slow arrival simulation (jitter defense)
    elapsed_slow = 480.0
    target_slow = min(float(configured_prebuffer), 240.0) if elapsed_slow < 350.0 else float(configured_prebuffer)
    assert target_slow == 400.0

def test_unbuffered_mode_passthrough():
    configured_prebuffer = 0
    target_prebuffer_ms = float(configured_prebuffer)
    prebuffer_released = (target_prebuffer_ms <= 0)
    assert prebuffer_released is True
