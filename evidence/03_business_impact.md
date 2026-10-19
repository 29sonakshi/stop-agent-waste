# Evidence 3: business impact, computed not estimated

_Executed against `main.tiffin_agent_waste` on Databricks (e2-dogfood) on 2026-10-19 22:26 UTC._

Every figure in the deck traces to one of these queries. Nothing is hardcoded: token counts come
from the OTel spans, prices from `bronze_model_pricing`, and the window is the 90 days of trace
history.

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

### Per agent: who is burning the budget, and who is over it

Two agents are running over their monthly budget, which is the business consequence of the waste rather than an abstraction.

```sql
SELECT agent_id, max(display_name) AS display_name, max(owner_team) AS owner_team,
       sum(tool_calls) AS tool_calls,
       round(sum(spend_usd)/90*365,0) AS spend_per_year,
       round(sum(waste_usd)/90*365,0) AS waste_per_year,
       round(100*sum(waste_usd)/sum(spend_usd),1) AS waste_pct,
       max(monthly_budget_usd) AS monthly_budget_usd,
       round(100*(sum(spend_usd)/3)/max(monthly_budget_usd),1) AS pct_of_monthly_budget
FROM main.tiffin_agent_waste.agent_waste_gold
GROUP BY agent_id ORDER BY waste_per_year DESC
```

| agent_id | display_name | owner_team | tool_calls | spend_per_year | waste_per_year | waste_pct | monthly_budget_usd | pct_of_monthly_budget |
|---|---|---|---|---|---|---|---|---|
| order-support-agent | Order Support Agent | support-ai | 442399 | 242829.0 | 54484.0 | 22.4 | 22000.0 | 90.7 |
| delivery-routing-agent | Delivery Routing Agent | logistics-ai | 319610 | 192290.0 | 38218.0 | 19.9 | 14000.0 | 112.9 |
| fraud-detection-agent | Fraud Detection Agent | risk-ai | 232294 | 68622.0 | 10186.0 | 14.8 | 5000.0 | 112.8 |
| customer-retention-agent | Customer Retention Agent | growth-ai | 84031 | 18541.0 | 5040.0 | 27.2 | 1800.0 | 84.7 |
| menu-recommendation-agent | Menu Recommendation Agent | discovery-ai | 68225 | 2061.0 | 803.0 | 39.0 | 400.0 | 42.3 |
| merchant-onboarding-agent | Merchant Onboarding Agent | merchant-ai | 153882 | 4079.0 | 687.0 | 16.8 | 600.0 | 55.9 |

_6 row(s)_

### The prioritisation insight: cost per failed call varies ~100x

Ranking by error **count** would put `update_merchant` above `list_deliveries`. Ranking by **money** inverts that. This is the query Genie writes for itself when asked which tool server wastes the most per failed call, which is why the ranking does not need to be precomputed into a separate serving table.

```sql
SELECT tool_server, tool_name,
       sum(recovering_traces) AS failed_calls,
       round(sum(waste_usd)/90*365,0) AS waste_per_year,
       round(sum(waste_usd)/nullif(sum(recovering_traces),0),4) AS waste_per_failed_call
FROM main.tiffin_agent_waste.tool_waste_gold
GROUP BY tool_server, tool_name
HAVING sum(recovering_traces) > 0
ORDER BY waste_per_failed_call DESC
```

| tool_server | tool_name | failed_calls | waste_per_year | waste_per_failed_call |
|---|---|---|---|---|
| payment-gateway | create_refund | 3010 | 28693.0 | 2.3505 |
| delivery-tracking-api | list_deliveries | 3844 | 33992.0 | 2.1804 |
| analytics-aggregator | batch_query | 1942 | 10186.0 | 1.2933 |
| geolocation-service | distance_matrix | 955 | 4226.0 | 1.0912 |
| order-history-api | list_orders | 5959 | 25790.0 | 1.0672 |
| promo-engine | create_promo | 4454 | 5040.0 | 0.279 |
| menu-service | upsert_menu_item | 1155 | 578.0 | 0.1234 |
| merchant-catalog-api | get_merchant | 1655 | 803.0 | 0.1197 |
| merchant-catalog-api | update_merchant | 1167 | 109.0 | 0.023 |

_9 row(s)_
