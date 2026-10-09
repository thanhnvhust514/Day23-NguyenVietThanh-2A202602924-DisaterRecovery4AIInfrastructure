"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi timeline riêng cho runbook."""
    ts = time.time()
    event = {"ts": ts, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)),
             "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(event) + "\n")
    print(json.dumps(event), flush=True)
    return event


def confirm(auto: bool, msg: str) -> bool:
    """Chỉ tiếp tục khi có xác nhận, hoặc khi chạy drill với --auto."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N]: ").strip().lower() == "y"
    except EOFError:
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Xác nhận outage, failover một lần, rồi kiểm tra phục hồi."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary/target must be different regions a/b")
    if backend not in ("fs", "minio"):
        raise ValueError("backend must be fs/minio")
    started = time.monotonic()
    invoked_at = time.time()

    def finish(ok, reason, **details):
        result = {"ok": ok, "primary": primary, "target": target, "reason": reason,
                  "elapsed_s": round(time.monotonic() - started, 3), **details}
        step(7, "post_incident", **result,
             measure_command="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
        return result

    probes = []
    for attempt in range(3):
        if attempt:
            time.sleep(5.0)
        ready, reason = hc.probe(primary, 2.0)
        target_ready, target_reason = hc.probe(target, 2.0)
        # Standby may be unready before restore, but its process must respond.
        try:
            response = httpx.get(f"{URL[target]}/healthz", timeout=2.0)
            target_alive = response.status_code == 200 and response.json().get("region") == target
        except (httpx.RequestError, ValueError, AttributeError):
            target_alive = False
        probes.append({"primary_ready": ready, "primary_reason": reason,
                       "target_ready": target_ready, "target_reason": target_reason,
                       "target_alive": target_alive})
        if ready or not target_alive:
            step(1, "xac_nhan_outage", ok=False, probes=probes)
            return finish(False, "Primary is ready or target process is unavailable", cutover=False)
    step(1, "xac_nhan_outage", ok=True, consecutive_fails=3,
         interval_s=5.0, probes=probes)

    # Chaos evidence is optional for operational use; never invent its timestamp.
    t_outage = None
    chaos = pathlib.Path("chaos/chaos-events.jsonl")
    if chaos.exists():
        events = [json.loads(line) for line in chaos.read_text(encoding="utf-8").splitlines()
                  if line.strip()]
        kills = [event for event in events if event.get("action") == "kill"
                 and event.get("region") == primary
                 and invoked_at - 300 <= event["ts"] <= invoked_at]
        if kills:
            t_outage = kills[-1]["ts"]
    incident = step(2, "thong_bao_incident", primary=primary, target=target,
                    t_outage=t_outage, auto=auto)
    if not confirm(auto, f"Confirm failover {primary} -> {target}?"):
        return finish(False, "Operator declined failover", cutover=False)

    # Exactly one call: this owns all five failover substeps.
    result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", ok=result.get("ok", False), result=result,
         operator_confirmed=True, operator_confirmed_at_before_failover=True)
    if not result.get("ok"):
        return finish(False, "Failover aborted", failover=result,
                      cutover=result.get("cutover", False))

    state = result.get("state", {})
    replica_ok = state.get("weights") is True and state.get("count", 0) > 0
    step(4, "verify_state_replica", ok=replica_ok, state=state,
         rpo_seconds=result.get("rpo_seconds"), docs_lost=result.get("docs_lost"),
         embed_model_version=result.get("snapshot", {}).get("embed_model_version"))
    cutover_ok = result.get("cutover") is True
    step(5, "dns_cutover", ok=cutover_ok, target=target)
    if not replica_ok or not cutover_ok:
        return finish(False, "Replica or cutover verification failed", cutover=cutover_ok)

    samples = []
    with httpx.Client(timeout=3.0) as client:
        for index in range(10):
            t0 = time.monotonic()
            try:
                response = client.get(f"{URL[target]}/v1/infer", params={"q": f"hoa don {index}"})
                body = response.json()
                ok = (response.status_code == 200 and body.get("region") == target
                      and bool(body.get("answer")))
                sample = {"ok": ok, "status": response.status_code,
                          "served_by": body.get("region"), "error": body.get("error")}
            except (httpx.RequestError, ValueError, AttributeError) as exc:
                sample = {"ok": False, "status": None, "error": type(exc).__name__}
            sample.update(seq=index, ts=time.time(),
                          latency_ms=round((time.monotonic() - t0) * 1000, 3))
            samples.append(sample)
    latencies = sorted(sample["latency_ms"] for sample in samples)
    p95 = latencies[math.ceil(0.95 * len(latencies)) - 1]
    error_rate = sum(not sample["ok"] for sample in samples) / len(samples)
    step(6, "verify_golden_signals", ok=error_rate == 0, requests=10,
         p95_latency_ms=p95, error_rate=error_rate, samples=samples)
    return finish(error_rate == 0, "Verified target inference" if error_rate == 0
                  else "Target inference errors; operator review required",
                  cutover=True, state=state, p95_latency_ms=p95, error_rate=error_rate,
                  t_outage=t_outage, operator_notified_at=incident["ts"],
                  rpo_seconds=result.get("rpo_seconds"), docs_lost=result.get("docs_lost"))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a", choices=["a", "b"])
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
