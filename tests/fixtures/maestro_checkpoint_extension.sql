-- Private E05-09 backend extension contract; NOT a Maestro migration/startup writer.
-- Apply explicitly in the SAME agent-owned journal DB, after E05-09 provisioning.
-- E11-05 implements SDK checkpoint write/re-entry; E11-02 tests provision this fixture.
CREATE TABLE maestro_journal.checkpoints (
  checkpoint_ref varchar(50) PRIMARY KEY,
  generation uuid NOT NULL,
  maestro_fence bigint NOT NULL CHECK(maestro_fence>0),
  descriptor jsonb NOT NULL CHECK(jsonb_typeof(descriptor)='object'),
  state jsonb NOT NULL CHECK(jsonb_typeof(state)='object'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK(checkpoint_ref=descriptor->>'checkpoint_ref'),
  CHECK(checkpoint_ref ~ '^checkpoint:v1:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')
);
CREATE FUNCTION maestro_journal.checkpoint_immutable_guard() RETURNS trigger LANGUAGE plpgsql
SET search_path=maestro_journal,pg_catalog,pg_temp AS $$
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'checkpoint immutable' USING ERRCODE='23514'; END IF;
  IF NOT EXISTS(SELECT 1 FROM maestro_journal.authority a JOIN maestro_journal.phases p
    ON p.delivery_id=(NEW.descriptor->>'delivery_id')::uuid AND p.phase_id=NEW.descriptor->>'source_phase_id'
    WHERE a.id AND a.integrity='ready' AND a.generation=NEW.generation
      AND a.organization_id=(NEW.descriptor->>'organization_id')::uuid
      AND a.workspace_id IS NOT DISTINCT FROM (NEW.descriptor->>'workspace_id')::uuid
      AND a.instance_agent_id=(NEW.descriptor->>'instance_agent_id')::uuid
      AND p.execution_id=(NEW.descriptor->>'source_execution_id')::uuid
      AND p.maestro_fence=NEW.maestro_fence AND p.state='running' AND p.lease_until>clock_timestamp()
      AND p.namespace->>'thread_id'=NEW.descriptor->>'thread_id'
      AND p.namespace->>'recipient_participant_id'=NEW.descriptor->>'agent_participant_id'
      AND (NEW.descriptor->>'retain_until')::timestamptz>clock_timestamp())
  THEN RAISE EXCEPTION 'checkpoint binding invalid' USING ERRCODE='23514'; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER checkpoint_immutable BEFORE INSERT OR UPDATE OR DELETE ON maestro_journal.checkpoints
  FOR EACH ROW EXECUTE FUNCTION maestro_journal.checkpoint_immutable_guard();
-- No automatic GC. Reader role: only CONNECT + schema USAGE + SELECT on these
-- three tables, never DDL/TRUNCATE/DML/owner/superuser. Owner migrations/restore
-- are offline with admission paused and integrityVerified disabled.
