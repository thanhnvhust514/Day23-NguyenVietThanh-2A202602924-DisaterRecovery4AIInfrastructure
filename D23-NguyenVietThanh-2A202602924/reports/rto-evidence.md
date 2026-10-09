# RTO/RPO Evidence — Lab 23

Ngày chạy: 09/10/2026, Windows PowerShell, primary A, standby B, backend fs. Hai drill dùng manual_stop: ghi mốc rồi Ctrl+C service A. Đây là phương pháp đã chạy thực tế, khác netblock --mock trong hướng dẫn Linux. Giờ trình bày dùng UTC+07:00; trường iso trong raw logs dùng UTC.

## 1. Drill 1 — chưa có DR

| Chỉ số | Giá trị | Evidence |
|---|---|---|
| Mốc outage | 2026-10-09T11:41:52.850+07:00 | `chaos/chaos-events.jsonl:5` |
| Request lỗi đầu | +10.3s, HTTP 503, ConnectTimeout | `reports/drill-1-nodr.jsonl:59` |
| Request lỗi | 5/63 request toàn drill | `reports/drill-1-nodr.jsonl:59`, `reports/drill-1-nodr.jsonl:63` |
| Request cuối | +19.398s, vẫn lỗi | `reports/drill-1-nodr.jsonl:63` |
| RTO verdict | NO_RECOVERY trong cửa sổ quan sát | `reports/drill-1-nodr.jsonl:59`, `reports/drill-1-nodr.jsonl:63` |

Baseline valid:true. Hai warnings thiếu health detection và cutover phù hợp với drill chưa triển khai DR. Không suy ra downtime vô hạn từ cửa sổ ngắn này.

## 2. Drill 2 — có DR

Mốc 0 là timestamp 1791521845.8547676. Công cụ đo trả valid:true, warnings:[], recovered_by_region:b.

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage | 0s | action:kill, A; other_alive:true, forced_both:false | `chaos/chaos-events.jsonl:7` |
| User thấy lỗi | +7.1s | Request đầu ok:false sau outage | `reports/drill-2-withdr.jsonl:93` |
| Health check phát hiện | +24.2s | A UNHEALTHY, 3 lỗi liên tiếp | `reports/health-events.jsonl:5` |
| Runbook xác nhận outage | +66.398s | Bước 1, ba probe | `reports/runbook-run.jsonl:8` |
| Incident được ghi | +66.399s | Bước 2, có t_outage và auto:true | `reports/runbook-run.jsonl:9` |
| Verify target xong | +66.602s | 1_verify_target | `reports/failover-events.jsonl:23` |
| Snapshot restore xong | +66.617s | 2_restore_snapshot, kèm RPO | `reports/failover-events.jsonl:24` |
| Scale pool full | +66.618s | 3_scale_pool | `reports/failover-events.jsonl:25` |
| B ready | +73.230s | 4_wait_ready, waited_s:6.609 | `reports/failover-events.jsonl:26` |
| DNS cutover | +73.2s | 5_dns_cutover, active B | `reports/failover-events.jsonl:27` |
| Golden signals trực tiếp B | +73.456s | 10 request, p95 16ms, error rate 0 | `reports/runbook-run.jsonl:13` |
| **RTO qua Edge** | **+79.5s** | Request OK đầu sau lỗi, served_by:b | `reports/drill-2-withdr.jsonl:125` |
| Kết thúc quan sát | +200.601s | Request cuối vẫn OK từ B | `reports/drill-2-withdr.jsonl:367` |

| Chỉ số | Đo được | Mục tiêu | Verdict | Evidence |
|---|---|---|---|---|
| RTO — Inference API | **79.5s** | 300s | PASS, dư 220.5s | `chaos/chaos-events.jsonl:7`, `reports/drill-2-withdr.jsonl:125` |
| RPO — Vector DB | **26.01s / 13 documents** | 300s; chưa có ngưỡng documents riêng | Đạt theo giây, dư 273.99s | `reports/failover-events.jsonl:24` |
| Request lỗi sau outage | 32/367 request toàn drill | Ghi nhận ảnh hưởng | Seq 92–123 lỗi liên tiếp | `reports/drill-2-withdr.jsonl:93`, `reports/drill-2-withdr.jsonl:124` |
| State sau restore | 325 documents, weights có, pool full | Ready trước cutover | Đạt | `reports/failover-events.jsonl:26` |
| Model version | vi-e5-base@v3 | Restore weights/version cùng index | Version có trong log | `reports/failover-events.jsonl:24` |

RTO theo công cụ = 1791521925.3170211 − 1791521845.8547676 = 79.4622535s → **79.5s**. Timestamp loadgen là lúc bắt đầu request; response phục hồi mất thêm 345.6ms. Mốc outage ghi trước thao tác Ctrl+C, nên +7.1s gồm độ trễ thao tác tay, không phải riêng timeout.

RPO tại restore = 1791521910.6393976 − 1791521884.6308985 = 26.0084991s → **26.01s**. Hàm RPO đếm 13 documents trên A có ingested_at lớn hơn timestamp mới nhất của bản restore. Snapshot được chọn từ `reports/replication.jsonl:9`, chu kỳ every_s:30.0. Không dùng lag hiện tại để thay thế số tại restore.

## 3. RTO breakdown

Bốn thành phần yêu cầu có mặt, đồng thời ghi thêm thời gian điều phối để tổng khớp thực tế. Budget detection mà công cụ lab báo là 5 × 3 = 15s; detection đo được là 24.246366s.

| Thành phần | Giây | Công thức và Evidence | Cách giảm |
|---|---|---|---|
| Health-check detection budget | 15.000000 | interval × threshold; `reports/health-events.jsonl:5` | Giảm interval có kiểm soát, giữ xác nhận liên tiếp |
| Detection vượt budget | 9.246366 | t_detect − t_outage − 15; `chaos/chaos-events.jsonl:7`, `reports/health-events.jsonl:5` | Mốc outage chính xác hơn; tối ưu timeout/polling |
| Chờ điều phối, xác nhận lại và verify target | 42.355218 | t_verify − t_detect; `reports/health-events.jsonl:5`, `reports/failover-events.jsonl:23` | Alert kèm một lần confirm; thêm telemetry cho operator wait |
| Snapshot restore, đo RPO và scale file | 0.016175 | t_scale − t_verify; `reports/failover-events.jsonl:23`, `reports/failover-events.jsonl:25` | Chuẩn bị snapshot và đĩa nhanh |
| GPU pool warm-up và poll readiness | 6.612207 | t_ready − t_scale; `reports/failover-events.jsonl:25`, `reports/failover-events.jsonl:26` | Giữ standby full nếu chấp nhận chi phí; waited_s riêng 6.609s |
| Ghi pointer và log cutover | 0.002078 | t_cutover − t_ready; `reports/failover-events.jsonl:26`, `reports/failover-events.jsonl:27` | Giữ thao tác nguyên tử và nhỏ |
| DNS/LB TTL cache, request đang chờ và lấy mẫu | 6.230211 | t_recovered − t_cutover; `reports/failover-events.jsonl:27`, `reports/drill-2-withdr.jsonl:125` | Giảm TTL/timeout có kiểm soát |
| **Tổng** | **79.462255 ≈ 79.5s** | Tổng các phần làm tròn; lệch timestamp gốc dưới 0.000002s | Ưu tiên khoảng điều phối lớn nhất |

Log không tách được copy snapshot thuần khỏi phép đo RPO, hoặc TTL thuần khỏi khoảng chờ request. Interval × threshold là budget trong lab, không phải định luật thời điểm phát hiện sớm nhất: pha poll, timeout và thời điểm thao tác ảnh hưởng detection.

## 4. Kiểm tra lại và giữ evidence

```powershell
python tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl --target-rto 300
python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300
python -X utf8 -m pytest tests/ -v
```

Giữ nguyên raw logs để số dòng không đổi. Nộp năm log evidence theo ngoại lệ .gitignore. Log chaos và runbook giữ local; các dẫn chứng tới hai log này cần bản local để đối chiếu. Không chỉnh timestamp hoặc thêm request giả. PASS của công cụ chưa xác nhận người chấm chấp nhận phương pháp manual_stop thay cho netblock --mock.

Trên Windows, dùng -X utf8 vì tests đọc Markdown bằng encoding mặc định; tránh lỗi cp1252 khi đọc tiếng Việt.
