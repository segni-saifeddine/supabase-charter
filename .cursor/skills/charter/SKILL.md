---
name: charter
description: >-
  Create, seed, extend, rotate, or delete a Supabase database through the
  charter MCP server. Use when the user asks for a database of any kind
  (dev, staging, demo, poc, test, or production), wants tables copied into
  one from an existing database, wants a password rotated, or wants one
  deleted.
---

# Charter

The `charter` MCP server does the work. You translate the request into tool
calls. The server refuses anything that is not in its ledger, so do not invent
project refs and do not suggest dashboard steps that bypass it.

Operations are independent. One message may contain several; run them in order.
A later chat that only says "delete the Acme database" is complete on its own.

## Before anything else

Call `agent_config` once per conversation. It returns the kinds, environments
and defaults this deployment actually allows. Never assume a kind or
environment exists, and never offer one the config does not list.

## Before you change an existing database

Call `list_dbs`. Match the user's words to exactly one `name`. If several
match, ask which one. If none match, say it is out of scope and stop.

## When the server refuses

A refusal ends the operation. Report the server message verbatim and stop.
Never retry with a different environment, secret path, kind, size, region, or
name than the user asked for, and never call it a fallback. If they want
different inputs, they will say so in a new request.

## Provision

Collect, asking only for what is missing:

| Input | Default |
|---|---|
| kind | ask, offering the kinds from `agent_config` |
| purpose | ask (becomes the name slug) |
| environment | the `default_environment` from `agent_config` |
| secret_path | the `default_secret_path` if set, otherwise ask. It is the folder under the Vault mount and may be nested |
| size | `micro` |
| duration | the kind's default: no expiry for a `permanent_kinds` kind, otherwise `default_ttl_days`. Pass `no_expiry=true` only when they ask for a long-lived database of a non-permanent kind |

Call `get_cost(size, ttl_days)`. Tell them the monthly `amount` and the
`estimated_for_ttl_usd` for the requested duration, and wait for a yes. Then
`provision_db(..., confirm_cost_id=<from get_cost>)` with the same size; the
server refuses a non-zero cost without a matching id. A cost approval belongs
to one request: call `get_cost` again for every new provision, even for the
same size, and ask for a new yes.

Report `name`, `ref`, `supabase_url`, `vault_path` and `expires_at`. Say that
the credentials are in Vault at that path, under the keys `SUPABASE_URL`,
`SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `DATABASE_URL`,
`POSTGRES_PASSWORD` and `PROJECT_REF`. Never print a password or key; those
exist only in Vault. Do not invent a delivery mechanism (no Kubernetes
manifests, no secret-operator resources) unless the user asks: how they read
Vault is their business.

### Production databases

A `prod` kind is a normal provision, with two differences in how you talk
about it: confirm the purpose and the environment explicitly before calling
`provision_db`, since it defaults to no expiry and lands in the production
mount, and never offer to copy data into it without being asked.

An empty database is finished here. Copying data is a separate request.

## Copy data from an existing database

The source is any database whose credentials are already in Vault. Resolve it
from what the user says, then confirm before touching it:

1. If they name a catalog entry (`list_sources` shows them), use that name.
   Otherwise the source is a Vault path `<mount>/<folder>/<secret>`. Ask for
   the folder if you do not know it; `list_sources(folder=...)` lists the
   secret names there so they can pick.
2. Call `describe_source(source)`. Read back `vault_path`, `host`, `port`,
   `database`, `user`, which key holds the connection string, and
   `pooler_rewrite` when set (the source is reached through the session pooler
   instead of the transaction pooler). Ask: "Is this the right source
   database?" Do not proceed on `usable: false`; report `problem`.
3. After the yes, ask for the tables and the period. Use
   `inspect_source_tables` and propose a timestamp column when several exist.
   Pass dates as given ("2026-03-31"): a date-only end includes that whole
   day. Only a timestamp end ("2026-03-31T12:00:00Z") is exclusive; say so if
   you use one.
4. Call `plan_copy` and show the table order, row estimates, `flags`,
   `dependencies` (types, functions, extensions the tables need; the missing
   ones are created in the target) and `notes`.
   - A note means a selected table references a parent that is not selected:
     propose adding the parent to the list.
   - A flag means a table has no period column. Ask, per table, "copy all rows
     or skip?" and lead with the flag's `recommended` value (`copy_all` when
     other selected tables reference it, `skip` otherwise). Pass the answers in
     `copy_all` / `skip` to both `plan_copy` and `copy_tables`; the server
     refuses to copy while a flag is undecided.
   - `mode=replace` empties the plan's `truncates` tables first, those and
     nothing else. Read that list back, ask for that separately, and pass
     `confirm_replace=true` only for that yes; it does not decide anything
     else. If the copy refuses because another table references them, say
     which, and offer to add it to the list or to use `append`.

   Wait for a yes on the plan, then `copy_tables` with the same arguments.
5. Read the result back: rows per table, `skipped`, `truncated`,
   `column_drift` (columns present on one side only; values of the source-only
   ones were not copied) and `orphans`. Foreign keys are not enforced during
   the copy, so `orphans` lists child rows whose parent row is missing (parent
   created outside the period, or parent table skipped). If it is not empty,
   say so plainly and offer the fixes: put the parent table in `copy_all`, or
   widen the period, then rerun. A rerun is safe: tables already in the target
   are kept, not recreated.

Never print a value from the source secret; `describe_source` returns none.
`schema_only=true` copies structure and no rows. The source is only ever read.

If the source holds real customer data and the target is a demo or a database
other people will see, say so once before the copy. The user decides; you make
sure the decision is conscious.

## Extend, rotate, delete

- **Extend**: `extend_db(name, extra_days)`.
- **Rotate**: `rotate_db_password(name)`. Tell them Vault now holds the new
  password at `vault_path`, and that anything reading the secret picks it up on
  its next read. Do not ask the tool to reveal the new password. If the tool
  answers that Vault was put back, the old password is still valid; report the
  Supabase error and stop.
- **Delete**: from `get_db`, read back name, ref, kind, purpose and created_at.
  After an explicit yes that names that project, call
  `delete_db(name, confirm=true)`. If it is not in the ledger, stop.
  A `provisioning` entry (a provision that failed halfway) is deleted the same
  way. If the tool answers that the marker could not be verified, say why, ask
  again naming the project, then pass `force_unreachable=true`. If the tool
  answers "run the deletion again" (a `deleting` entry: the project is gone but
  Vault or the ledger was not updated), report that and call `delete_db` once
  more with the same confirmation; it continues from where it stopped.
  `list_dbs` flags such entries with `action_needed`.

For a database of a permanent kind (`prod`, and whatever else `agent_config`
lists), require the user to name it in their confirmation and state that it is
production. Do not accept a bare "yes" that followed a different question.

## Ledger

Call `ledger_status` once per conversation when you first touch the ledger; it
tells you the mode.

- `manual`: the server writes `state/databases.yaml` in the user's checkout and
  does not commit it. Tell them the file changed so they can review and commit
  it with their branch.
- `auto`: the server edits the ledger in its own worktree, never in the user's
  branch. The first change opens a `charter/...` branch from the base
  branch; every change (provision, extend, rotate, copy, delete) is one commit
  on it. Nothing is visible to others until it is published, so:
  - If `ledger_status` shows an open branch with pending commits at the start
    of a conversation, say so first ("there is an unpublished ledger change
    from earlier: <branch>, N commits") and offer to publish it.
  - After each change, remind them it is not published yet. When they have
    nothing more, ask: "Is this all for now? I will publish the ledger change
    and open a pull request."
  - Treat "that's all", "done", "nothing else", or any yes as the answer: call
    `publish_ledger(summary)` with a one-line summary (for example
    `add billing production database`), report the PR URL, and say the entry is
    visible once the PR is merged. If `pr_url` is empty, give them the branch
    name so they can open the PR by hand.
  - Never end a conversation with a pending ledger change without having asked.

Never edit the ledger file or run git on it yourself, in either mode.
