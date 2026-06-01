# Polymarket Wallet Analyzer

App Streamlit để phân tích một ví Polymarket bằng public Data API: trades, positions, closed positions, activity, current value và traded volume.

## Tính năng

- Fetch dữ liệu ví từ `https://data-api.polymarket.com`.
- Gom dữ liệu theo `conditionId`/market.
- Tính cost basis, proceeds, current value, realized PnL, unrealized PnL, total PnL và ROI.
- Xếp hạng market theo PnL, xem PnL theo category và phân phối ROI.
- **Skill score 0–100 (skilled vs ăn may)** với breakdown từng tiêu chí, dựa trên 5 nhóm bằng chứng:
  1. **Edge so với giá vào lệnh** – win rate kèo đã resolve so với giá mua trung bình (edge per share). Đây là tín hiệu kỹ năng cốt lõi của prediction market: mua được outcome bị định giá thấp.
  2. **Ý nghĩa thống kê** – khoảng tin cậy 95% (bootstrap) và t-statistic của ROI theo từng market.
  3. **Ổn định theo thời gian** – tỉ lệ tháng có lãi (lợi nhuận bền hay dồn vào một đợt).
  4. **Số quyết định độc lập** – gom market tương quan theo `eventSlug` để ước lượng cỡ mẫu hiệu dụng.
  5. **Tập trung & rủi ro** – top 1/top 3 contribution, Gini/HHI, Sharpe (ROI) và profit factor.
- Phân loại: `skilled` / `lucky (one-hit wonder)` / `inconclusive` / `unprofitable`, kèm mức độ tin cậy và **cảnh báo khi dữ liệu bị cắt** (chạm `max_records`).
- Biểu đồ PnL theo tháng & lũy kế.
- Export bảng market ra CSV.

## Cài đặt

```bash
cd /home/ubuntu/polymarket-wallet-analyzer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Chạy app

```bash
streamlit run app.py
```

Nhập ví dạng `0x...` ở sidebar rồi bấm **Phân tích ví**.

## Deploy hiện tại

App đã được cấu hình chạy qua Nginx ở port `9765`:

- URL public: `http://158.180.25.244:9765`
- Streamlit service: `polymarket-wallet-analyzer.service`
- Streamlit internal: `127.0.0.1:8501`
- Nginx config: `/etc/nginx/sites-available/polymarket-wallet-analyzer`
- Firewall service: `polymarket-wallet-analyzer-firewall.service`
- Deploy templates: `deploy/nginx/` và `deploy/systemd/`

Lệnh quản trị nhanh:

```bash
sudo systemctl status polymarket-wallet-analyzer.service
sudo systemctl restart polymarket-wallet-analyzer.service
sudo systemctl reload nginx
curl http://127.0.0.1:9765/_stcore/health
```

## Chạy CLI

```bash
python -m polymarket_wallet_analyzer.cli 0x296bd652f74deac6a8bd9bcb04265f3a65fd2cf2 --max-records 5000 --csv exports/report.csv
```

## Chạy test

```bash
pytest
```

## Cách tính chính

- `cost`: ưu tiên `totalBought * avgPrice` từ positions/closed positions; nếu không có thì fallback sang notional của lệnh BUY.
- `realized_pnl`: tổng `realizedPnl` từ open positions và closed positions.
- `unrealized_pnl`: ưu tiên `cashPnl` từ open positions; nếu thiếu thì dùng `currentValue - initialValue`.
- `pnl`: `realized_pnl + unrealized_pnl`.
- `roi`: `pnl / cost` khi cost lớn hơn 0.
- `resolved` / `won`: một market được coi là đã resolve khi có closed position hoặc activity `REDEEM`; kết quả thắng/thua đọc từ `curPrice` (gần 0/1) hoặc dấu của PnL. Open position (kể cả longshot giá ~0) **không** bị coi là đã resolve để tránh nhầm.
- `edge_per_share`: `outcome(0/1) − giá vào lệnh`, trung bình trên các kèo đã resolve – đo việc mua dưới giá.
- `skill_score` (0–100): trung bình có trọng số của 6 thành phần (ý nghĩa thống kê 25, edge 20, ổn định theo thời gian 15, số quyết định độc lập 15, không phụ thuộc 1 kèo 15, hiệu suất điều chỉnh rủi ro 10); trọng số được chuẩn hóa lại khi thiếu dữ liệu cho một thành phần.
- Verdict: `lucky` khi có lãi nhưng tập trung vào số ít kèo và điểm < 60; `skilled` khi điểm ≥ 65 và không tập trung; `unprofitable` khi tổng PnL ≤ 0; còn lại là `inconclusive`.
- Hai cờ cũ `one-hit wonder` / `probably skilled` vẫn được giữ để tương thích ngược.

## Lưu ý

- Data API có giới hạn `limit`/`offset`; slider `max_records` giúp cân bằng tốc độ và độ đầy đủ dữ liệu.
- Category là heuristic dựa trên title/slug/event slug, không phải taxonomy chính thức.
- Đây là công cụ phân tích dữ liệu, không phải lời khuyên tài chính.
