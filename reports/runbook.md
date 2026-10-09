# Runbook — Region A down, phục hồi sang B

**Phạm vi:** lab Windows PowerShell, A:8001, B:8002, Edge:8080, backend fs. Chạy trong thư mục repo, kích hoạt `.\venv\Scripts\Activate.ps1`. B/Edge còn sống, snapshot có sẵn. IC (incident commander) duyệt cutover/rollback; on-call thực hiện, state owner kiểm tra dữ liệu. Không chạy failover song song hoặc gọi lại sau cutover.

**Trước drill:** chạy ingest/replication trước, thấy REPLICATE đầu tiên; chạy loadgen 240 giây và health checker ở terminal riêng. Windows dùng ghi mốc rồi Ctrl+C A, không dùng netblock --mock. Sự kiện kill riêng không chứng minh A đã dừng. Runbook mặc định hỏi y/N; --auto chỉ dành drill/CI.

| # | Bước | Lệnh PowerShell copy-paste | Biết xong khi | Owner | Điều kiện dừng / rollback |
|---|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status`; `Get-Content reports/health-events.jsonl -Tail 5` | A UNHEALTHY sau 3 probe readiness lỗi; B alive:true, có thể chưa ready | On-call | A còn ready: không failover. B không alive: dừng, báo IC |
| 2 | Ghi incident, xác nhận và gọi orchestration một lần | `python dr/runbook.py --primary a --target b --backend fs` | Bước 1/2 vào log, operator nhập y; incident có ts và t_outage nếu chaos gần đây | On-call; IC duyệt | Từ chối: dừng. Không có snapshot: không cutover; state owner kiểm tra |
| 3 | Kiểm tra restore do runbook thực hiện | `Get-Content reports/failover-events.jsonl -Tail 5` | 2_restore_snapshot ok:true, có RPO/docs_lost/model version, theo sau verify | State owner | Restore lỗi, version sai hoặc RPO vượt 300 giây: báo IC, không lặp failover |
| 4 | Xác nhận pool và replica | `curl.exe --max-time 3 http://localhost:8002/readyz`; `curl.exe --max-time 3 http://localhost:8002/v1/state` | B ready:true, pool full, weights:true, count > 0; 4_wait_ready thành công | Serving on-call | Timeout phải dừng trước cutover. B lỗi sau cutover: xem rollback khi A đủ điều kiện |
| 5 | Xác nhận routing | `Start-Sleep -Seconds 6`; `curl.exe --max-time 3 http://localhost:8080/edge/state`; `curl.exe --max-time 3 http://localhost:8080/v1/infer` | 5_dns_cutover thành công, Edge active B, inference từ B | On-call | Còn A: kiểm tra TTL/log, không ghi pointer liên tục. B lỗi: báo IC |
| 6 | Verify golden signals | `Get-Content reports/runbook-run.jsonl -Tail 7` | Bước 6 có 10 request thật vào B, error_rate 0 và p95 có giá trị; bước 5 kiểm tra qua Edge | Serving on-call | Có request lỗi: chưa resolved. P95 tăng so baseline: điều tra; chưa có latency SLO phê duyệt |
| 7 | Đo RTO, lưu postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300`; `python -X utf8 -m pytest tests/ -v` | Traffic kết thúc; valid:true, warnings rỗng, recovered_by B, RTO ≤ 300 giây | On-call; IC review | Evidence thiếu: giữ log và drill lại; không sửa số. RTO vượt mục tiêu: mở action item |

Bước 2 tự thực hiện restore → scale → wait ready → cutover. Bước 3–5 chỉ kiểm tra, không chạy lại snapshot get hoặc failover.py. Kết quả thực tế: RTO 79.5s, RPO 26.01s/13 documents; p95 trực tiếp B 16ms, error rate 0 (`reports/runbook-run.jsonl:13`). P95 là quan sát, không phải ngưỡng SLA.

## Rollback có kiểm soát

**Điều kiện:** B lỗi liên tiếp sau cutover hoặc replica không đáp ứng yêu cầu; A đã ready 3 lần liên tiếp cách nhau 5 giây. State owner xác nhận dữ liệu/weights/version của A phù hợp, đối soát mọi ghi mới trên B nếu có. Nếu A chưa ready, giữ B và xử lý incident, không tự động chuyển ngược.

**Thẩm quyền:** IC phê duyệt rõ việc trả traffic; on-call thao tác, ghi người duyệt vào incident record. Nếu A đã dừng, bật lại ở terminal service A:

```powershell
$env:REGION = 'a'
$env:STATE_DIR = 'state/region-a'
$env:WARMUP_SECONDS = '6'
python -m uvicorn serving.app:app --host 127.0.0.1 --port 8001 --log-level warning
```

Tại terminal điều khiển, kiểm tra sau khi state owner đã xác nhận:

```powershell
python -c "import httpx,time; c=httpx.Client(timeout=2); checks=[(c.get('http://127.0.0.1:8001/readyz').status_code==200,time.sleep(5))[0] for _ in range(3)]; c.close(); assert all(checks), 'A is not ready'; print('A ready: 3/3')"
```

Chỉ khi lệnh kiểm tra thành công và IC duyệt mới chạy:

```powershell
python -c "from pathlib import Path; p=Path('edge/active_region'); q=p.with_name('active_region.rollback.tmp'); q.write_text('a', encoding='utf-8'); q.replace(p)"
python -c "from dr.runbook import step; step('rollback','ic_approved_return_to_a',active_region='a')"
Start-Sleep -Seconds 6
curl.exe --max-time 3 http://localhost:8080/edge/state
curl.exe --max-time 3 http://localhost:8080/v1/infer
```

Xác nhận Edge active A và inference từ A. Không gọi failover ngược để restore snapshot A cũ lên A vì có thể ghi đè dữ liệu mới. Không make clean hoặc xóa evidence trong incident.
