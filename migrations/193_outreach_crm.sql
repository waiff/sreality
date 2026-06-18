-- Phase 4: broker outreach CRM (human-in-the-loop).
--
-- Operator creates a campaign (a goal + target criteria), the system drafts a
-- personalised message per targeted broker with the LLM, the operator reviews /
-- edits / approves and sends MANUALLY (mailto / copy — no automated email send in
-- v1; the schema is ready for an 'email' channel when a provider is wired). GDPR:
-- legitimate-interest B2B basis; suppressed brokers are never targeted, and every
-- message records the contact used + timestamps for auditability.

CREATE TABLE outreach_campaigns (
  id          bigserial PRIMARY KEY,
  name        text NOT NULL,
  goal        text,                        -- operator's free-text objective
  guidance    text,                        -- extra steering fed to the LLM drafter
  status      text NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'active', 'archived')),
  target      jsonb NOT NULL DEFAULT '{}'::jsonb,  -- broker_leaderboard-shaped selection
  created_at  timestamptz NOT NULL DEFAULT now(),
  created_by  text,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE outreach_messages (
  id           bigserial PRIMARY KEY,
  campaign_id  bigint NOT NULL REFERENCES outreach_campaigns(id) ON DELETE CASCADE,
  broker_id    bigint NOT NULL REFERENCES brokers(id) ON DELETE CASCADE,
  channel      text NOT NULL DEFAULT 'email' CHECK (channel IN ('email')),
  to_email     text,
  to_phone     text,
  subject      text,
  body         text,
  status       text NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft', 'approved', 'sent', 'skipped', 'replied', 'bounced')),
  llm_call_id  bigint,                      -- audit link into llm_calls
  model        text,
  cost_usd     numeric,
  generated_at timestamptz NOT NULL DEFAULT now(),
  approved_at  timestamptz,
  sent_at      timestamptz,
  sent_via     text,                        -- 'manual' in v1; 'email' when a provider lands
  notes        text,
  UNIQUE (campaign_id, broker_id)           -- one message per broker per campaign
);

CREATE INDEX outreach_messages_campaign_status_idx
  ON outreach_messages (campaign_id, status);

-- GDPR opt-out / do-not-contact list at the canonical-broker grain.
CREATE TABLE broker_outreach_suppression (
  broker_id     bigint PRIMARY KEY REFERENCES brokers(id) ON DELETE CASCADE,
  reason        text,
  suppressed_at timestamptz NOT NULL DEFAULT now(),
  created_by    text
);

-- Operator-tunable drafting prompt + model (same app_settings pattern as the other
-- LLM features; editable via /settings, history-tracked by the migration-020 trigger).
INSERT INTO app_settings (key, value, description) VALUES
  ('outreach_draft_model', to_jsonb('claude-sonnet-4-5'::text),
   'Model used to draft broker outreach messages.'),
  ('outreach_draft_system_prompt', to_jsonb($prompt$You draft short, professional B2B outreach emails in Czech from a real-estate analytics operator to an individual real-estate broker. The goal is to open a relationship and ask whether the broker has off-market ("pod rukou") properties that match the operator's interest.

Rules:
- Write in Czech, polite vykání, concise (max ~140 words). No emoji, no hype.
- Personalise ONLY from the facts provided (broker name, firm, where and what they list). Never invent numbers, deals, or claims.
- One clear ask: whether they have any off-market / pre-market listings to share.
- Include a short, genuine opt-out sentence offering to stop contacting them (legitimate-interest B2B basis).
- Sign off generically; do not fabricate a sender name, phone, or company the operator did not provide.
- Call the record_outreach_draft tool exactly once with a subject (max 80 chars) and the email body. Do not output anything else.$prompt$::text),
   'System prompt for the broker outreach LLM drafter.')
ON CONFLICT (key) DO NOTHING;
