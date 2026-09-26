-- Cutout bus: group messages (`to` as an array of agent ids). Run after
-- agents.sql, and before deploying an index.ts that sends arrays.
-- Idempotent.
-- A group message stores its list in to_list (in the order sent) and
-- to_agent = ''; every other message keeps to_list null.
alter table cutout.messages add column if not exists to_list text[];
create index if not exists messages_to_list_idx
  on cutout.messages using gin (to_list);
alter table cutout.messages drop constraint if exists messages_to_list_check;
alter table cutout.messages add constraint messages_to_list_check check (
  to_list is null
  or (to_agent = '' and cardinality(to_list) between 1 and 16
      and array_position(to_list, null) is null));
