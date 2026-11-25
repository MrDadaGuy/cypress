-- Wider audiences: public, members, staff, security, board (see cypress/roles.py).
alter table document_sets drop constraint if exists document_sets_visibility_check;
alter table document_sets add constraint document_sets_visibility_check
  check (visibility in ('public', 'members', 'staff', 'security', 'board'));
