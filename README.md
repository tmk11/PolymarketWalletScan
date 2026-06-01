# Polymarket Wallet Analyzer

App Streamlit để phân tích một ví Polymarket bằng public Data API: trades, positions, closed positions, activity, current value và traded volume.

## Tính năng

- Fetch dữ liệu ví từ `https://data-api.polymarket.com`.
- Gom dữ liệu theo market-level key an toàn (`conditionId`, `marketId`, `slug`); không dùng `asset/token_id` làm market key nếu chưa map được về `conditionId`.
- Tính riêng `trading_pnl`, `rewards_pnl`, `realized_pnl`, `unrealized_pnl`, nhiều loại ROI và cảnh báo record unmapped.
- Xếp hạng market theo PnL, xem PnL theo category và phân phối ROI.
- **Skill score 0–100 (skilled vs ăn may)** với breakdown từng tiêu chí, dựa trên 5 nhóm bằng chứng:
  1. **Edge so với giá vào lệnh** – win rate kèo đã resolve so với giá mua trung bình (edge per share). Đây là tín hiệu kỹ năng cốt lõi của prediction market: mua được outcome bị định giá thấp.
  2. **Ý nghĩa thống kê** – khoảng tin cậy 95% (bootstrap) và t-statistic của ROI theo từng market.
  3. **Ổn định theo thời gian** – tỉ lệ tháng có lãi (lợi nhuận bền hay dồn vào một đợt).
  4. **Số quyết định độc lập** – gom market tương quan theo `eventSlug` để ước lượng cỡ mẫu hiệu dụng.
  5. **Tập trung & rủi ro** – top 1/top 3 contribution, Gini/HHI, Sharpe (ROI) và profit factor.
- Phân loại: `skilled` / `category_skilled` / `lucky_or_one_hit_wonder` / `inconclusive` / `unprofitable` / `insufficient_data`, kèm `confidence_level` và cảnh báo khi dữ liệu bị cắt.
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
python -m polymarket_wallet_analyzer.cli 0x296bd652f74deac6a8bd9bcb04265f3a65fd2cf2 --max-records 5000 --csv exports/report.csv --json exports/report.json
```

## Chạy kiểm tra

```bash
pytest
ruff check .
mypy .
```

## Cách tính chính

- `trading_pnl`: PnL từ giao dịch prediction market, bằng `realized_pnl + unrealized_pnl`, không bao gồm reward mặc định.
- `rewards_pnl`: reward/maker rebate/referral rebate từ activity, tách riêng khỏi trading skill.
- `total_pnl_including_rewards`: `trading_pnl + rewards_pnl`, hữu ích để xem tổng tiền nhưng không dùng mặc định để chấm skill.
- `realized_pnl`: phần đã chốt từ SELL/REDEEM/closed records. Nếu một market có cả open và closed records, app không cộng trùng `realizedPnl` từ open position.
- `unrealized_pnl`: phần đang mở, ưu tiên `cashPnl`, fallback `currentValue - initialValue`.
- `asset/token_id`: là outcome-level token (YES/NO). App chỉ dùng token để map về `conditionId`; nếu không map được thì record vào `unmapped_records` và không dùng cho verdict chính thức.
- `roi_cost_basis`: `trading_pnl / cost_basis`. Đây là ROI trên vốn gốc/cost basis ước tính theo market.
- `roi_buy_notional`: `trading_pnl / total_buy_notional`. Đây là ROI trên toàn bộ tiền đã BUY, nên thấp hơn khi ví mua bán nhiều vòng.
- `roi_max_capital_at_risk`: `trading_pnl / max_capital_at_risk`. Nếu thiếu lịch sử trades đầy đủ, trường này là best-effort và report sẽ báo `max_capital_at_risk_estimated`.
- `resolved` / `won`: một market được coi là đã resolve khi có closed position hoặc activity `REDEEM`; kết quả thắng/thua đọc từ `curPrice` (gần 0/1) hoặc dấu của PnL. Open position (kể cả longshot giá ~0) **không** bị coi là đã resolve để tránh nhầm.
- `outcome_level_edge`: edge tính theo `conditionId + outcome/tokenId`, không trộn YES và NO trong cùng market. PnL vẫn gom ở market-level.
- `edge_per_share`: `outcome(0/1) − giá vào lệnh`, trung bình trên các outcome đã resolve – đo việc mua dưới giá.
- `skill_score` (0–100): trung bình có trọng số của 6 thành phần (ý nghĩa thống kê 25, edge 20, ổn định theo thời gian 15, số quyết định độc lập 15, không phụ thuộc 1 kèo 15, hiệu suất điều chỉnh rủi ro 10); trọng số được chuẩn hóa lại khi thiếu dữ liệu cho một thành phần.
- `market_win_rate`, `median_market_roi`, `mean_market_roi_unweighted`, `mean_market_roi_cost_weighted`, `profit_factor`, `hhi_profit_concentration`, `gini_profit_concentration`, `effective_bets`, `max_drawdown`, `profitable_months_count`, `total_active_months` giúp kiểm tra độ ổn định thay vì chỉ nhìn leaderboard PnL.
- Verdict: `skilled` khi ví có lãi bền trên nhiều market, ROI buy-notional dương sau khi bỏ top1/top3, win rate/median ROI ổn và confidence không thấp. `category_skilled` khi edge chỉ rõ ở một vài category. `lucky_or_one_hit_wonder` khi lãi phụ thuộc top market/top3. `unprofitable` khi trading PnL không dương. `insufficient_data` khi quá ít dữ liệu hoặc unmapped quá nhiều.
- Hai cờ cũ `one-hit wonder` / `probably skilled` vẫn được giữ để tương thích ngược; `skill.legacy_verdict` map verdict mới về nhãn cũ khi cần.

## Vì sao không chỉ nhìn ROI tổng?

- ROI tổng có thể cao vì một market nhỏ thắng lớn hoặc vì ví đang giữ vị thế unrealized chưa chốt.
- `roi_ex_top1` / `roi_ex_top3` trả lời câu hỏi: nếu bỏ các market thắng lớn nhất, ví còn có edge không?
- `top1_contribution_net_pnl = top1_positive_pnl / net_total_pnl` có thể lớn hơn 100%. Ví dụ top1 lời $1,500 nhưng các market khác lỗ $500, net PnL là $1,000, nên contribution là 150%. Đây không phải bug; đó là tín hiệu one-hit wonder rất mạnh.
- `top1_share_of_gross_profit` dùng mẫu số là tổng các market lãi, nên nằm trong 0–100% và đo độ tập trung lợi nhuận theo cách dễ đọc hơn.
- Không nên copy ví chỉ vì leaderboard PnL cao: PnL có thể đến từ reward, từ một event correlated, từ unrealized PnL, hoặc từ sample quá nhỏ. Hãy xem `confidence_level`, `unmapped_records_count`, `roi_ex_top1`, `roi_ex_top3`, category breakdown và outcome-level edge.

## Cấu trúc JSON report

Report JSON có các nhóm chính:

- `summary`: PnL/ROI toàn ví, concentration, stability, confidence và verdict.
- `category_breakdown`: Weather, Politics, Sports, Crypto, Economy/Fed, Tech/AI, Geopolitics, Entertainment, Other.
- `top_winning_markets` / `top_losing_markets`: market-level PnL.
- `outcome_level_edge`: edge theo từng outcome/token.
- `unmapped_records_count` / `warnings`: dữ liệu bị thiếu key hoặc ước tính.

## Lưu ý

- Data API có giới hạn `limit`/`offset`; slider `max_records` giúp cân bằng tốc độ và độ đầy đủ dữ liệu.
- Category là heuristic dựa trên title/slug/event slug, không phải taxonomy chính thức.
- Đây là công cụ phân tích dữ liệu, không phải lời khuyên tài chính.
