# Charter — User Guide

> Charter a database in a chat: you get a Supabase project with its credentials
> in Vault, recorded in the ledger. Hand it back the same way when you are done.

**Audience:** anyone on Linux, macOS, or Windows with WSL.
**Time to set up:** about 15 minutes, once.

For the tool reference and the full configuration table see the
[README](../README.md); for why it is built this way see
[design.md](design.md).

---

## 1. What it does

The agent is a small local MCP server plus a skill that teaches an AI agent how
to drive it. You talk to it in plain English:

| You ask | It does |
|---|---|
| "Create a production database for the billing service, secret path billing" | Shows the cost, waits for your yes, creates the project, writes the credentials to Vault, records it in the ledger |
| "Create a demo database for the Acme pilot, secret path acme, 5 days" | The same, with an expiry date |
| "Copy orders and customers from prod/analytics/primary-db for the last 3 months" | Confirms the source with you, then copies the schema and the rows for that period (the source is only read) |
| "Extend the Acme demo by 3 days" / "Rotate its password" | Updates the expiry, or the password and Vault |
| "Delete the Acme demo database" | Reads the details back, waits for your yes, deletes the project and the Vault entry |

Every database it creates is listed in a git-tracked file, the **ledger**
(`state/databases.yaml`). The agent can only touch databases in the ledger.
Anything created by hand, or by anyone else, is out of its reach.

### What you get after provisioning

- The Supabase project URL and ref.
- A Vault path `<mount>/<secret_path>/<database-name>` with the keys
  `DATABASE_URL`, `POSTGRES_PASSWORD`, `PROJECT_REF`, `SUPABASE_URL`,
  `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`.
- An entry in the ledger recording what it is, who asked for it, and when it
  expires.

That is the whole hand-off. How your applications read the secret out of Vault
is up to you: the Vault API at boot, `vault agent` templating, a secrets
operator, or your CI. The agent has no opinion and needs none.

`DATABASE_URL` points at the Supabase session pooler (IPv4), so it works from
laptops and WSL where the direct host (IPv6 only) does not.

### Rules the agent follows

- Shows the monthly cost and the cost for the requested lifetime, and waits for
  a yes before creating anything.
- Never prints a password or key in chat, and never writes a secret to git.
- Reads sources in read-only transactions, through a session-mode connection
  (a transaction-pooler address on port 6543 is switched to the session pooler
  on 5432), so it cannot write to them or leave settings behind for the next
  client of a shared connection.
- Asks you to confirm the exact name before deleting, and refuses anything that
  is not in the ledger.
- Never overwrites another database's credentials: a provision is refused when a
  live project with the same name, or a secret at the same Vault path, already
  exists.
- When the server refuses, the agent stops. It does not retry with a different
  environment, size, or secret path.

---

## 2. One-time setup

### 2.1 What you need access to

| Thing | Why |
|---|---|
| A Vault login, any auth method | The agent uses your session; it never stores a Vault token |
| Read on the Supabase token path (default `secret/supabase`) | The agent's Supabase identity, read at each call; it never lands on your machine |
| Create, read, update, list on the mounts in `DB_AGENT_VAULT_MOUNTS` | Writing your databases' credentials, and reading the sources you copy from |
| Delete on `<mount>/<folder>/<kind>-*` | Removing the credentials when you delete a database. Without it the agent leaves a tombstone instead |
| A Supabase organization access token, in Vault | Creating and deleting projects. See [README section 5.1](../README.md#51-authentication-one-global-token-path) for the exact permissions |

You do not need a Supabase account of your own.

### 2.2 Prerequisites

Python 3.12+, the `vault` CLI, PostgreSQL 17 client tools (`pg_dump`, `psql`,
only needed to copy data), and the GitHub CLI (`gh`) if you use the ledger's
`auto` mode.

**Ubuntu / Debian / WSL**

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv curl gpg

# PostgreSQL 17 client tools. Supabase runs 17, and an older pg_dump cannot
# dump a 17 server.
sudo apt install -y postgresql-common
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
sudo apt install -y postgresql-client-17

# Vault CLI
curl -fsSL https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt update && sudo apt install -y vault

sudo apt install -y gh
```

**macOS (Homebrew)**

```bash
brew install python@3.12 libpq gh
brew link --force libpq            # puts pg_dump / psql on PATH
brew tap hashicorp/tap && brew install hashicorp/tap/vault
```

**Check**

```bash
python3 --version      # 3.12 or newer
pg_dump --version      # 17.x
vault version
```

> **WSL note.** Run everything inside WSL, not PowerShell, and open the
> repository through the WSL remote so the MCP config and the server are on the
> same side. A clone under your Linux home (`~/`) is much faster than one under
> `/mnt/c/`.

### 2.3 Put the Supabase token in Vault

Once per deployment, by whoever administers it:

```bash
export VAULT_ADDR=https://vault.example.com
vault kv put secret/supabase ORG_SLUG=<your-org> ACCESS_TOKEN=-
# paste the token, press Enter, then Ctrl+D
```

Reading from stdin keeps the token out of your shell history. The path is
configurable with `DB_AGENT_SUPABASE_TOKEN_VAULT_PATH`.

### 2.4 Log in to Vault

```bash
export VAULT_ADDR=https://vault.example.com   # add to ~/.bashrc or ~/.zshrc
vault login -method=<your method>             # oidc, github, userpass, ...
vault token lookup                            # must succeed

gh auth login                                 # only for ledger auto mode
```

Vault tokens expire. When the agent reports "invalid token" or "permission
denied", log in again and restart the server.

### 2.5 Install the server

```bash
git clone <this-repo> && cd supabase-charter
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
echo "DB_AGENT_MCP_TOKEN=$(openssl rand -hex 24)" >> .env
```

`.env` is gitignored and holds non-secret settings only. The Supabase token is
never on your machine; the server reads it from Vault at each call. The file is
read from the project directory whatever directory you start the server from.

Open `.env` and set at least:

```bash
VAULT_ADDR=https://vault.example.com
DB_AGENT_VAULT_MOUNTS=dev=dev,staging=staging,prod=prod   # your KV mounts
DB_AGENT_KINDS=dev,staging,demo,prod,poc,test             # what you allow
DB_AGENT_DEFAULT_ENVIRONMENT=dev
DB_AGENT_PROTECTED_REFS=                                  # your production refs
DB_AGENT_STATE_GIT_SYNC=manual                            # or auto, see section 4
```

Every setting is explained inline in [`.env.example`](../.env.example).

### 2.6 Register the server with your MCP client

For Cursor, `.cursor/mcp.json` at the root of the folder you open:

```json
{
  "mcpServers": {
    "charter": {
      "url": "http://127.0.0.1:8766/mcp",
      "headers": { "Authorization": "Bearer <paste DB_AGENT_MCP_TOKEN from .env>" }
    }
  }
}
```

In Cursor, **Settings → MCP** should show `charter` with a green dot and
about fifteen tools once the server is running. The skill
(`.cursor/skills/charter/`) and the deletion rule
(`.cursor/rules/charter-deletion.mdc`) ship with this repository and are
picked up when it is in your workspace.

### 2.7 Verify

```bash
.venv/bin/supabase-charter
# INFO ... listening on http://127.0.0.1:8766/mcp
```

Keep that terminal open. In a new chat, type `agent config`. You should get the
kinds, environments and defaults from your `.env`.

---

## 3. Daily use

### 3.1 Start the server

```bash
cd supabase-charter && .venv/bin/supabase-charter
```

Leave it running; `Ctrl+C` when you are done.

### 3.2 Create a database

Say what you need. The useful details are the **kind**, what it is **for**, the
**secret path** (the folder under the Vault mount), and **how long** you need
it. Permanent kinds such as `prod` get no expiry by default; the others get
seven days unless you say otherwise.

The project is named `<kind>-<purpose>-<date>`, for example
`prod-billing-service-20260412`; the agent shows the name before you confirm.

```
Create a production database for the billing service, secret path billing.
```

```
Create a dev database for the pricing service, secret path pricing, for 10 days.
```

The agent answers with the cost (for example `$10/month, about $3.33 for 10
days`) and waits. Say **yes**. It then reports the project ref, the Vault path
and the expiry date.

Optional: `size` (`micro` by default; larger sizes cost more and are shown
before you confirm), `environment` (which Vault mount the credentials go to),
`region`, or "no expiry".

### 3.3 Copy data from an existing database (optional)

Name the source by its Vault path, or by a shortcut from `DB_AGENT_SOURCES`. If
you do not remember the secret name, ask the agent to list the folder.

```
Copy customers, orders and order_items from prod/analytics/primary-db into the
pricing dev database, rows created in the last 3 months.
```

The agent:

1. Resolves the path and reads back the host, database and user (never the
   password), then asks **"Is this the right database?"**
2. Proposes the timestamp column that defines the period when several exist.
   Give dates as you say them: "until 2026-03-31" includes the whole of
   March 31.
3. Shows the plan: tables in foreign-key order, estimated rows, tables with no
   timestamp column (you decide per table: copy all rows, or skip — the agent
   recommends "copy all" when other selected tables depend on it), tables that
   reference a table you did not include, and the types, functions and
   extensions the tables need.
4. On your yes, copies schema and rows. Missing enum and domain types, trigger
   and default functions, and extensions are created first; tables that already
   exist in the target are kept, so a rerun after a failure is safe. Generated
   columns are computed by your database, not copied.
5. Reports rows per table and **orphans**: rows whose parent is missing because
   the parent was created outside the period — an order from July whose customer
   signed up in March. Foreign keys are not checked during the copy, so those
   rows do exist in your database. If the count is not zero, ask to copy the
   parent table in full or to widen the period, then rerun. It also reports
   **column drift** when a table's columns differ between source and target:
   values of source-only columns are not copied, and target-only columns keep
   their defaults.

You can copy again later to add tables or extend the period. `replace` mode
empties the selected tables first, only those, and asks for its own
confirmation; if another table in your database references them, the agent
refuses and asks you to include it or to append instead.

### 3.4 Use the database

```bash
vault kv get -mount=<mount> <secret_path>/<database-name>
```

Take `DATABASE_URL` from there and use it with `psql` or your application.
In a deployed environment, read the same path with whatever mechanism you
already use for Vault secrets.

### 3.5 Update

In any chat, any time:

```
Extend the pricing dev database by 5 days.
Rotate the password of the pricing dev database.
Add the prices table for the same period.
```

A rotation writes the new password to Vault first, then to Supabase. If
Supabase refuses, the agent puts Vault back and tells you; the old password is
still valid and nothing has changed. Anything reading the secret from Vault
picks the new password up on its next read, so restart whatever caches the
connection string.

### 3.6 Delete

```
Delete the pricing dev database.
```

The agent reads back the name, ref, kind, purpose and creation date, and waits
for an explicit **yes**. Then it deletes the Supabase project, removes the Vault
entry, and marks the ledger entry deleted.

Databases are **not** deleted automatically when they expire; the expiry date is
a reminder. Delete what you no longer use: each project costs about $10 a month.

A deletion that stops halfway (Vault unreachable, server restarted) leaves the
ledger entry as `deleting`; the agent refuses every other operation on it and
`list_dbs` flags it with `action_needed`. Ask to delete it again: the agent sees
the project is already gone and finishes the Vault and ledger steps.

A provision that failed halfway (status `provisioning`) can be deleted the same
way; nothing else can be done with it, so provision again instead. If the agent
says the marker table could not be verified (database unreachable, or the Vault
entry is gone), it asks you once more and then deletes with
`force_unreachable`.

---

## 4. The ledger and pull requests

The ledger lists the databases the agent manages. In `manual` mode (the
default) the server writes `state/databases.yaml` in your checkout and tells you
it changed; you commit it with your branch.

In **auto** mode the server keeps the ledger in its own git worktree, never in
your working branch:

- Each change (create, copy, extend, rotate, delete) is one commit on a
  `charter/...` branch.
- When you say **"that's all for now"**, the agent pushes the branch, opens a
  pull request against the base branch, and gives you the link. The entry is
  visible to others once it merges.
- If you start a new chat while a change is unpublished, the agent tells you and
  offers to publish it.
- If a ledger commit fails, nothing is left half-done: the next request starts
  clean and the real error is reported.

In both modes you never edit the ledger file or run git on it yourself, and your
own branch and uncommitted work are never touched.

---

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `charter` is red in your MCP client, or `ECONNREFUSED 127.0.0.1:8766` | Server not running | Start it (section 3.1), then refresh the MCP entry |
| `503 DB_AGENT_MCP_TOKEN is not set` | No bearer token configured | Add `DB_AGENT_MCP_TOKEN` to `.env` and restart |
| `401` from the server | The token in your MCP config differs from `DB_AGENT_MCP_TOKEN` | Make them identical, refresh the MCP entry |
| `Vault authentication is missing or expired` | Vault token expired, or policy missing | Log in again, restart the server; check `vault token lookup` |
| `Missing required permission(s): ...` from Supabase | The organization token lacks a permission | Add it to the token; the list is in [README 5.1](../README.md#51-authentication-one-global-token-path) |
| `environment '...' is not configured` | The environment is not in `DB_AGENT_VAULT_MOUNTS` | Add it, or use one that is listed. Ask the agent for `agent config` |
| `secret_path is required` | No folder given and no `DB_AGENT_SECRET_PATH` default | Say the folder, or set the default |
| `pg_dump failed: server version mismatch` | Client tools older than the source server | Install PostgreSQL 17 client tools (section 2.2) or set `DB_AGENT_PG_DUMP`; `psql` is taken from the same directory unless `DB_AGENT_PSQL` overrides it |
| `A Supabase project named ... already exists` or `Vault path ... already holds a secret` | Someone provisioned the same kind and purpose today, perhaps on an unmerged ledger change | Use a different purpose wording |
| `... did not finish provisioning. Delete it and provision again.` | An earlier provision died halfway | Ask the agent to delete it, then provision again |
| `The agent marker table could not be verified: ...` | Database unreachable, or its Vault entry is gone | Confirm the name again; the agent deletes with `force_unreachable`. Provision again afterwards |
| `The deletion of ... was started but did not finish.` | An earlier deletion stopped after the ledger was marked `deleting` | Ask the agent to delete it again; it continues from where it stopped |
| `Password rotation of ... failed at Supabase: ... Vault was put back` | Supabase refused the change, usually a token missing `database_config_write` | Nothing to repair; the old password still works. Fix the token permission |
| `Supabase has no project ... anymore, but the ledger lists ... as active.` | The project was removed outside the agent | Have an operator close the ledger entry and remove the Vault path by hand. The agent never closes an entry it did not ask to delete |
| Deletion reports `vault: tombstoned` | Your Vault policy has no delete on that path | The project is gone and the secret holds no credentials; an operator removes the path with `vault kv metadata delete` |
| `replace would also have to empty [...]` | Another table in the target references the tables being replaced | Include that table in the list, or use append |
| `The target lacks objects the schema needs that this agent cannot recreate: ...` | A composite or range type the tables use | Create it in the target by hand, then rerun |
| `Sources may only come from Vault mounts [...]` | The source path is on a mount outside `DB_AGENT_SOURCE_MOUNTS` | Add the mount, or use a listed one |
| `publish_ledger` returns no PR link | `gh` not logged in | `gh auth login`, then publish again, or open the PR from the branch name it gives you |
| The agent refuses to delete a database | It is not in the ledger | Delete it in the Supabase dashboard; the agent only deletes what it created |

Server log lines appear in the terminal where it runs; include them when asking
for help.

---

## 6. Limits

- Creates projects in one Supabase organization.
- Copies tables from the `public` schema only, with the enum and domain types,
  trigger and default functions, and extensions they need. Views and unrelated
  functions are not copied.
- Does not deliver secrets anywhere; it writes Vault.
- Does not delete expired databases on its own.
- One user, one local server; there is no shared service.
- Vault KV v2 only, through the `vault` CLI.
