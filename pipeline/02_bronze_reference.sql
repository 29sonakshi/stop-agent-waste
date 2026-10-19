-- BRONZE: reference dimensions.
--
-- Deliberately a DIFFERENT ingestion path from the spans. These are exports from systems
-- that really do hand you files -- an MCP `tools/list` introspection dump, Unity Catalog
-- metadata, a Jira export, the negotiated rate card, CI/CD history -- so read_files is the
-- honest pattern here, and the pipeline demonstrates both ingestion styles.
--
-- Materialized views (batch read, no STREAM) because these are small and republished in
-- full on every regeneration. An Auto Loader stream would skip overwritten files.
--
-- Grouped in one file on purpose: six 4-line datasets are more readable together than
-- split across six near-identical files.

CREATE OR REFRESH MATERIALIZED VIEW bronze_tool_catalog
COMMENT 'Declared MCP tool schemas (tools/list dump). expected_args_json is the contract.'
AS SELECT * FROM read_files('/Volumes/main/tiffin_agent_waste/reference/tool_catalog',
                            format => 'parquet');

CREATE OR REFRESH MATERIALIZED VIEW bronze_agent_catalog
COMMENT 'Agent registry: owner team, model endpoint, budget.'
AS SELECT * FROM read_files('/Volumes/main/tiffin_agent_waste/reference/agent_catalog',
                            format => 'parquet');

CREATE OR REFRESH MATERIALIZED VIEW bronze_bug_registry
COMMENT 'Triaged bug families (Jira export). Descriptive only -- costs are computed here.'
AS SELECT * FROM read_files('/Volumes/main/tiffin_agent_waste/reference/bug_registry',
                            format => 'parquet');

CREATE OR REFRESH MATERIALIZED VIEW bronze_model_pricing
COMMENT 'Per-endpoint token pricing. Makes every dollar figure auditable.'
AS SELECT * FROM read_files('/Volumes/main/tiffin_agent_waste/reference/model_pricing',
                            format => 'parquet');

CREATE OR REFRESH MATERIALIZED VIEW bronze_agent_tool_affinity
COMMENT 'Which tools each agent calls, and how often.'
AS SELECT * FROM read_files('/Volumes/main/tiffin_agent_waste/reference/agent_tool_affinity',
                            format => 'parquet');

CREATE OR REFRESH MATERIALIZED VIEW bronze_deploy_events
COMMENT 'Tool-server deploy history. Lets a waste spike be tied to the deploy that caused it.'
AS SELECT * FROM read_files('/Volumes/main/tiffin_agent_waste/reference/deploy_events',
                            format => 'parquet');
