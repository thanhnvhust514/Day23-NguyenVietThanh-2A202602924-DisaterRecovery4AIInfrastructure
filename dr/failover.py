"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append một sự kiện có timestamp và in ra stdout."""
    ts = time.time()
    event = {"ts": ts, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(event) + "\n")
    print(json.dumps(event), flush=True)
    return event


def state_of(region: str) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    response.raise_for_status()
    state = response.json()
    if state.get("region") != region:
        raise ValueError(f"Expected region {region}, got {state.get('region')}")
    return state


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore, scale, xác nhận readiness rồi mới chuyển routing."""
    if target not in URL or backend not in ("fs", "minio") or wait <= 0:
        raise ValueError("target must be a/b, backend fs/minio, wait > 0")
    primary = "b" if target == "a" else "a"
    current_step = "1_verify_target"
    cutover = False
    try:
        initial = state_of(target)
        emit(step=current_step, target=target, ok=True, state=initial)

        current_step = "2_restore_snapshot"
        restored = snapshot.get(target, backend)
        source = restored.get("source_region", primary)
        if source != primary:
            raise ValueError(f"Snapshot source {source} is not primary {primary}")
        recovery = snapshot.rpo(
            pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )
        emit(step=current_step, target=target, ok=True, **restored, **recovery)

        current_step = "3_scale_pool"
        pool = pathlib.Path(f"state/region-{target}/pool_state")
        pool.parent.mkdir(parents=True, exist_ok=True)
        pool.write_text("full", encoding="utf-8")
        emit(step=current_step, target=target, ok=True, pool_state="full")

        current_step = "4_wait_ready"
        started = time.monotonic()
        deadline = started + wait
        reason = "readiness timeout"
        while time.monotonic() < deadline:
            try:
                response = httpx.get(
                    f"{URL[target]}/readyz",
                    timeout=min(2.0, max(0.001, deadline - time.monotonic())),
                )
                body = response.json()
                if (response.status_code == 200 and body.get("ready") is True
                        and body.get("region") == target):
                    break
                reason = f"HTTP {response.status_code}: {body}"
            except (httpx.RequestError, ValueError, AttributeError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.5, remaining))
        else:
            emit(step=current_step, target=target, ok=False,
                 waited_s=round(time.monotonic() - started, 3), reason=reason)
            return {"ok": False, "target": target, "cutover": False,
                    "failed_step": current_step, "reason": reason}
        # Read the restored state before writing the routing pointer.
        final_state = state_of(target)
        emit(step=current_step, target=target, ok=True,
             waited_s=round(time.monotonic() - started, 3), state=final_state)

        current_step = "5_dns_cutover"
        active = pathlib.Path("edge/active_region")
        active.parent.mkdir(parents=True, exist_ok=True)
        temporary = active.with_name("active_region.tmp")
        temporary.write_text(target, encoding="utf-8")
        temporary.replace(active)
        cutover = True
        emit(step=current_step, target=target, ok=True, active_region=target)
        return {"ok": True, "target": target, "cutover": True,
                "state": final_state, "snapshot": restored, **recovery}
    except (Exception, SystemExit) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        emit(step=current_step, target=target, ok=False, reason=reason)
        return {"ok": False, "target": target, "cutover": cutover,
                "failed_step": current_step, "reason": reason}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
