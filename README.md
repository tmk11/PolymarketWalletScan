# Polymarket Wallet Analyzer

App Streamlit để phân tích một ví Polymarket bằng public Data API: trades, positions, closed positions, activity, current value và traded volume.

## Tính năng

- Fetch dữ liệu ví từ `https://data-api.polymarket.com`.
- Gom dữ liệu theo `conditionId`/market.
- Tính cost basis, proceeds, current value, realized PnL, unrealized PnL, total PnL và ROI.
- Xếp hạng market theo PnL, xem PnL theo category và phân phối ROI.
- Tính top 1/top 3 contribution, ROI sau khi bỏ top 1/top 3, win rate, median ROI.
- Flag `one-hit wonder` và `probably skilled` theo logic trong prompt.
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
- `one-hit wonder`: `top1_contribution > 50%` hoặc ROI âm sau khi bỏ top 1/top 3.
- `probably skilled`: ROI tổng > 5%, ít nhất 30 market, ROI ex-top1 dương, top1 contribution < 40%, win rate > 50%, median ROI > -2%.

## Lưu ý

- Data API có giới hạn `limit`/`offset`; slider `max_records` giúp cân bằng tốc độ và độ đầy đủ dữ liệu.
- Category là heuristic dựa trên title/slug/event slug, không phải taxonomy chính thức.
- Đây là công cụ phân tích dữ liệu, không phải lời khuyên tài chính.
