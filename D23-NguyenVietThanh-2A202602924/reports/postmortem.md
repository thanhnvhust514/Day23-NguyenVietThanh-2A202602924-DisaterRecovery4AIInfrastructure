# Blameless Postmortem — DR Drill Lab 23

Ngày 09/10/2026, Windows PowerShell; primary A, standby B, Edge local, backend fs. Phương pháp manual_stop: ghi mốc rồi Ctrl+C service A. Công cụ trả valid:true, warnings:[], phục hồi bằng B. Kết quả này chưa chứng minh drill Linux netblock --mock hoặc outage toàn bộ hạ tầng cloud.

## 1. Timeline

Giờ ISO dưới đây dùng UTC+07:00; raw iso dùng UTC. Timestamp số là nguồn tính khoảng thời gian.

| ISO time (UTC+07:00) | +giây | Sự kiện | Evidence |
|---|---|---|---|
| 2026-10-09T11:57:25.854+07:00 | 0 | Ghi mốc outage A; other_alive:true | `chaos/chaos-events.jsonl:7` |
| 2026-10-09T11:57:32.939+07:00 | 7.085 | Request lỗi đầu, HTTP 503, ConnectTimeout | `reports/drill-2-withdr.jsonl:93` |
| 2026-10-09T11:57:50.101+07:00 | 24.246 | Health checker A UNHEALTHY sau 3 lỗi | `reports/health-events.jsonl:5` |
| 2026-10-09T11:58:04.677+07:00 | 38.823 | Snapshot được dùng khi restore; chu kỳ 30 giây | `reports/replication.jsonl:9` |
| 2026-10-09T11:58:32.252+07:00 | 66.398 | Runbook hoàn tất xác nhận lại outage | `reports/runbook-run.jsonl:8` |
| 2026-10-09T11:58:32.253+07:00 | 66.399 | Incident được ghi; trễ 66.399 giây so mốc outage | `reports/runbook-run.jsonl:9` |
| 2026-10-09T11:58:32.254+07:00 | 66.399 | Chấp nhận qua auto, đọc operator_confirmed_at trong bước 3 | `reports/runbook-run.jsonl:10` |
| 2026-10-09T11:58:32.456+07:00 | 66.602 | Verify B, pool warm, 275 documents trước restore | `reports/failover-events.jsonl:23` |
| 2026-10-09T11:58:32.471+07:00 | 66.617 | Restore xong; RPO 26.01 giây, 13 documents | `reports/failover-events.jsonl:24` |
| 2026-10-09T11:58:32.472+07:00 | 66.618 | Scale full | `reports/failover-events.jsonl:25` |
| 2026-10-09T11:58:39.084+07:00 | 73.230 | B ready, 325 documents, weights có, waited_s:6.609 | `reports/failover-events.jsonl:26` |
| 2026-10-09T11:58:39.086+07:00 | 73.232 | Cutover sau readiness | `reports/failover-events.jsonl:27` |
| 2026-10-09T11:58:39.310+07:00 | 73.456 | 10 request trực tiếp B đều OK; p95 16ms | `reports/runbook-run.jsonl:13` |
| 2026-10-09T11:58:45.317+07:00 | 79.462 | Request OK đầu qua Edge từ B: resolved theo công cụ | `reports/drill-2-withdr.jsonl:125` |
| 2026-10-09T12:00:46.456+07:00 | 200.601 | Request cuối vẫn OK từ B | `reports/drill-2-withdr.jsonl:367` |

Ảnh hưởng: 32 request lỗi liên tiếp, seq 92–123, trong 367 request toàn drill. Không thấy lỗi từ request phục hồi đến cuối file. Evidence: `reports/drill-2-withdr.jsonl:93`, `reports/drill-2-withdr.jsonl:124`, `reports/drill-2-withdr.jsonl:125`, `reports/drill-2-withdr.jsonl:367`. Timestamp request là lúc gửi; response đến sau latency.

## 2. RTO/RPO và gap analysis

| Chỉ số | Mục tiêu | Đo được | Gap = đo được − mục tiêu | Evidence |
|---|---|---|---|---|
| RTO | 300s | 79.5s | −220.5s; đạt, dư 220.5s | `chaos/chaos-events.jsonl:7`, `reports/drill-2-withdr.jsonl:125` |
| RPO | 300s | 26.01s / 13 documents | −273.99s theo thời gian; chưa đặt ngưỡng documents | `reports/failover-events.jsonl:24` |

Khoảng lớn nhất: từ detection đến verify target là 42.355s, khoảng 53.3% RTO. Nó gồm chờ thao tác, xác nhận lại outage trong runbook và verify target; chưa đủ telemetry để gán toàn bộ cho người vận hành. Evidence: `reports/health-events.jsonl:5`, `reports/runbook-run.jsonl:8`, `reports/failover-events.jsonl:23`.

Các phần khác: detection thực tế 24.246s; restore/RPO/scale 0.016s; warm-up/readiness 6.612s; cutover 0.002s; từ cutover đến request OK 6.230s. Tổng timestamp gốc 79.4622535s → 79.5s. Breakdown đầy đủ trong rto-evidence.md. 6.230s không phải TTL thuần; gồm request đang chờ và lấy mẫu traffic.

RPO tính tại restore, không phải tuổi snapshot: 1791521910.6393976 − 1791521884.6308985 = 26.0084991s, làm tròn 26.01s; 13 documents trên A mới hơn watermark của bản restore (`reports/failover-events.jsonl:24`). Ingest/replication tiếp tục đọc ghi SQLite A sau khi serving A dừng; snapshot còn tạo ở +38.823s (`reports/replication.jsonl:9`). Vì vậy đây là outage serving, không phải mất toàn bộ disk/region. 13 documents thiếu trên B tại restore chưa đồng nghĩa mất vĩnh viễn.

## 3. Root cause — 5 whys

1. Vì sao người dùng nhận lỗi? Edge vẫn trỏ A khi A không đáp ứng; timeout và HTTP 503. Evidence: `reports/drill-2-withdr.jsonl:93`.
2. Vì sao B chưa phục vụ ngay? Standby pool warm, cần snapshot mới và scale full trước readiness. Evidence: `reports/failover-events.jsonl:23`, `reports/failover-events.jsonl:24`, `reports/failover-events.jsonl:25`.
3. Vì sao detection chưa dẫn đến phục hồi ngay? Health checker chỉ ghi transition; runbook được gọi riêng và xác nhận lại ba probe. Evidence: `reports/health-events.jsonl:5`, `reports/runbook-run.jsonl:8`.
4. Vì sao điều phối chậm? Workflow nhiều terminal chưa có handoff alert → một lần confirm và telemetry rõ cho thời gian chờ. Khoảng 42.355s đo được, nhưng phần nào do chờ người/phần nào do probe chưa được tách. Evidence: `reports/health-events.jsonl:5`, `reports/failover-events.jsonl:23`.
5. Vì sao restore thiếu 13 documents? Replication chu kỳ 30 giây; watermark snapshot cũ hơn latest timestamp A 26.01 giây. Evidence: `reports/replication.jsonl:9`, `reports/failover-events.jsonl:24`.

Nguyên nhân cần cải thiện là standby chưa ready đầy đủ, điều phối chưa nối với detection và replication bất đồng bộ. Không quy lỗi cá nhân. Với mất region thật, restore và phép tính RPO có thể thất bại: lab dùng snapshot và primary SQLite trên cùng máy. Cần replica độc lập và ledger/watermark để đánh giá mất dữ liệu khi A không đọc được.

## 4. Action items — kế hoạch, chưa thực hiện

Owner là vai trò đề xuất để IC phân công. Deadline theo giờ Việt Nam. Tác động dự kiến cần kiểm chứng bằng drill mới.

| # | Action | Owner | Deadline | Tác động dự kiến / tiêu chí xác nhận |
|---|---|---|---|---|
| 1 | Nối alert với confirm một lần, ghi runbook start/operator wait; giữ circuit breaker | DR/SRE owner | 2026-10-12 | Nhắm giảm ít nhất 20 giây trong khoảng điều phối 42.355 giây; chứng minh qua 3 drill |
| 2 | Thử replication mỗi 10 giây, snapshot SQLite nhất quán và version độc lập | State owner | 2026-10-13 | Budget lag giảm 20 giây; đo lại RPO/documents và I/O, không coi là kết quả đã đạt |
| 3 | Đánh giá giữ B full hoặc rút warm-up kèm chi phí | Serving owner | 2026-10-14 | Có thể giảm khoảng 6.6 giây pha warm-up; kiểm chứng capacity/drill |
| 4 | Windows launcher/chaos có PID đúng, ghi mốc sau khi thao tác thành công và verify outage | Lab tooling owner | 2026-10-12 | Giảm sai số manual_stop; chứng minh service thật sự dừng và request fail |
| 5 | Drill mất primary storage trong môi trường tách biệt, phục hồi từ replica độc lập | DR owner + State owner | 2026-10-16 | Restore khi A không đọc được, đo RPO qua ledger; không suy ra từ drill serving này |

## 5. Ba câu hỏi bắt buộc

1. Interval × threshold = 5 × 3 = 15s, khoảng 18.9% RTO 79.5s. Detection thực tế 24.246s, khoảng 30.5%. 15s là budget công cụ lab báo, không phải định luật thời điểm phát hiện sớm nhất trong mọi pha poll; timeout và độ trễ thao tác ảnh hưởng kết quả. Evidence: `reports/health-events.jsonl:5`, `reports/drill-2-withdr.jsonl:125`.
2. Nếu interval 1 giây, threshold 3, budget giảm 15 xuống 3 giây: giảm lý thuyết 12 giây; nếu mọi phần khác giữ nguyên, RTO khoảng 67.5 giây. Chưa đo nên không cam kết. Probe tăng, nhạy với lỗi ngắn; timeout 2 giây và poll tuần tự có thể kéo dài chu kỳ. Giữ xác nhận liên tiếp và chặn failover hai chiều, rồi drill lại.
3. Nếu outage 6 giờ và A mất dữ liệu vĩnh viễn, 13 documents là ghi có trên A nhưng chưa vào bản restore tại lần này; cần replay/re-ingest từ nguồn khác hoặc khách hàng gửi lại. Không nhân 13 với 6 giờ để suy ra mất dữ liệu. Trong drill này A disk còn đọc được và ingest tiếp tục, nên chưa chứng minh 13 documents mất vĩnh viễn. Evidence: `reports/failover-events.jsonl:24`, `reports/replication.jsonl:9`.

## 6. Bảo toàn hồ sơ

Giữ raw logs cùng ba reports. Chỉ nộp năm log evidence theo ngoại lệ .gitignore; log chaos và runbook giữ local, cung cấp riêng khi cần đối chiếu timeline. Không sửa timestamp để hợp thức hóa phương pháp. Người chấm cần xác nhận chấp nhận manual_stop nếu yêu cầu netblock --mock.
