CREATE TABLE IF NOT EXISTS connector_operations (
 operation_id text PRIMARY KEY,
 business_id text NOT NULL,
 entity_id text NOT NULL,
 transaction_id text NOT NULL,
 action text NOT NULL CHECK(action='card_purchase'),
 target text NOT NULL,
 series text NOT NULL,
 reason text,
 UNIQUE(business_id,entity_id,transaction_id,action)
);
CREATE TABLE IF NOT EXISTS connector_plans (
 operation_id text PRIMARY KEY REFERENCES connector_operations(operation_id),
 fingerprint text NOT NULL CHECK(length(fingerprint)=64),
 encrypted text NOT NULL
);
CREATE TABLE IF NOT EXISTS connector_steps (
 operation_id text NOT NULL REFERENCES connector_operations(operation_id),
 kind text NOT NULL CHECK(kind IN ('create','upload','link','ack')),
 state text NOT NULL CHECK(state IN ('ready','intent','uncertain','confirmed','investigate')),
 attempt_id uuid,
 request_hash text,
 remote jsonb,
 reason text,
 PRIMARY KEY(operation_id,kind),
 CHECK((state='ready' AND attempt_id IS NULL AND request_hash IS NULL) OR (state<>'ready' AND attempt_id IS NOT NULL AND length(request_hash)=64)),
 CHECK(state<>'confirmed' OR remote IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS connector_attempts (
 attempt_id uuid PRIMARY KEY,
 operation_id text NOT NULL,
 kind text NOT NULL,
 request_hash text NOT NULL,
 encrypted text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(operation_id,kind) REFERENCES connector_steps(operation_id,kind)
);
CREATE TABLE IF NOT EXISTS connector_tokens (
 provider text PRIMARY KEY,
 encrypted text NOT NULL,
 expires_at timestamptz NOT NULL,
 identity_hash text NOT NULL,
 generation integer NOT NULL
);
CREATE OR REPLACE FUNCTION connector_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable accounting plan or attempt'; END $$;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='connector_plan_immutable' AND tgrelid='connector_plans'::regclass) THEN
  CREATE TRIGGER connector_plan_immutable BEFORE UPDATE OR DELETE ON connector_plans FOR EACH ROW EXECUTE FUNCTION connector_immutable();
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='connector_attempt_immutable' AND tgrelid='connector_attempts'::regclass) THEN
  CREATE TRIGGER connector_attempt_immutable BEFORE UPDATE OR DELETE ON connector_attempts FOR EACH ROW EXECUTE FUNCTION connector_immutable();
 END IF;
END $$;
