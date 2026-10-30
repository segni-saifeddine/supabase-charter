# Design

Why this agent is built the way it is. If you only want to use it, read the
[guide](guide.md); if you want the tool and configuration reference, read the
[README](../README.md).

---

## 1. The problem

Creating a database is a chain of small steps, and the boring ones get skipped.
Create the project, pick a compute size, wait for it to come up, collect the
keys, put them somewhere safe, remember it exists, delete it when it is done.
Done by hand in a dashboard and a few ad-hoc scripts, the result is predictable:

- **Slow and inconsistent.** Everyone provisions, names, sizes and seeds
  differently; the same demo gets rebuilt several times.
- **Secrets end up in the wrong place.** Connection strings and service-role
  keys get pasted into chats, `.env` files and tickets.
- **Databases are forgotten.** There is no inventory of who created what, why,
  and until when, so unused projects keep running and keep costing.
- **Production is one click away.** Any tool that can delete a Supabase project
  can delete the production ones. There is no technical boundary between "mine,
  for a demo" and "ours, serving customers".

Handing the chain to an AI agent makes the first two problems better and the
last two much worse — unless the boundary is built in code rather than asked
for in a prompt. That is the design problem this repository solves.

## 2. Goals and non-goals

**Goals**

1. Create a Supabase database of **any kind**, from a throwaway test to
   production, by describing it in plain English: kind, purpose, environment,
   Vault secret path, compute size, duration.
2. Optionally copy data into it from any existing PostgreSQL database: chosen
   tables, chosen period, read-only against the source.
3. Credentials are written to **Vault and nowhere else**. No secret value is
   ever shown, logged, returned to the model, or committed.
4. Keep an inventory of what was created — who, what, why, when, until when —
   shared through git and reviewed like code.
5. The agent can only modify or delete databases **it** created. Everything
   else is technically out of reach.
6. Every step works in any order, in any chat, at any time: provision today,
   copy tomorrow, delete in three weeks from another machine.
7. Nothing about the deployment is hardcoded. Kinds, environments, Vault
   mounts, secret paths, regions and durations are configuration.

**Non-goals**

- Automatic deletion on expiry. Reminders only; a person always confirms.
- Masking or anonymising copied data.
- Delivering secrets from Vault to applications. The agent writes Vault; what
  reads it is your choice and deliberately not the agent's concern.
- Copying Supabase Storage buckets, Edge Functions or Auth users.
- Managing projects the agent did not create.

## 3. The three layers, and which one you can trust

| Layer | Artifact | Role |
|---|---|---|
| Conversation | `.cursor/skills/charter/SKILL.md` | What to ask, in what order, what to read back before a destructive step. |
| Behaviour | `.cursor/rules/charter-deletion.mdc` | Hard rules: confirm before destroying, never act outside the ledger, never print a secret. |
| Enforcement | the MCP server | Every rule that actually matters, checked in code. |

The split is the point. The skill and the rule shape a good conversation, but
they are instructions to a language model and therefore advisory. Everything
that must hold regardless of how the conversation goes — ownership, cost
approval, read-only sources, secret handling — is a check in the server that
returns a refusal. A refusal is final by design: the skill tells the model never
to retry with different arguments, and the server would refuse again anyway.

This is why the server, not the model, owns the Supabase token: the model can
only ask for operations the server is willing to perform.

## 4. Ownership: four independent proofs

Before any write or destructive operation, the server requires **all** of:

1. **A ledger entry** for the target, in a status that allows the operation.
2. **A name match**: the live project name from the Supabase API starts with a
   configured kind prefix and equals the ledger entry's name.
3. **A marker row**: the database itself contains `public._charter_meta` with
   a `state_id` equal to the ledger entry's `id`.
4. **Not protected**: the project ref is not in `DB_AGENT_PROTECTED_REFS`.

Plus, for deletion, an explicit `confirm=true`.

Four proofs rather than one because each fails differently. The ledger could be
stale (someone deleted the project by hand). A name could collide (two people,
same purpose, same day). The marker proves identity but needs the database to be
reachable and its credentials to still be in Vault. The protected list is the
only one that holds even if everything else is wrong, which is why it exists.

Requiring all four means an attacker — or a confused model — needs the ledger,
the name, the live database and the configuration to agree before anything is
destroyed. The marker in particular cannot be forged by editing a file: it lives
inside the database being targeted.

The interesting cases are the degraded ones, and they are deliberate:

- A `provisioning` entry (a provision that died halfway) may have no marker, no
  Vault entry, not even a project ref. It is deletable on proofs 1, 2 and 4
  alone, because the alternative is a project that stays billed forever with no
  way to remove it through the agent.
- A retried deletion whose project Supabase no longer has skips proofs 2 and 3
  as well. The ledger already recorded that the agent asked for that removal, so
  a missing project is the expected outcome, not a reason to refuse.
- An `active` entry whose project vanished without the agent asking is **not**
  closed silently. That is the one case where the right answer is to stop and
  tell a human, because something happened outside the agent's model of the
  world.

## 5. State: a git-tracked, secret-free ledger

The inventory is one YAML file, `state/databases.yaml`, committed to git.

Why a file in git rather than a database or a local cache:

| Alternative | Why not |
|---|---|
| A table in a product database | Mixes tooling state into a product, and needs write access to that database from every developer machine. |
| A local file per user | Does not survive machines or time, which defeats "delete it three weeks later from anywhere". |
| A managed service | Another thing to run, authenticate and back up, for data that is a few kilobytes and changes a few times a week. |

Git gives, for free, the properties this inventory needs: durable, shared,
diffable, reviewable, and already authenticated for everyone who can clone the
repository. Changes to the inventory arrive as pull requests, so "we created a
production database" is a reviewable event rather than a side effect.

The file holds **no secret values** — only the Vault path where the credentials
live. That is what makes it safe to commit, and it is enforced by the record
model rather than by convention.

One file, not one per database, because it is easier to read and review. The
cost is a possible textual conflict when two people provision at once, resolved
in the second pull request like any other conflict.

### The worktree

In `auto` mode the server never touches your checked-out branch. It keeps its
own git worktree and behaves like a careful colleague: fetch, read the merged
state, open a `charter/...` branch, one commit per change, and a pull
request when you say you are done.

This exists because the alternative — committing to whatever branch the user
happens to be on — means a tool silently mixing its commits into unrelated work.
The worktree makes the agent's git activity completely separate from yours, at
the cost of one extra directory.

`publish_ledger` never force-pushes. If the branch was already pushed with a
different history (a publish interrupted after its push, whose pull request may
since have merged), the pending commits go to a fresh branch. Losing a ledger
commit is worse than having a stray branch.

## 6. Secrets: Vault as the only destination

Every credential the agent produces goes to Vault KV v2 and nowhere else, at
`<mount>/<secret_path>/<database-name>`. Access goes through the `vault` CLI,
reusing whatever login the operator already has, so the agent holds no Vault
credential of its own.

The path is composed of three parts, each a control point:

- the **mount** from the environment, so a dev database cannot be written into
  the production mount, and each mount can carry its own Vault policy;
- the **secret path**, given per database or defaulted, validated so it cannot
  escape the mount it was reported to go to (no absolute paths, no `..`);
- the **database name**, generated rather than supplied, always carrying a kind
  prefix — which is what lets a Vault policy distinguish agent-managed secrets
  from application secrets in the same folder.

Secret delivery is explicitly out of scope. An earlier version of this agent was
coupled to a specific Kubernetes secret-sync operator, which meant its Vault
paths had to match that operator's conventions and its documentation had to
explain cluster behaviour. Dropping that coupling cost nothing — the agent's job
ends at the Vault write — and made it work for anyone, whatever reads their
secrets.

### Why Vault is written before Supabase on rotation

A rotation has two writes: the new password into Vault, and the new password
into Supabase. One of them can fail.

Vault first. If the process dies in between, the database still accepts the
password Vault held before (the previous KV version), and the next rotation
finds it there and completes. Supabase first would leave a password that exists
nowhere but in a dead process: the database would accept a password nobody can
look up.

The cost is a few seconds in which readers may hold a password Supabase has not
accepted yet. That is recoverable; the other failure is not.

Both writes are check-and-set on the version read, so concurrent rotations
cannot clobber each other, and a Supabase refusal restores the exact previous
contents.

### Why deletion records its intent first

Deletion touches three systems: Supabase, Vault, the ledger. The entry is set
to `deleting` and committed **before** the Supabase call.

Without that, a crash between deleting the project and deleting the Vault entry
would leave an entry marked `active` pointing at a project that no longer
exists — and the ownership checks would then refuse to clean it up, because
proof 3 can no longer be satisfied. Recording the intent first turns that into a
resumable operation: the retry knows a missing project is expected.

## 7. Why a custom MCP server

The official Supabase MCP server can create a project, but it cannot choose the
compute size and it cannot delete one. The Management API can do both, so this
server wraps the API directly and adds what the API has no concept of.

| Alternative | Why not |
|---|---|
| Official Supabase MCP server only | No compute size on create, no delete, no ownership model, no Vault, no data copy. A custom server would still be needed for the rest. |
| Supabase branching | Branches derive from a production project's schema and are tied to git branches of an app. They do not give an independently sized, independently owned project, and they do not cover slicing data by period. |
| A shell script | No ownership model, no confirmation protocol, and nothing to stop it from deleting production. The guard rails are the product. |

## 8. Copying data

Copying a slice of a real database is the one operation where correctness is
subtle, so the design is conservative.

- **The source is read-only in code**, not by convention: every transaction is
  `READ ONLY`, and only mounts in `DB_AGENT_SOURCE_MOUNTS` are accepted.
- **Session mode is forced.** A Supabase transaction-pooler DSN (port 6543) is
  rewritten to the session pooler (5432), and any other transaction-mode pooler
  is refused. Through a transaction pooler, backend connections are shared
  between clients, so anything set at session level — `pg_dump`'s `search_path`,
  for instance — leaks to the next client.
- **A plan is always shown first.** Foreign-key order, row estimates,
  dependencies, and the tables that need a decision.
- **Tables without a period column need an explicit decision**, per table, and
  the copy refuses while one is undecided. The alternative — guessing — either
  copies a whole production table nobody asked for, or silently leaves a lookup
  table empty.
- **Foreign keys are not enforced during the copy**, because a child row whose
  parent falls outside the period would abort the whole table. Instead the agent
  counts the orphans afterwards and reports them per foreign key, with the two
  remedies. Reporting an imperfection beats refusing a copy that is usually
  exactly what was wanted.
- **`replace` truncates only the planned tables**, in one statement without
  `CASCADE`, and is refused if another table references them. `CASCADE` would
  silently empty tables nobody confirmed.

## 9. Risks

| Risk | Mitigation |
|---|---|
| The management token can delete any project in the organization | Server-side ownership guards (ledger, kind prefix, marker table, protected refs, explicit confirm), unit-tested as a matrix; the token is readable only by the policy that needs it, and is rotatable without restarting. |
| The agent can now create production databases | Production kinds are opt-in configuration, default to no expiry, and the skill requires the user to name the database in their confirmation. Production refs that must never be touched go in `DB_AGENT_PROTECTED_REFS`. |
| Ledger drift (two people at once, or a hand edit) | Reads always see the merged state; changes land as pull requests; the worktree is reset before each operation; the rule forbids hand edits. |
| Forgotten databases and cost | `expires_at` with reminders, `list_dbs` flags expired entries, and the cost is stated and confirmed before every provision. |
| Real data in a non-production database | Tables and period are chosen explicitly, the plan is shown first, sources are read-only. Masking is an explicit non-goal, stated in the README, and the skill asks the user to acknowledge the copy. |
| Expired Vault login or a missing CLI | Tools fail fast with the exact remedy, and nothing is half-applied because the token is read before any API call. |
| Management API changes (fields, sizes, status values) | A thin client with recorded-response tests; sizes and regions are passed through rather than hardcoded beyond defaults. |

## 10. Known limits

- One Supabase organization per server instance.
- Only the `public` schema is copied, with the types, functions and extensions
  its tables need. Views and unrelated functions are not.
- Vault KV v2 only, through the `vault` CLI.
- One user, one local server. There is no shared, multi-tenant service, and the
  ledger's concurrency model (pull requests) assumes human-scale change rates.
