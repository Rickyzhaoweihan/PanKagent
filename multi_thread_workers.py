import os
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

# Bounded cap for leaf-level parallelism (per call). Prevents the per-task
# thread explosion that raw start_new_thread caused under high concurrency.
# Env-tunable so fan-out can be traded for thread count without a code change.
LEAF_THREADS = max(1, int(os.environ.get("LEAF_THREADS", "8")))


def _resolve_workers(n_inputs: int, max_workers: int) -> int:
    """Pick a bounded worker count. max_workers==0 -> min(n_inputs, LEAF_THREADS)."""
    if max_workers <= 0:
        return max(1, min(n_inputs, LEAF_THREADS))
    return max(1, min(n_inputs, max_workers))


def map_once(func, inputs: list, max_workers: int = 0) -> list:
    '''
    For each item in the input list, apply the function to it and put the result
    in the corresponding index of the output list.

    Don't consider exceptions, no retry (failed items stay None). Uses a bounded
    ThreadPoolExecutor instead of one raw thread per item — no busy-wait polling.
    '''
    assert (max_workers >= 0)
    if not inputs:
        return []
    results = [None] * len(inputs)
    workers = _resolve_workers(len(inputs), max_workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_idx = {pool.submit(func, inputs[i]): i for i in range(len(inputs))}
        for fut in as_completed(future_to_idx):
            idx = future_to_idx[fut]
            try:
                results[idx] = fut.result()
            except Exception:
                traceback.print_exc()
    return results


def map_infinite_retry(func, inputs: list, max_workers: int = 0, print_progress: bool = False) -> list:
    '''
    For each item in the input list, apply the function to it and put the result
    in the corresponding index of the output list.

    Will retry a failed item until it succeeds. Bounded ThreadPoolExecutor; each
    worker re-submits its own item on failure so the pool size stays constant.
    '''
    assert (max_workers >= 0)
    if not inputs:
        return []
    results = [None] * len(inputs)
    workers = _resolve_workers(len(inputs), max_workers)

    def run_until_success(index: int) -> int:
        while True:
            try:
                results[index] = func(inputs[index])
                return index
            except Exception:
                # retry the same item
                continue

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_until_success, i) for i in range(len(inputs))]
        done = 0
        for _ in as_completed(futures):
            done += 1
            if print_progress:
                print(f"map_infinite_retry: {done}/{len(inputs)} done")
    return results
