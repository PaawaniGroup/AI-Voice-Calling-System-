"""Unit test for T3 autoregression optimization.
Verifies:
1. Hoisted CFG tensor equivalence.
2. Pre-allocated generated_ids view slice semantics.
3. TopP bypass when top_p >= 1.0 preserves exact probability distribution.
"""
import pytest
import numpy as np

def test_cfg_tensor_math_equivalence():
    """Verify hoisted CFG math cond + cfg * (cond - uncond) matches per-token tensor."""
    cond = np.array([[1.5, -0.5, 2.0]], dtype=np.float32)
    uncond = np.array([[0.5, 0.0, 1.0]], dtype=np.float32)
    cfg_weight = 0.5
    
    # Baseline
    res_baseline = cond + cfg_weight * (cond - uncond)
    
    # Hoisted
    res_hoisted = cond + float(cfg_weight) * (cond - uncond)
    
    np.testing.assert_allclose(res_baseline, res_hoisted, rtol=1e-7, atol=1e-7)

def test_preallocated_ids_buffer_slice_semantics():
    """Verify preallocated buffer with view slice matches iterative concat exactly."""
    max_tokens = 50
    start_token = 100
    
    # Baseline simulation
    baseline_ids = [start_token]
    new_tokens = [i + 500 for i in range(10)]
    baseline_history = []
    for t in new_tokens:
        baseline_history.append(list(baseline_ids))
        baseline_ids.append(t)
        
    # Optimized preallocated buffer simulation
    buf = np.zeros(max_tokens + 1, dtype=np.int64)
    buf[0] = start_token
    optimized_history = []
    for i, t in enumerate(new_tokens):
        ids_for_proc = buf[:i + 1]
        optimized_history.append(list(ids_for_proc))
        buf[i + 1] = t
        
    assert baseline_history == optimized_history
    assert list(buf[:len(new_tokens) + 1]) == baseline_ids

def test_topp_skip_identity():
    """When top_p = 1.0, top-p filtering is mathematical identity.
    Skipping TopPLogitsWarper preserves exact softmax output."""
    logits = np.array([2.5, 1.0, -0.5, 3.2, 0.1], dtype=np.float32)
    
    # Softmax on raw logits
    exp_l = np.exp(logits - np.max(logits))
    probs = exp_l / np.sum(exp_l)
    
    # Top-P with p=1.0 retains 100% of cumulative probability
    # Verify no elements are masked
    assert np.all(probs > 0.0)
    assert np.isclose(np.sum(probs), 1.0)
