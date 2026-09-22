-- 550_url_parser_prompt_speaks_the_canon.sql
-- FIELD CAPTURE W5 -- the on-demand URL parser's prompt speaks the canon it is
-- now constrained by.
--
-- THE DEFECT. W5 gives `price_unit` a real JSON enum for the first time
-- (`scraper/source_parsers/common.py`, generated from `vocabulary.CANON`, so the
-- provider constrains the string instead of the description asking nicely). The
-- prose the model actually reads is NOT in the repo -- it is
-- `app_settings.llm_parse_system_prompt`, seeded by migration 020 and edited
-- through the Settings UI -- and it still instructs the two spellings the
-- collapse retired: "měsíc" / "celkem". Prompt and schema now contradict each
-- other, and the model follows the prompt: a schema-enforcing provider drops
-- the field (or fails the call) on a column that was 100 % filled on every
-- portal. CI cannot see this, because the prompt is DB-resident (W7's §7).
--
-- The disposition list is corrected in the same pass for the same reason: every
-- value it names is legal, but it stops at 6+1 while the canon now runs to 9+1,
-- so the model cannot state a layout four portals publish.
--
-- ADDITIVE: a targeted `replace()` on the two blocks, NOT a whole-prompt
-- rewrite -- the live value already carries operator edits (it is 3,822 chars
-- against migration 020's seed) and a wholesale set would discard them. The
-- `app_settings` trigger from migration 020 archives the previous value into
-- `app_settings_history`, so this is reversible.

set lock_timeout = '5s';

update app_settings
   set value = to_jsonb(
         replace(
           replace(
             value #>> '{}',
             '- price_unit (string): "měsíc" if the price is a monthly rent figure;' || E'\n' ||
             '  "celkem" if it is a total/sale price.',
             '- price_unit (string): "za mesic" if the price is a monthly rent figure;' || E'\n' ||
             '  "za nemovitost" if it is a total/sale price. Those two slugs exactly --' || E'\n' ||
             '  they are the stored canon, not display Czech.'
           ),
           '  "5+kk", "5+1", "6+kk", "6+1", or null. Lowercase exactly as shown.',
           '  "5+kk", "5+1", "6+kk", "6+1", "7+kk", "7+1", "8+kk", "8+1",' || E'\n' ||
           '  "9+kk", "9+1", or null. Lowercase exactly as shown.'
         )
       ),
       updated_by = 'migration_550'
 where key = 'llm_parse_system_prompt';

-- A silent no-op is the failure mode that matters here: if the operator has
-- already reworded either block, the replace matches nothing and the prompt
-- keeps contradicting the enum with no signal at all.
do $$
declare
  prompt text;
begin
  select value #>> '{}' into prompt from app_settings where key = 'llm_parse_system_prompt';
  if prompt is null then
    raise exception 'llm_parse_system_prompt is missing';
  end if;
  if position('"za nemovitost"' in prompt) = 0 or position('"za mesic"' in prompt) = 0 then
    raise exception 'price_unit block did not match -- reword it by hand to the canon (za mesic / za nemovitost)';
  end if;
  if position('"měsíc" if the price' in prompt) > 0 then
    raise exception 'the retired price_unit spelling is still in the prompt';
  end if;
  if position('"9+1"' in prompt) = 0 then
    raise exception 'disposition block did not match -- extend it by hand to 9+1';
  end if;
end $$;
