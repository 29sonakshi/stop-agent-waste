-- BRONZE: raw agent telemetry.
--
-- Reads the Gateway's own Delta table as a STREAM. This mirrors production: AI Gateway
-- emits OTel spans and Zerobus persists them to a Unity Catalog Delta table within
-- seconds -- there is no file-landing step for traces. The table is NOT owned by this
-- pipeline, so it is referenced by its fully-qualified name.
CREATE OR REFRESH STREAMING TABLE bronze_spans
COMMENT 'Raw OTel spans streamed from the AI Gateway trace table. One row per span.'
TBLPROPERTIES ('delta.enableVariantShredding' = 'false')
AS SELECT * FROM STREAM(main.tiffin_agent_waste.raw_otel_spans);
