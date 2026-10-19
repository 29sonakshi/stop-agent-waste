# Evidence 3 — business impact, computed not estimated

_Executed against `main.tiffin_agent_waste` on Databricks (e2-dogfood) — 2026-10-19 21:08 UTC. Every table below is real query output, not illustrative._

Every figure in the deck traces to one of these queries. Nothing is hardcoded: token counts come from the OTel spans, prices from `bronze_model_pricing`, and the window is the 90 days of trace history.

### Headline: spend and waste across all six agents

```sql
SELECT round(sum(spend_usd),2) AS spend_90d,
       round(sum(waste_usd),2)  AS waste_90d,
       round(sum(spend_usd)/90*365,0) AS spend_per_year,
       round(sum(waste_usd)/90*365,0) AS waste_per_year,
       round(100*sum(waste_usd)/sum(spend_usd),1) AS waste_pct
FROM main.tiffin_agent_waste.agent_waste_gold
```

| spend_90d | waste_90d | spend_per_year | waste_per_year | waste_pct |
|---|---|---|---|---|
| 130295.61 | 26979.89 | 528421.0 | 109418.0 | 20.7 |

_1 row(s)_

### Per agent — who is burning the budget

```sql
SELECT agent_id, max(display_name) AS display_name, max(owner_team) AS owner_team,
       sum(tool_calls) AS tool_calls,
       round(sum(spend_usd)/90*365,0) AS spend_per_year,
       round(sum(waste_usd)/90*365,0) AS waste_per_year,
       round(100*sum(waste_usd)/sum(spend_usd),1) AS waste_pct
FROM main.tiffin_agent_waste.agent_waste_gold GROUP BY agent_id ORDER BY waste_per_year DESC
```

| agent_id | display_name | owner_team | tool_calls | spend_per_year | waste_per_year | waste_pct |
|---|---|---|---|---|---|---|
| order-support-agent | Order Support Agent | support-ai | 442399 | 242829.0 | 54484.0 | 22.4 |
| delivery-routing-agent | Delivery Routing Agent | logistics-ai | 319610 | 192290.0 | 38218.0 | 19.9 |
| fraud-detection-agent | Fraud Detection Agent | risk-ai | 232294 | 68622.0 | 10186.0 | 14.8 |
| customer-retention-agent | Customer Retention Agent | growth-ai | 84031 | 18541.0 | 5040.0 | 27.2 |
| menu-recommendation-agent | Menu Recommendation Agent | discovery-ai | 68225 | 2061.0 | 803.0 | 39.0 |
| merchant-onboarding-agent | Merchant Onboarding Agent | merchant-ai | 153882 | 4079.0 | 687.0 | 16.8 |

_6 row(s)_

### The prioritisation insight: cost per failed call varies ~100x

Ranking by error **count** would put `update_merchant` (1,167 failures) above `list_deliveries`. Ranking by **money** inverts that: $0.02 vs $2.18 per failed call. This is why the fix queue is ordered by dollars.

```sql
SELECT tool_server, tool_name, owner_team, failed_calls,
       waste_usd_per_failed_call, waste_usd_per_year, fix_rank
FROM main.tiffin_agent_waste.fix_queue ORDER BY waste_usd_per_failed_call DESC
```

| tool_server | tool_name | owner_team | failed_calls | waste_usd_per_failed_call | waste_usd_per_year | fix_rank |
|---|---|---|---|---|---|---|
| payment-gateway | create_refund | payments-platform | 3010 | 2.3505 | 28693.43 | 2 |
| delivery-tracking-api | list_deliveries | logistics-platform | 3844 | 2.1804 | 33992.15 | 1 |
| analytics-aggregator | batch_query | data-platform | 1942 | 1.2933 | 10186.26 | 4 |
| geolocation-service | distance_matrix | logistics-platform | 955 | 1.0912 | 4226.23 | 6 |
| order-history-api | list_orders | orders-platform | 5959 | 1.0672 | 25790.13 | 3 |
| promo-engine | create_promo | growth-platform | 4454 | 0.279 | 5040.09 | 5 |
| menu-service | upsert_menu_item | merchant-platform | 1155 | 0.1234 | 577.89 | 8 |
| merchant-catalog-api | get_merchant | merchant-platform | 1655 | 0.1197 | 803.26 | 7 |
| merchant-catalog-api | update_merchant | merchant-platform | 1167 | 0.023 | 109.02 | 9 |

_9 row(s)_
