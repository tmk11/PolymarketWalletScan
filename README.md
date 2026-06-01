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

Token resolver được bật mặc định trong CLI để giảm record chỉ có token/asset:

```bash
python -m polymarket_wallet_analyzer.cli 0x... --resolve-tokens
python -m polymarket_wallet_analyzer.cli 0x... --no-resolve-tokens
python -m polymarket_wallet_analyzer.cli 0x... --token-cache-path .cache/polymarket_token_resolver_cache.json
```

## Chạy kiểm tra

```bash
pytest
ruff check .
mypy .
```

## Cách tính chính

### PnL

- `trading_pnl = realized_pnl + unrealized_pnl`: PnL từ trading prediction market, không bao gồm reward mặc định.
- `realized_pnl`: PnL đã chốt. Analyzer ưu tiên reconstruction từ trades/activity khi đủ dữ liệu; nếu không đủ thì dùng best-effort từ `closed_positions` hoặc `positions`, nhưng tránh double-count khi cùng market có cả open và closed records.
- `unrealized_pnl`: PnL tạm tính của open positions, ưu tiên `cashPnl`/`unrealizedPnl`, fallback `currentValue - initialValue`.
- `rewards_pnl`: rewards/maker rebates/referral rebates, tách riêng khỏi trading skill.
- `total_pnl_including_rewards = trading_pnl + rewards_pnl`: dùng để xem tổng tiền, không phải metric mặc định để chấm skill.
- Nếu market có cả open positions và closed positions đều chứa `realizedPnl`, report thêm warning `possible_overlap_realized_pnl` và chỉ chọn một nguồn realized PnL để tránh cộng trùng.

### ROI

- `roi_cost_basis = trading_pnl / total_cost_basis`: ROI trên vốn gốc/cost basis ước tính.
- `roi_buy_notional = trading_pnl / total_buy_notional`: ROI trên toàn bộ tiền đã BUY, bao gồm mua bán nhiều vòng; thường bảo thủ hơn khi ví turnover nhiều.
- `roi_max_capital_at_risk = trading_pnl / total_max_capital_at_risk`: ROI trên vốn rủi ro lớn nhất tại một thời điểm; nếu thiếu full trade timeline thì là best-effort và report sẽ cảnh báo.
- `roi_ex_top1` / `roi_ex_top3` / `roi_ex_top5`: ROI sau khi loại các market thắng lớn nhất, giúp kiểm tra ví còn edge không nếu bỏ outlier.
- Các alias cũ như `total_pnl`, `total_roi`, `roi_ex_top1`, `top1_contribution` vẫn tồn tại để tương thích, nhưng code mới ưu tiên field rõ nghĩa hơn.

### Market key

- Analyzer ưu tiên market-level identifiers: `conditionId`, `condition_id`, `conditionID`, `marketId`, `market_id`, `marketSlug`, `slug`.
- `asset`/`token_id` là outcome-level token như YES/NO, nên không được dùng làm market key nếu chưa map được về `conditionId`.
- Record không map được sẽ vào `unmapped_records`, tăng `unmapped_records_count`, có warning, và không dùng để kết luận skill chính thức.

### Token metadata resolver

- Một số record từ Data API chỉ có `asset`, `token_id`, `tokenId`, `clobTokenId` hoặc `clob_token_id`. Các field này là outcome-level token, không phải market-level key.
- Resolver trích token theo thứ tự an toàn: `clobTokenId`, `clob_token_id`, `tokenId`, `token_id`, `asset`.
- Resolver thử map token về `conditionId` bằng CLOB metadata: trước tiên `GET https://clob.polymarket.com/markets/{token_id}`, sau đó fallback `GET https://clob.polymarket.com/book?token_id={token_id}` khi cần lấy condition từ order book.
- Chỉ record có `resolver_confidence = "high"` mới được đưa vào PnL/ROI/skill. Nếu resolve fail hoặc confidence không high, record vẫn nằm trong `unmapped_records` và bị loại khỏi kết luận chính thức.
- Cache mặc định nằm ở `.cache/polymarket_token_resolver_cache.json`, giúp tránh gọi API lặp lại cho cùng token.
- Summary có thêm `resolved_from_token_count`, `resolved_from_token_high_confidence_count`, `low_confidence_resolved_count`, `token_resolver_enabled`, `token_resolver_cache_hits`, `token_resolver_api_calls`, `token_resolver_failures`.
- Resolved token records là best-effort: nếu API lỗi, timeout, JSON invalid hoặc thiếu `conditionId`, analyzer không crash và không gom bừa market.

### Top contribution

- `top1_contribution_net_pnl = top1_positive_pnl / net_total_trading_pnl` có thể lớn hơn 100% nếu market thắng lớn đang bù lỗ cho các market khác. Ví dụ top1 lời $1,500, các market khác lỗ $500, net PnL $1,000 → contribution 150%. Đây là tín hiệu one-hit wonder, không phải bug.
- `top1_share_of_gross_profit = top1_positive_pnl / gross_positive_pnl` dùng tổng gross profit làm mẫu số nên dễ đọc hơn và nằm trong 0–100%.

### Verdict

- `insufficient_data`: quá ít market hoặc dữ liệu không đủ để kết luận. Với sample nhỏ (<10 market), analyzer không kết luận chắc là lucky dù có pattern one-hit; thay vào đó thêm warning `low_sample_one_hit_pattern_detected` nếu thấy tín hiệu này.
- `lucky_or_one_hit_wonder`: ví có lời nhưng lợi nhuận phụ thuộc quá nhiều vào một vài market lớn, hoặc ROI ex-top1/ex-top3 âm trên sample đủ lớn.
- `category_skilled`: không skilled toàn bộ nhưng có dấu hiệu skill ở một category cụ thể.
- `skilled`: có lợi nhuận phân tán, đủ mẫu, `roi_ex_top1`/`roi_ex_top3` vẫn ổn, win rate/median ROI hợp lý và confidence không thấp.
- `unprofitable`: trading PnL không dương trên sample đủ để đọc.
- `resolved` / `won`: một market được coi là resolved khi có closed position hoặc activity `REDEEM`; open longshot chưa resolve không bị coi là thua.
- `outcome_level_edge`: edge tính theo `conditionId + outcome/tokenId`, không trộn YES và NO trong cùng market. PnL vẫn gom ở market-level.

## Vì sao không chỉ nhìn ROI tổng?

- ROI tổng có thể cao vì một market nhỏ thắng lớn hoặc vì ví đang giữ vị thế unrealized chưa chốt.
- `roi_ex_top1` / `roi_ex_top3` trả lời câu hỏi: nếu bỏ các market thắng lớn nhất, ví còn có edge không?
- Không nên copy ví chỉ vì leaderboard PnL cao: PnL có thể đến từ reward, từ một event correlated, từ unrealized PnL, từ sample quá nhỏ, hoặc từ dữ liệu bị truncate. Hãy xem `confidence_level`, `unmapped_records_count`, `roi_ex_top1`, `roi_ex_top3`, category breakdown và outcome-level edge.

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
