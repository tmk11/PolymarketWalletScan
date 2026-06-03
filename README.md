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

### Phong độ gần đây / copy risk

- `recent_buy_trade_3d` / `recent_trade_3d` / `recent_market_3d`: phong độ 3 ngày gần nhất, là phần chính để đánh giá copy risk ngắn hạn.
- `recent_buy_trade_windows`: mark-to-market estimate cho các cửa sổ BUY gần nhất (`10`, `25`, `50`). BUY-only phù hợp hơn để đánh giá rủi ro copy entry mới; SELL thường là lệnh thoát vị thế. Các cửa sổ này là bảng tham khảo phụ sau 3D.
- `recent_trade_windows`: mark-to-market estimate cho tất cả trade gần nhất, dùng như tín hiệu phụ.
- `recent_market_windows`: PnL market-level gần nhất, sắp theo timestamp mới nhất.
- `recent_7d_trade_count`, `recent_7d_trade_notional`, `recent_7d_avg_trades_per_day`, `recent_7d_frequency_label`: tần suất trade trong 7 ngày gần nhất, tính từ dữ liệu `/trades` cộng với `/activity` type `TRADE` đã dedupe, tương đối theo timestamp trade mới nhất trong dữ liệu ví.
- `recent_copy_risk_level`: cảnh báo `high`/`medium` nếu 3 ngày gần nhất hoặc các cửa sổ BUY gần nhất đang lỗ theo mark-to-market estimate. Đây là cảnh báo cho người muốn copy ví, không thay thế verdict skill dài hạn.

### Skill score vs Copy suitability score

- `raw_skill_score`: điểm bằng chứng dài hạn trước khi cap, dựa trên meaningful win rate/median ROI, outcome-level edge, ổn định theo tháng, số event độc lập, concentration và profit factor.
- `skill_score`: điểm skill dài hạn đã điều chỉnh rủi ro. Điểm này có thể bị cap nếu verdict là `lucky_or_one_hit_wonder`, `insufficient_data`, `inconclusive`, confidence thấp, dữ liệu bị truncate, hoặc `recent_copy_risk_level` đang `medium/high`.
- `score_adjustment`: liệt kê các cap/reason đã áp dụng. Vì vậy một ví có raw score cao vẫn có displayed score thấp nếu lợi nhuận tập trung, dữ liệu yếu, hoặc phong độ gần đây xấu.
- `copy_suitability_score`: điểm riêng cho câu hỏi “có nên copy ví này ngay không?”. Điểm này ưu tiên 3 ngày gần nhất: BUY mark-to-market, market-level PnL gần đây, copy risk, tần suất trade, rồi mới đến skill dài hạn và chất lượng dữ liệu.
- `copy_suitability_score` thấp không có nghĩa ví không có skill dài hạn; nó nghĩa là entry/copy hiện tại rủi ro hơn, ví dụ 3 ngày gần nhất đang lỗ hoặc dữ liệu gần đây không đủ rõ.

### Win-rate padding detector

- `market_win_rate` là raw win rate nên có thể bị làm đẹp bằng nhiều market “thắng chắc” nhưng lời vài cent.
- `meaningful_market_win_rate` chỉ tính market thắng có PnL và ROI đủ đáng kể, mặc định `pnl >= $1` và `roi_buy_notional >= 2%`.
- `low_value_winning_markets` là số market thắng nhưng PnL/ROI quá nhỏ; `low_value_winning_markets_ratio` đo tỷ lệ nhóm này trên tổng market thắng.
- `low_value_wins_profit_share` cho biết nhóm thắng nhỏ này đóng góp bao nhiêu vào gross profit; nếu win nhiều nhưng gần như không đóng góp PnL thì raw win rate không đáng tin.
- `win_rate_quality_gap = market_win_rate - meaningful_market_win_rate`; gap lớn là dấu hiệu win-rate padding.
- `win_rate_padding_suspected` bật khi ví/category có nhiều low-value wins, nhóm đó đóng góp rất ít PnL, và gap raw-vs-meaningful lớn. Khi flag này bật, analyzer không kết luận `skilled` chỉ nhờ raw win rate và sẽ cap `skill_score`.

### Metric gaming flags khác

- `small_bet_roi_padding`: nhiều kèo vốn rất nhỏ có ROI cao, làm ROI trung bình/unweighted nhìn đẹp nhưng đóng góp PnL thấp.
- `correlated_cluster`: nhiều market nằm trong cùng `eventSlug` hoặc cùng cụm correlated, làm số market nhìn đa dạng hơn thực tế. Hãy xem `effective_bets`, `market_to_effective_bets_ratio`, `top_event_cluster_profit_share`.
- `tail_risk`: ví thắng rất nhiều khoản nhỏ nhưng có loss lớn hiếm gặp; đây là kiểu “pennies in front of a steamroller”. Hãy xem `largest_loss_to_median_win`.
- `unrealized_pnl_dominance`: PnL phụ thuộc nhiều vào open/unrealized positions, chưa phải lợi nhuận đã chốt.
- `reward_dependency`: lợi nhuận phụ thuộc nhiều vào rewards/maker rebates, không nên tính như trading skill.
- `recent_performance_divergence`: long-term PnL đẹp nhưng recent BUY/market windows đang xấu, rủi ro nếu copy ngay.
- Các flag này được gom trong `metric_gaming_flags`; nếu có flag nghiêm trọng, `skill_score` có thể bị cap dù raw score cao.

### Verdict

- `insufficient_data`: quá ít market hoặc dữ liệu không đủ để kết luận. Với sample nhỏ (<10 market), analyzer không kết luận chắc là lucky dù có pattern one-hit; thay vào đó thêm warning `low_sample_one_hit_pattern_detected` nếu thấy tín hiệu này.
- `lucky_or_one_hit_wonder`: ví có lời nhưng lợi nhuận phụ thuộc quá nhiều vào một vài market lớn, hoặc ROI ex-top1/ex-top3 âm trên sample đủ lớn.
- `category_skilled`: không skilled toàn bộ nhưng có dấu hiệu skill ở một category cụ thể.
- `skilled`: có lợi nhuận phân tán, đủ mẫu, `roi_ex_top1`/`roi_ex_top3` vẫn ổn, meaningful win rate/median ROI hợp lý, không bị flag win-rate padding và confidence không thấp.
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
