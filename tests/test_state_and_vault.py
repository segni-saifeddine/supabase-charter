from pathlib import Path

import httpx
import psycopg
import pytest

from supabase_charter.config import Settings
from supabase_charter.errors import AgentError
from supabase_charter.state import DatabaseRecord, Ledger, SeedRecord
from supabase_charter.supabase_api import SupabaseApi
from supabase_charter.vault import VaultClient, split_vault_path


def test_split_vault_path():
    assert split_vault_path("secret/supabase") == ("secret", "supabase")
    assert split_vault_path("staging/acme-demo/demo-acme-20261002") == (
        "staging",
        "acme-demo/demo-acme-20261002",
    )
    with pytest.raises(AgentError):
        split_vault_path("onlymount")


def test_ledger_round_trip_is_secret_free(tmp_path):
    path = tmp_path / "databases.yaml"
    ledger = Ledger(path, "manual")
    ledger.load()
    ledger.add(
        DatabaseRecord(
            id="7f3c9a2e-1111-2222-3333-444444444444",
            name="dev-pricing-20261002",
            kind="dev",
            ref="ref1",
            environment="staging",
            secret_path="pricing-dev",
            vault_path="staging/pricing-dev/dev-pricing-20261002",
            size="micro",
            region="eu-central-1",
            requested_by="dev@example.com",
            purpose="pricing",
            created_at="2026-10-02T09:00:00Z",
            expires_at=None,
            seeds=[
                SeedRecord(
                    at="2026-10-02T09:05:00Z",
                    source="staging/sandbox-saif/test-mock-source-database-for-copy-tests-20261007",
                    tables=["regions"],
                    period=None,
                    rows={"regions": 4},
                    mode="append",
                    schema_only=False,
                )
            ],
        )
    )
    ledger.save("chore(charter): add dev-pricing-20261002")
    text = path.read_text()
    assert "password" not in text.lower()
    # Long values must stay on one line; the repo's pre-commit hooks reject
    # the trailing space a wrapped scalar leaves behind.
    assert not any(line != line.rstrip() for line in text.splitlines())
    assert "source: staging/sandbox-saif/test-mock-source" in text
    again = Ledger(path, "manual")
    again.load()
    assert again.find("ref1").name == "dev-pricing-20261002"
    assert again.find("missing") is None


class FakeGit:
    """Records git/gh commands and simulates the worktree branch state."""

    def __init__(self, worktree):
        self.worktree = worktree
        self.calls = []
        self.branch = None
        self.commits = []
        self.rebase_fails = False
        self.committed_text = None  # ledger content as of the last commit
        self.commit_fails = False
        self.leftover = False  # a failed commit left the worktree dirty
        self.remote_branches = {}  # branch -> tip the remote has

    def _file(self):
        return self.worktree / "state/databases.yaml"

    def _dirty(self):
        if self.leftover:
            return True
        file = self._file()
        if not file.exists():
            return False
        return file.read_text() != self.committed_text

    def git(self, args, cwd):
        self.calls.append((Path(cwd).name, args))
        head = args[0]
        if head == "worktree" and args[1] == "add":
            (self.worktree / ".git").parent.mkdir(parents=True, exist_ok=True)
            (self.worktree / ".git").write_text("gitdir: fake")
        elif head == "symbolic-ref":
            if self.branch is None:
                raise AgentError("fatal: ref HEAD is not a symbolic ref")
            return self.branch + "\n"
        elif head == "status":
            return " M databases.yaml\n" if self._dirty() else ""
        elif head == "reset":
            self.leftover = False
            if self.committed_text is None:
                self._file().unlink(missing_ok=True)
            else:
                self._file().write_text(self.committed_text)
        elif head == "checkout":
            if args[1] == "-b":
                self.branch = args[2]
            elif args[1] == "--detach":
                self.branch = None
        elif head == "commit":
            if self.commit_fails:
                self.leftover = True
                raise AgentError("trailing-whitespace............Failed")
            self.commits.append(args[args.index("-m") + 1])
            self.committed_text = self._file().read_text()
        elif head == "log":
            return "\n".join(reversed(self.commits)) + "\n"
        elif head == "rebase" and args[1:] == ["--abort"]:
            raise AgentError("fatal: no rebase in progress")
        elif head == "rebase" and self.rebase_fails:
            raise AgentError("CONFLICT (content): databases.yaml")
        elif head == "branch" and args[1] == "-D":
            self.commits = []
        elif head == "branch" and args[1] == "-m":
            self.branch = args[2]
        elif head == "rev-parse":
            return f"local-{len(self.commits)}\n"
        elif head == "ls-remote":
            tip = self.remote_branches.get(args[-1], "")
            return f"{tip}\trefs/heads/{args[-1]}\n" if tip else ""
        elif head == "push":
            if self.remote_branches.get(args[-1], "") not in (
                "",
                f"local-{len(self.commits)}",
            ):
                raise AgentError("! [rejected] (non-fast-forward)")
            self.remote_branches[args[-1]] = f"local-{len(self.commits)}"
        return ""

    def gh(self, args, cwd):
        self.calls.append(("gh", args))
        return "https://github.com/org/repo/pull/42\n"


def _auto_ledger(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    path = repo / "state" / "databases.yaml"
    worktree = repo / ".ledger-worktree"
    fake = FakeGit(worktree)
    ledger = Ledger(
        path, "auto", git_runner=fake.git, worktree_path=worktree, gh_runner=fake.gh
    )
    return ledger, fake, worktree


def _record(name):
    return DatabaseRecord(
        id="7f3c9a2e-1111-2222-3333-444444444444",
        name=name,
        kind="demo",
        ref="ref1",
        environment="staging",
        secret_path="acme-demo",
        vault_path=f"staging/acme-demo/{name}",
        size="micro",
        region="eu-central-1",
        requested_by="dev@example.com",
        purpose="acme",
        created_at="2026-10-02T09:00:00Z",
    )


def test_auto_mode_uses_a_worktree_and_a_change_branch(tmp_path):
    ledger, fake, worktree = _auto_ledger(tmp_path)

    ledger.load()  # first call: worktree created from origin/main
    assert ("repo", ["worktree", "add", "--detach", str(worktree), "origin/main"]) in (
        fake.calls
    )
    assert ledger.path == worktree / "state/databases.yaml"
    assert ledger.status()["open_branch"] is None

    ledger.add(_record("demo-acme-20261002"))
    ledger.save("chore(charter): add demo-acme-20261002")
    assert fake.branch.startswith("charter/add-demo-acme-20261002-")
    assert (
        ".ledger-worktree",
        ["add", "--", "state/databases.yaml"],
    ) in (fake.calls)
    assert "password" not in ledger.path.read_text().lower()

    # Later call in the same change: rebase on base, no new branch, second commit.
    ledger.load()
    assert (".ledger-worktree", ["rebase", "origin/main"]) in fake.calls
    first_branch = fake.branch
    ledger.find("demo-acme-20261002").expires_at = "2026-10-20T09:00:00Z"
    ledger.save("chore(charter): update demo-acme-20261002")
    assert fake.branch == first_branch
    assert ledger.status()["pending_commits"] == [
        "chore(charter): update demo-acme-20261002",
        "chore(charter): add demo-acme-20261002",
    ]

    result = ledger.publish("demo database for Acme")
    assert (".ledger-worktree", ["push", "--set-upstream", "origin", first_branch]) in (
        fake.calls
    )
    gh_call = next(args for who, args in fake.calls if who == "gh")
    assert gh_call[:6] == ["pr", "create", "--base", "main", "--head", first_branch]
    assert "chore(charter): demo database for Acme" in gh_call
    assert result["pr_url"] == "https://github.com/org/repo/pull/42"
    assert fake.branch is None  # back on detached origin/main
    assert ledger.status()["open_branch"] is None


def test_publish_never_force_pushes_over_a_branch_that_was_published_before(
    tmp_path,
):
    ledger, fake, _ = _auto_ledger(tmp_path)
    ledger.load()
    ledger.add(_record("demo-acme-20261002"))
    ledger.save("chore(charter): add demo-acme-20261002")
    stale = fake.branch
    # The branch was pushed earlier (and merged), but the worktree never left
    # it: the server stopped before publish() finished. The remote still has
    # that old tip, the local branch now carries different commits.
    fake.remote_branches[stale] = "old-tip-already-merged"

    result = ledger.publish("acme again")
    assert result["branch"] != stale
    assert result["branch"].startswith("charter/add-demo-acme-20261002-")
    assert (".ledger-worktree", ["branch", "-m", result["branch"]]) in fake.calls
    assert not any(
        "--force" in " ".join(args) for who, args in fake.calls if args[0] == "push"
    )
    assert fake.remote_branches[stale] == "old-tip-already-merged"  # untouched
    assert result["pr_url"] and fake.branch is None

    # A branch whose remote tip equals the local one (push done, PR step
    # interrupted) keeps its name and is simply pushed again.
    ledger.load()
    ledger.add(_record("demo-beta-20261002"))
    ledger.save("chore(charter): add demo-beta-20261002")
    same = fake.branch
    fake.remote_branches[same] = f"local-{len(fake.commits)}"
    assert ledger.publish("beta")["branch"] == same


def test_auto_mode_refuses_when_open_change_does_not_rebase(tmp_path):
    ledger, fake, _ = _auto_ledger(tmp_path)
    ledger.load()
    ledger.add(_record("demo-acme-20261002"))
    ledger.save("chore(charter): add demo-acme-20261002")
    fake.rebase_fails = True
    with pytest.raises(AgentError, match="Publish or discard"):
        ledger.load()
    assert (".ledger-worktree", ["rebase", "--abort"]) in fake.calls


def test_auto_mode_survives_a_failed_commit_and_skips_empty_commits(tmp_path):
    ledger, fake, _ = _auto_ledger(tmp_path)
    ledger.load()
    ledger.add(_record("demo-acme-20261002"))

    # A hook rejects the commit: the hook's message is what the caller sees,
    # and the dirty ledger file is discarded on the next call, not left staged.
    fake.commit_fails = True
    with pytest.raises(AgentError, match="trailing-whitespace"):
        ledger.save("chore(charter): add demo-acme-20261002")
    fake.commit_fails = False
    ledger.load()
    assert (".ledger-worktree", ["reset", "--hard", "HEAD"]) in fake.calls
    assert "no rebase in progress" not in str(fake.calls)

    ledger.add(_record("demo-acme-20261002"))
    ledger.save("chore(charter): add demo-acme-20261002")
    assert fake.commits == ["chore(charter): add demo-acme-20261002"]
    assert ["commit", "--no-verify", "-m", fake.commits[0]] in [
        args for _, args in fake.calls
    ]

    # Saving an unchanged document (a retried delete that already recorded
    # delete_failed) must not try to commit, so git never says "nothing to
    # commit" in place of the real error.
    ledger.load()
    ledger.save("chore(charter): update demo-acme-20261002")
    assert fake.commits == ["chore(charter): add demo-acme-20261002"]


def test_manual_mode_never_runs_git_and_refuses_publish(tmp_path):
    calls = []
    ledger = Ledger(
        tmp_path / "databases.yaml", "manual", git_runner=lambda a, cwd: calls.append(a)
    )
    ledger.load()
    ledger.save("chore(charter): add x")
    assert calls == []
    with pytest.raises(AgentError, match="manual"):
        ledger.publish("x")


def test_vault_caches_token_and_lists_key_names_not_values():
    calls = {"n": 0}

    def runner(args, stdin=None, vault_addr=""):
        calls["n"] += 1
        return {"data": {"data": {"ACCESS_TOKEN": "secret-token", "ORG_SLUG": "acme"}}}

    settings = Settings(DB_AGENT_SUPABASE_TOKEN_VAULT_PATH="secret/supabase")
    client = VaultClient(settings, runner=runner)
    assert client.management_credentials() == ("secret-token", "acme")
    assert client.management_credentials() == ("secret-token", "acme")
    assert calls["n"] == 1
    client.invalidate()
    assert client.management_credentials(force=True)[1] == "acme"
    assert calls["n"] == 2

    def empty(args, stdin=None, vault_addr=""):
        return {"data": {"data": {"SOMETHING_ELSE": "hidden"}}}

    client = VaultClient(settings, runner=empty)
    with pytest.raises(AgentError, match="SOMETHING_ELSE") as caught:
        client.management_credentials()
    assert "hidden" not in caught.value.message


def test_api_rereads_token_after_401(respx_mock):
    seen = []

    def provider(force=False):
        token = "second" if force else "first"
        seen.append(token)
        return token, "acme"

    respx_mock.get("https://api.supabase.com/v1/projects/abc").mock(
        side_effect=[
            httpx.Response(401, text="unauthorized"),
            httpx.Response(
                200,
                json={
                    "ref": "abc",
                    "name": "demo-acme-20261002",
                    "status": "ACTIVE_HEALTHY",
                },
            ),
        ]
    )
    project = SupabaseApi(provider).get_project("abc")
    assert project["status"] == "ACTIVE_HEALTHY"
    assert seen == ["first", "second"]


def test_api_404_is_its_own_error(respx_mock):
    from supabase_charter.errors import ProjectNotFoundError

    respx_mock.get("https://api.supabase.com/v1/projects/gone").mock(
        return_value=httpx.Response(404, json={"message": "Project not found"})
    )
    respx_mock.delete("https://api.supabase.com/v1/projects/busy").mock(
        return_value=httpx.Response(500, text="boom")
    )
    api = SupabaseApi(lambda force=False: ("token", "acme"))
    with pytest.raises(ProjectNotFoundError, match="404"):
        api.get_project("gone")
    with pytest.raises(AgentError, match="500") as caught:
        api.delete_project("busy")
    assert not isinstance(caught.value, ProjectNotFoundError)


def test_delete_refuses_unknown_and_unconfirmed(tmp_path):
    from supabase_charter.provision import ProvisionService

    path = tmp_path / "databases.yaml"
    ledger = Ledger(path, "manual")
    ledger.load()
    ledger.add(
        DatabaseRecord(
            id="7f3c9a2e-1111-2222-3333-444444444444",
            name="demo-acme-20261002",
            kind="demo",
            ref="abcdef",
            environment="staging",
            secret_path="acme-demo",
            vault_path="staging/acme-demo/demo-acme-20261002",
            size="micro",
            region="eu-central-1",
            requested_by="dev@example.com",
            purpose="Acme",
            created_at="2026-10-02T09:00:00Z",
            status="active",
        )
    )
    ledger.save()

    class Api:
        def get_project(self, ref):
            return {"name": "demo-acme-20261002", "ref": ref}

        def delete_project(self, ref):
            raise AssertionError("must not delete without confirm")

    class Vault:
        def kv_get(self, mount, path):
            raise AssertionError("must not read secrets when confirm is missing")

    service = ProvisionService(Settings(), Vault(), Api(), ledger, connect=None)
    with pytest.raises(AgentError, match="out of scope"):
        service.delete_db("not-ours", confirm=True)
    with pytest.raises(AgentError, match="confirm=true"):
        service.delete_db("demo-acme-20261002", confirm=False)


def test_next_project_cost_mirrors_official_mcp():
    from supabase_charter.pricing import next_project_cost

    paid = {"id": "org1", "plan": "pro"}
    free = {"id": "org1", "plan": "free"}
    projects = [
        {"organization_id": "org1", "status": "ACTIVE_HEALTHY"},
        {"organization_id": "org1", "status": "REMOVED"},
        {"organization_id": "other", "status": "ACTIVE_HEALTHY"},
    ]

    micro = next_project_cost(paid, projects, "micro", ttl_days=3)
    assert micro["amount"] == 10
    assert micro["active_projects"] == 1
    assert micro["estimated_for_ttl_usd"] == 1.0
    assert "$10/month" in micro["message"]
    assert "3 day" in micro["message"]

    large = next_project_cost(paid, projects, "large", ttl_days=3)
    assert large["amount"] == 110
    assert large["estimated_for_ttl_usd"] == 11.0
    assert large["confirm_cost_id"] != micro["confirm_cost_id"]
    assert "estimated_for_ttl_usd" not in next_project_cost(paid, projects, "large")

    assert next_project_cost(paid, [], "micro")["amount"] == 0
    assert next_project_cost(free, projects, "micro")["amount"] == 0
    # No org id (plan from settings): every visible active project counts.
    unknown_org = next_project_cost({"id": None, "plan": "pro"}, projects, "micro")
    assert unknown_org["active_projects"] == 2
    with pytest.raises(AgentError, match="paid"):
        next_project_cost(free, projects, "large")
    with pytest.raises(AgentError, match="size must be one of"):
        next_project_cost(paid, projects, "huge")


def test_provision_requires_matching_confirm_cost_id(tmp_path):
    from supabase_charter.provision import ProvisionService

    class Api:
        def list_projects(self):
            return [{"organization_id": "org1", "status": "ACTIVE_HEALTHY"}]

        def create_project(self, **kwargs):
            raise AssertionError("must not create without a matching confirm_cost_id")

    class Vault:
        def management_credentials(self, force=False):
            raise AssertionError("cost quote must not need the org endpoint")

    ledger = Ledger(tmp_path / "databases.yaml", "manual")
    settings = Settings(DB_AGENT_ORG_PLAN="pro")
    service = ProvisionService(settings, Vault(), Api(), ledger, connect=None)
    quote = service.cost("micro", 1)
    assert quote["amount"] == 10
    assert quote["plan"] == "pro"
    assert quote["estimated_for_ttl_usd"] == 0.33
    for bad in ("", "stale"):
        with pytest.raises(AgentError, match="confirm_cost_id"):
            service.provision_db(
                kind="test",
                purpose="smoke",
                environment="staging",
                secret_path="sandbox",
                ttl_days=1,
                confirm_cost_id=bad,
            )
    ledger.load()
    assert not ledger.doc.databases


def test_database_url_prefers_session_pooler():
    from supabase_charter.provision import database_url

    host = "aws-0-eu-central-1.pooler.supabase.com"
    transaction = {
        "database_type": "PRIMARY",
        "db_host": host,
        "db_user": "postgres.abcdef",
        "db_name": "postgres",
        "db_port": 6543,
        "pool_mode": "transaction",
    }
    session = {**transaction, "db_port": 5432, "pool_mode": "session"}
    password = "p@ss/word"
    encoded = "p%40ss%2Fword"

    url = database_url("abcdef", password, [transaction, session])
    assert url == f"postgresql://postgres.abcdef:{encoded}@{host}:5432/postgres"

    url = database_url("abcdef", password, [transaction])
    assert url == f"postgresql://postgres.abcdef:{encoded}@{host}:5432/postgres"

    direct = f"postgresql://postgres:{encoded}@db.abcdef.supabase.co:5432/postgres"
    assert database_url("abcdef", password, None) == direct
    assert database_url("abcdef", password, []) == direct


def test_sources_catalog_is_not_a_fixed_list():
    settings = Settings(
        DB_AGENT_SOURCES="analytics=secret/supabase-sources/analytics,acme=secret/supabase-sources/acme"
    )
    assert settings.sources() == {
        "analytics": "secret/supabase-sources/analytics",
        "acme": "secret/supabase-sources/acme",
    }
    assert Settings().sources() == {}


def test_source_resolver_accepts_vault_paths_and_never_leaks_passwords():
    from supabase_charter.sources import SourceResolver

    secrets = {
        ("prod", "analytics/analytics-db"): {
            "DATABASE_URL": "postgresql://postgres.abc:s3cr3t@aws-0-eu-central-1.pooler.supabase.com:5432/postgres",
            "SUPABASE_SERVICE_ROLE_KEY": "sk",
        },
        ("prod", "platform/orchestrator"): {
            "POSTGRES_HOST": "db.example.com",
            "POSTGRES_PASSWORD": "p@ss",
            "POSTGRES_DB_NAME": "orchestrator",
            "POSTGRES_USERNAME": "app",
            "POSTGRES_PORT": "6543",
        },
        ("prod", "services/charter"): {
            "database-url": "postgresql://postgres:pw@db.ref.supabase.co:5432/postgres"
        },
        ("prod", "platform/no-db"): {"SUPABASE_URL": "https://x.supabase.co"},
    }

    class Vault:
        def kv_get(self, mount, path):
            from supabase_charter.vault import VaultSecret

            return VaultSecret(data=secrets[(mount, path)])

        def kv_list(self, mount, path):
            return ["analytics-db"] if path == "analytics" else []

    resolver = SourceResolver(
        {"analytics": "prod/analytics/analytics-db"}, {"prod", "staging"}, Vault()
    )
    assert resolver.names() == ["analytics"]
    assert resolver.list_folder("prod", "analytics") == ["analytics-db"]

    info = resolver.describe("analytics").public()
    assert info["vault_path"] == "prod/analytics/analytics-db"
    assert info["dsn_key"] == "DATABASE_URL"
    assert info["host"] == "aws-0-eu-central-1.pooler.supabase.com"
    assert info["user"] == "postgres.abc"
    assert info["usable"] is True
    assert "s3cr3t" not in str(info)

    direct = resolver.describe("prod/platform/orchestrator").public()
    assert direct["composed_from"] == [
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USERNAME",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB_NAME",
    ]
    assert direct["database"] == "orchestrator"
    assert direct["user"] == "app" and direct["port"] == "6543"
    assert "p@ss" not in str(direct) and "p%40ss" not in str(direct)
    # a non-Supabase host on the transaction-pooler port is refused, not guessed
    assert direct["usable"] is False and "transaction-mode pooler" in direct["problem"]
    with pytest.raises(AgentError, match="transaction-mode pooler"):
        resolver.resolve("prod/platform/orchestrator")

    hyphen = resolver.describe("prod/services/charter").public()
    assert hyphen["dsn_key"] == "database-url" and hyphen["usable"] is True
    assert ":pw@" not in str(hyphen)

    missing = resolver.describe("prod/platform/no-db").public()
    assert missing["usable"] is False and "No connection string" in missing["problem"]
    with pytest.raises(AgentError, match="no usable connection string"):
        resolver.resolve("prod/platform/no-db")
    with pytest.raises(AgentError, match="mounts"):
        resolver.describe("secret/supabase")


def test_settings_reject_unknown_environment():
    settings = Settings(DB_AGENT_VAULT_MOUNTS="staging=staging")
    with pytest.raises(ValueError, match="dev"):
        settings.mount_for("dev")
    assert settings.mount_for("staging") == "staging"


def test_settings_lists_ignore_whitespace_and_empty_items():
    settings = Settings(
        DB_AGENT_KINDS=" demo, dev ,,poc,",
        DB_AGENT_PROTECTED_REFS="abcdefghijklmnopqrst, uvwxyzabcdefghijklmn ",
        DB_AGENT_SOURCE_MOUNTS=" prod ,staging",
    )
    assert settings.kind_list() == ["demo", "dev", "poc"]
    assert settings.protected_ref_set() == {
        "abcdefghijklmnopqrst",
        "uvwxyzabcdefghijklmn",
    }
    assert settings.source_mount_set() == {"prod", "staging"}
    assert Settings(DB_AGENT_PROTECTED_REFS=" ").protected_ref_set() == set()
    # Built-in default, independent of any .env next to the package.
    assert Settings(_env_file=None).kind_list() == [
        "dev",
        "staging",
        "demo",
        "prod",
        "poc",
        "test",
    ]


def test_staging_kind_names_a_staging_backend():
    from datetime import date

    from supabase_charter.names import project_name

    name = project_name("staging", "listings staging serving", date(2026, 10, 14))
    assert name == "staging-listings-staging-serving-20261014"


def test_copy_select_keeps_values_as_literals():
    from psycopg import sql

    from supabase_charter.sql import copy_out_query

    query = copy_out_query("prices", "created_at", "2026-01-01", "2026-04-01", None)
    rendered = query.as_string(None)
    assert '"prices"' in rendered
    assert '"created_at"' in rendered
    assert "2026-01-01" in rendered
    assert "drop table" not in rendered
    assert isinstance(query, sql.Composed)
    with pytest.raises(AgentError):
        copy_out_query("prices;drop", None, None, None, None)

    open_start = copy_out_query("prices", "created_at", "2026-07-07", None, None)
    assert open_start.as_string(None).endswith(
        "\"created_at\" >= '2026-07-07') TO STDOUT"
    )
    # A date-only end is the whole day: the bound is the next day, exclusive.
    open_end = copy_out_query("prices", "created_at", None, "2026-10-08", None)
    assert "\"created_at\" < '2026-10-09'" in open_end.as_string(None)
    # A timestamp end is taken as written.
    stamped = copy_out_query("prices", "created_at", None, "2026-10-08T10:00:00Z", None)
    assert "\"created_at\" < '2026-10-08T10:00:00Z'" in stamped.as_string(None)
    assert "WHERE" not in copy_out_query(
        "prices", "created_at", None, None, None
    ).as_string(None)

    from supabase_charter.sql import (
        copy_in_query,
        period_end_bound,
        truncate_query,
    )

    assert period_end_bound("2026-03-31") == "2026-04-01"
    assert period_end_bound("2026-12-31") == "2027-01-01"
    assert period_end_bound("2026-03-31 23:00") == "2026-03-31 23:00"
    assert period_end_bound(None) is None

    # Explicit column lists on both ends; generated columns are not in them.
    listed = copy_out_query("prices", None, None, None, None, ["id", "amount"])
    assert listed.as_string(None).startswith(
        'COPY (SELECT "id", "amount" FROM "public"."prices") TO STDOUT'
    )
    assert (
        copy_in_query("prices", ["id", "amount"]).as_string(None)
        == 'COPY "public"."prices" ("id", "amount") FROM STDIN'
    )
    with pytest.raises(AgentError):
        copy_in_query("prices", ["id; drop"])

    # One statement for all tables, no CASCADE.
    rendered = truncate_query(["customers", "orders"]).as_string(None)
    assert rendered == (
        'TRUNCATE TABLE "public"."customers", "public"."orders" RESTART IDENTITY'
    )
    assert "CASCADE" not in rendered
    with pytest.raises(AgentError):
        truncate_query([])


def test_sources_are_reached_through_the_session_pooler():
    from supabase_charter.sources import session_mode_dsn

    pooler = "aws-1-eu-north-1.pooler.supabase.com"
    tx = f"postgresql://postgres.ref:pw@{pooler}:6543/postgres?pgbouncer=true&sslmode=require"
    dsn, note = session_mode_dsn(tx)
    assert dsn == f"postgresql://postgres.ref:pw@{pooler}:5432/postgres?sslmode=require"
    assert "transaction pooler" in note and "5432" in note

    session = f"postgresql://postgres.ref:pw@{pooler}:5432/postgres"
    assert session_mode_dsn(session) == (session, "")
    direct = "postgresql://postgres:pw@db.ref.supabase.co:5432/postgres"
    assert session_mode_dsn(direct) == (direct, "")

    with pytest.raises(AgentError, match="transaction-mode pooler"):
        session_mode_dsn("postgresql://u:p@pgbouncer.internal:6543/app")
    with pytest.raises(AgentError, match="transaction-mode pooler"):
        session_mode_dsn("postgresql://u:p@db.internal:5432/app?pgbouncer=true")


def test_connect_postgres_readonly_sets_nothing_at_session_level(monkeypatch):
    import supabase_charter.app as app_module

    class Conn:
        read_only = None
        executed: list[str] = []

        def execute(self, statement, *args):
            self.executed.append(str(statement))

        def transaction(self):
            raise AssertionError("no transaction should be opened at connect time")

    monkeypatch.setattr(app_module.psycopg, "connect", lambda dsn: Conn())
    conn = app_module.connect_postgres("postgresql://x", readonly=True)
    assert conn.read_only is True
    assert conn.executed == []


class _FakeSourceConn:
    """Answers the catalog queries plan_copy issues; counts return a constant."""

    def __init__(self, tables, columns, edges, count=7):
        self.tables, self.columns, self.edges, self.count = (
            tables,
            columns,
            edges,
            count,
        )
        self.count_queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        pass

    def cursor(self):
        conn = self

        class Cursor:
            description = None

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, query, params=None):
                text = (
                    query.as_string(None) if hasattr(query, "as_string") else str(query)
                )
                if "relkind = 'r'" in text:
                    self.description = [("name",), ("estimate",)]
                    self.rows = [(t, 10) for t in conn.tables]
                elif "contype = 'f'" in text and "conkey" not in text:
                    self.description = [("child",), ("parent",)]
                    self.rows = conn.edges
                elif "information_schema.columns" in text:
                    self.description = [
                        ("table_name",),
                        ("column_name",),
                        ("data_type",),
                        ("is_generated",),
                    ]
                    self.rows = [
                        tuple(c) if len(c) == 4 else (*c, "NEVER") for c in conn.columns
                    ]
                elif "typcategory" in text:  # dependency types: none
                    self.description = [
                        ("oid",),
                        ("name",),
                        ("schema",),
                        ("kind",),
                        ("extension",),
                    ]
                    self.rows = []
                elif "pg_get_function_identity_arguments" in text:  # functions: none
                    self.description = [
                        ("oid",),
                        ("name",),
                        ("schema",),
                        ("args",),
                        ("extension",),
                    ]
                    self.rows = []
                elif text.startswith("TRUNCATE"):
                    conn.count_queries.append(text)
                    self.rows = []
                else:
                    conn.count_queries.append(text)
                    self.rows = [(conn.count,)]

            def fetchall(self):
                return self.rows

            def fetchone(self):
                return self.rows[0]

        return Cursor()


def test_plan_recommends_copy_all_for_parents_and_copy_refuses_undecided(monkeypatch):
    import supabase_charter.copy as copy_module
    from supabase_charter.copy import CopyService

    tables = ["regions", "customers", "orders", "audit_log"]
    columns = [
        ("regions", "code", "text"),
        ("customers", "id", "integer"),
        ("customers", "created_at", "timestamp with time zone"),
        ("customers", "fts", "tsvector", "ALWAYS"),  # stored generated column
        ("orders", "id", "integer"),
        ("orders", "created_at", "timestamp with time zone"),
        ("audit_log", "id", "integer"),
    ]
    edges = [("customers", "regions"), ("orders", "customers")]

    class Resolver:
        def resolve(self, source):
            return "postgresql://source"

    conn = _FakeSourceConn(tables, columns, edges)
    service = CopyService(Resolver(), lambda dsn, readonly=False: conn)
    args = dict(
        source="s",
        tables=["audit_log", "orders", "customers", "regions"],
        column="created_at",
        start="2026-07-07",
        end=None,
        limit=None,
        mode="append",
        schema_only=False,
    )
    plan = service.plan_copy(**args)
    assert plan["tables"] == ["audit_log", "regions", "customers", "orders"]
    flagged = {f["table"]: f["recommended"] for f in plan["flags"]}
    assert flagged == {"regions": "copy_all", "audit_log": "skip"}
    assert plan["row_estimates"]["regions"] is None

    # Undecided flags: the copy refuses before touching the target.
    with pytest.raises(AgentError, match="regions \\(recommended: copy_all\\)"):
        service.copy_tables(source="s", target_dsn="t", plan=plan)

    # Decided: regions copied whole (no period), audit_log skipped.
    decided = service.plan_copy(**args, copy_all=["regions"], skip=["audit_log"])
    assert decided["flags"] == []
    assert decided["row_estimates"]["regions"] == 7
    assert decided["row_estimates"]["audit_log"] is None

    streamed = []
    monkeypatch.setattr(
        copy_module,
        "_stream_table",
        lambda connect, s, t, table, cols, column, start, end, limit: (
            streamed.append((table, cols, column, start)) or 1
        ),
    )
    monkeypatch.setattr(copy_module, "count_orphans", lambda conn, children: {})
    monkeypatch.setattr(copy_module, "_psql", lambda dsn, script, binary: None)
    service.dump_runner = lambda dsn, tables: "-- ddl"
    outcome = service.copy_tables(source="s", target_dsn="t", plan=decided)
    # Explicit column lists, generated columns left out (customers.fts).
    assert streamed == [
        ("regions", ["code"], None, None),
        ("customers", ["id", "created_at"], "created_at", "2026-07-07"),
        ("orders", ["id", "created_at"], "created_at", "2026-07-07"),
    ]
    assert outcome["skipped"] == ["audit_log"]
    assert outcome["truncated"] == [] and outcome["column_drift"] == {}
    assert not any(q.startswith("TRUNCATE") for q in conn.count_queries)

    with pytest.raises(AgentError, match="both copy_all and skip"):
        service.plan_copy(**args, copy_all=["regions"], skip=["regions"])
    with pytest.raises(AgentError, match="not in the copy"):
        service.plan_copy(**args, copy_all=["elsewhere"])

    # replace: only the listed tables are emptied, never their children.
    alone = service.plan_copy(**{**args, "tables": ["customers"], "mode": "replace"})
    assert alone["truncates"] == ["customers"]
    assert any("orders" in note and "mode=append" in note for note in alone["notes"])
    with pytest.raises(AgentError, match=r"also have to empty \['orders'\]"):
        service.copy_tables(source="s", target_dsn="t", plan=alone)
    assert not any(q.startswith("TRUNCATE") for q in conn.count_queries)

    both = service.plan_copy(
        **{**args, "tables": ["orders", "customers"], "mode": "replace"}
    )
    outcome = service.copy_tables(source="s", target_dsn="t", plan=both)
    truncates = [q for q in conn.count_queries if q.startswith("TRUNCATE")]
    assert truncates == [
        'TRUNCATE TABLE "public"."customers", "public"."orders" RESTART IDENTITY'
    ]
    assert outcome["truncated"] == ["customers", "orders"]

    # A date-only end means the whole day.
    dated = service.plan_copy(**{**args, "end": "2026-09-30"})
    assert dated["period"] == {
        "column": "created_at",
        "from": "2026-07-07",
        "to": "2026-09-30",
        "end_inclusive": True,
    }
    stamped = service.plan_copy(**{**args, "end": "2026-09-30T12:00:00Z"})
    assert stamped["period"]["end_inclusive"] is False


def test_orphan_count_query_handles_composite_keys():
    from supabase_charter.copy import orphan_count_query

    rendered = orphan_count_query(
        "order_items", ["order_id", "tenant"], "orders", ["id", "tenant"]
    ).as_string(None)
    assert rendered.startswith('SELECT count(*) FROM "public"."order_items" c WHERE')
    assert 'c."order_id" IS NOT NULL AND c."tenant" IS NOT NULL' in rendered
    assert 'p."id" = c."order_id" AND p."tenant" = c."tenant"' in rendered
    with pytest.raises(AgentError):
        orphan_count_query("x; drop", ["a"], "y", ["b"])


def test_copy_tables_requires_confirm_replace_only_for_replace():
    from supabase_charter.app import AgentApp

    app = AgentApp.__new__(AgentApp)
    with pytest.raises(AgentError, match="confirm_replace=true"):
        app.copy_tables("db", mode="replace", source="s", tables=["t"])


def test_plan_notes_missing_parents_and_copy_skips_existing_tables():
    from supabase_charter.copy import CopyService, _missing_parents

    edges = [("customers", "regions"), ("orders", "customers"), ("x", "y")]
    assert _missing_parents(["customers", "orders"], edges) == [
        ("customers", "regions")
    ]
    assert _missing_parents(["regions", "customers"], edges) == []

    class Resolver:
        def resolve(self, source):
            return "postgresql://target"

    class Cursor:
        description = [("name",), ("estimate",)]

        def __init__(self, rows):
            self.rows = rows

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *_):
            pass

        def fetchall(self):
            return self.rows

    class Conn:
        def __init__(self, tables):
            self.tables = tables

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return Cursor([(t, 0) for t in self.tables])

    target_tables = ["regions", "customers"]  # left over from a failed run
    dumped: list[list[str]] = []

    def dump_runner(_dsn, tables):
        dumped.append(tables)
        return "-- ddl"

    import supabase_charter.copy as copy_module

    applied = []
    original = copy_module._psql
    copy_module._psql = lambda dsn, script, binary: applied.append((script, binary))
    try:
        service = CopyService(
            Resolver(),
            lambda dsn, readonly=False: Conn(target_tables),
            dump_runner,
            psql="/opt/pg17/bin/psql",
        )
        service._dependency_script = lambda s, t, tables: (
            "CREATE TYPE public.order_status AS ENUM ('new');\n",
            {"types": ["public.order_status (enum)"]},
        )
        plan = {
            "tables": ["customers", "orders"],
            "schema_only": True,
            "missing_parents": [{"table": "customers", "parent": "regions"}],
        }
        outcome = service.copy_tables(source="s", target_dsn="t", plan=plan)
        assert dumped == [["orders"]]  # only the table the target lacks
        # Dependencies first, then the dump, one psql run from Settings' binary.
        assert applied == [
            (
                "CREATE TYPE public.order_status AS ENUM ('new');\n-- ddl",
                "/opt/pg17/bin/psql",
            )
        ]
        assert outcome["dependencies_created"] == {
            "types": ["public.order_status (enum)"]
        }
        assert outcome["schema_created"] == ["orders"]
        assert outcome["schema_existing"] == ["customers"]

        plan["missing_parents"] = [{"table": "orders", "parent": "shops"}]
        with pytest.raises(AgentError, match="shops"):
            service.copy_tables(source="s", target_dsn="t", plan=plan)
    finally:
        copy_module._psql = original


# -- provision / delete / rotate against fakes --------------------------------


class _FakeApi:
    """Management API fake: one live project, every call recorded."""

    def __init__(self, projects=None):
        self.projects = projects if projects is not None else []
        self.calls = []
        self.delete_error = None
        self.ref = "newref"

    def list_projects(self):
        return list(self.projects)

    def get_project(self, ref):
        from supabase_charter.errors import ProjectNotFoundError

        self.calls.append(("get", ref))
        for project in self.projects:
            if project["ref"] == ref:
                return project
        raise ProjectNotFoundError(f"Supabase API GET /v1/projects/{ref} failed (404)")

    def create_project(self, **kwargs):
        self.calls.append(("create", kwargs["name"]))
        self.projects.append(
            {"ref": self.ref, "name": kwargs["name"], "status": "ACTIVE_HEALTHY"}
        )
        return {"ref": self.ref}

    def wait_until_healthy(self, ref):
        return self.get_project(ref)

    def api_keys(self, ref):
        return {"anon": "anon-key", "service_role": "service-key"}

    def pooler(self, ref):
        return []

    def update_password(self, ref, password):
        self.calls.append(("password", ref))

    def delete_project(self, ref):
        self.calls.append(("delete", ref))
        if self.delete_error:
            raise AgentError(self.delete_error)
        self.projects = [p for p in self.projects if p["ref"] != ref]


class _FakeVault:
    """KV v2 fake: versions per path, optional permission denied on delete."""

    def __init__(self, store=None, *, can_delete=True):
        self.store = store or {}  # "mount/path" -> VaultSecret
        self.calls = []
        self.can_delete = can_delete
        self.delete_error = None  # raised by kv_delete (not a permission issue)
        self.put_error = None  # raised by kv_put when set
        self.history = {}  # (key, version) -> VaultSecret, older versions

    def management_credentials(self, force=False):
        return "token", "acme"

    def kv_get(self, mount, path, *, version=None):
        key = f"{mount}/{path}"
        secret = self.store.get(key)
        if version is not None and (secret is None or secret.version != version):
            secret = self.history.get((key, version))
        if secret is None:
            raise AgentError(f"No value found at {mount}/data/{path}")
        return secret

    def kv_peek(self, mount, path):
        return self.store.get(f"{mount}/{path}")

    def kv_put(self, mount, path, data, *, cas=None):
        from supabase_charter.vault import VaultSecret

        key = f"{mount}/{path}"
        current = self.store.get(key)
        version = current.version if current else 0
        self.calls.append(("put", key, cas, sorted(data)))
        if self.put_error:
            raise AgentError(self.put_error)
        if cas is not None and cas != version:
            raise AgentError(
                "check-and-set parameter did not match the current version"
            )
        if current is not None:
            self.history[(key, version)] = current
        self.store[key] = VaultSecret(data=dict(data), version=version + 1)

    def kv_delete(self, mount, path):
        self.calls.append(("delete", f"{mount}/{path}"))
        if self.delete_error:
            raise AgentError(self.delete_error)
        if not self.can_delete:
            self.kv_put(mount, path, {"STATUS": "deleted"})
            return "tombstoned"
        self.store.pop(f"{mount}/{path}", None)
        return "deleted"


class _MarkerConn:
    """Postgres fake: the marker row exists iff `marker` is True."""

    def __init__(self, marker):
        self.marker = marker
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def execute(self, query, params=None):
        self.executed.append(query)

    def fetchone(self):
        return (1,) if self.marker else None

    def commit(self):
        pass


def _service(tmp_path, api, vault, *, marker=True, records=()):
    from supabase_charter.provision import ProvisionService

    ledger = Ledger(tmp_path / "databases.yaml", "manual")
    ledger.load()
    for record in records:
        ledger.add(record)
    ledger.save()
    settings = Settings(DB_AGENT_PROTECTED_REFS="prodref", DB_AGENT_ORG_PLAN="free")
    connect = lambda dsn, readonly=False: _MarkerConn(marker)  # noqa: E731
    return ProvisionService(settings, vault, api, ledger, connect), ledger


def _owned_record(name="demo-acme-20261002", *, ref="abcdef", status="active"):
    return DatabaseRecord(
        id="7f3c9a2e-1111-2222-3333-444444444444",
        name=name,
        kind="demo",
        ref=ref,
        environment="staging",
        secret_path="acme-demo",
        vault_path=f"staging/acme-demo/{name}",
        size="micro",
        region="eu-central-1",
        requested_by="dev@example.com",
        purpose="Acme",
        created_at="2026-10-02T09:00:00Z",
        status=status,
    )


def _secret(url="postgresql://u:p@h:5432/d", version=1):
    from supabase_charter.vault import VaultSecret

    return VaultSecret(
        data={"DATABASE_URL": url, "POSTGRES_PASSWORD": "p"}, version=version
    )


def test_delete_succeeds_records_failures_and_tombstones(tmp_path):
    record = _owned_record()
    live = {"ref": "abcdef", "name": record.name, "status": "ACTIVE_HEALTHY"}
    api = _FakeApi([live])
    vault = _FakeVault({record.vault_path: _secret()}, can_delete=False)
    service, ledger = _service(tmp_path, api, vault, records=[record])

    # The API error is what the caller sees, and the ledger remembers the attempt.
    api.delete_error = "Supabase API DELETE failed (500): boom"
    with pytest.raises(AgentError, match="boom"):
        service.delete_db(record.name, confirm=True)
    ledger.load()
    assert ledger.find(record.name).status == "delete_failed"
    assert vault.calls == []  # Vault untouched while the project still exists

    api.delete_error = None
    result = service.delete_db(record.name, confirm=True)
    assert result["status"] == "deleted"
    assert result["vault"] == "tombstoned"  # metadata delete denied -> tombstone
    ledger.load()
    done = ledger.find(record.name)
    assert done.status == "deleted" and done.deleted_at and done.deleted_by
    with pytest.raises(AgentError, match="out of scope"):
        service.delete_db(record.name, confirm=True)


def test_delete_marks_the_ledger_before_touching_supabase(tmp_path):
    record = _owned_record()
    live = {"ref": "abcdef", "name": record.name, "status": "ACTIVE_HEALTHY"}
    api = _FakeApi([live])
    vault = _FakeVault({record.vault_path: _secret()})
    service, ledger = _service(tmp_path, api, vault, records=[record])
    seen = []
    original = ledger.save

    def spy(message=""):
        seen.append((message, ledger.find(record.name).status, list(api.calls)))
        return original(message)

    ledger.save = spy
    service.delete_db(record.name, confirm=True)
    # First save: status `deleting`, and Supabase has not been asked to delete yet.
    first_message, first_status, calls_at_first_save = seen[0]
    assert "(started)" in first_message and first_status == "deleting"
    assert ("delete", "abcdef") not in calls_at_first_save
    assert seen[-1][1] == "deleted"


def test_delete_finishes_on_retry_when_vault_failed_after_the_project_was_gone(
    tmp_path,
):
    record = _owned_record()
    live = {"ref": "abcdef", "name": record.name, "status": "ACTIVE_HEALTHY"}
    api = _FakeApi([live])
    vault = _FakeVault({record.vault_path: _secret()})
    service, ledger = _service(tmp_path, api, vault, records=[record])

    # Supabase deleted the project, then Vault blew up (expired token, network).
    vault.delete_error = "Vault said: connection refused"
    with pytest.raises(AgentError, match="Run the deletion of .* again") as caught:
        service.delete_db(record.name, confirm=True)
    assert "connection refused" in caught.value.message
    assert ("delete", "abcdef") in api.calls and api.projects == []
    ledger.load()
    assert ledger.find(record.name).status == "deleting"

    # Nothing but delete may touch a `deleting` entry.
    with pytest.raises(AgentError, match="Run the deletion again"):
        service.extend_db(record.name, 1)
    with pytest.raises(AgentError, match="Run the deletion again"):
        service.rotate_db_password(record.name)

    # Retry: the project is 404 at Supabase; that is expected now, not a refusal.
    vault.delete_error = None
    deletes_before = api.calls.count(("delete", "abcdef"))
    result = service.delete_db(record.name, confirm=True)
    assert result["status"] == "deleted" and "already gone" in result["note"]
    assert api.calls.count(("delete", "abcdef")) == deletes_before  # not re-sent
    assert record.vault_path not in vault.store
    ledger.load()
    assert ledger.find(record.name).status == "deleted"


def test_delete_retry_treats_a_removed_project_as_gone(tmp_path):
    # Supabase may keep listing a deleted project for a while as REMOVED.
    record = _owned_record(status="deleting")
    removed = {"ref": "abcdef", "name": record.name, "status": "REMOVED"}
    api = _FakeApi([removed])
    vault = _FakeVault({record.vault_path: _secret()})
    service, ledger = _service(tmp_path, api, vault, marker=False, records=[record])
    with pytest.raises(AgentError, match="confirm=true"):
        service.delete_db(record.name, confirm=False)
    result = service.delete_db(record.name, confirm=True)
    assert result["status"] == "deleted" and "already gone" in result["note"]
    assert not any(call[0] == "delete" for call in api.calls)
    assert ledger.find(record.name).status == "deleted"

    # Right after deletion Supabase answers 403 on GET for a while; the
    # listing, which no longer has the project, decides.
    class TearingDownApi(_FakeApi):
        def get_project(self, ref):
            self.calls.append(("get", ref))
            raise AgentError(
                f"Supabase API GET /v1/projects/{ref} failed (403): "
                '{"message":"Missing required permission(s): project_admin_read"}'
            )

    again = _owned_record("demo-again-20261002", ref="gone", status="deleting")
    api = TearingDownApi([{"ref": "other", "name": "x", "status": "ACTIVE_HEALTHY"}])
    service, ledger = _service(tmp_path / "c", api, _FakeVault({}), records=[again])
    assert service.delete_db(again.name, confirm=True)["status"] == "deleted"
    assert ledger.find(again.name).status == "deleted"

    # Same 403 but the project is still listed: not gone, the error stands.
    still = _owned_record("demo-still-20261002", ref="here", status="deleting")
    api = TearingDownApi(
        [{"ref": "here", "name": still.name, "status": "ACTIVE_HEALTHY"}]
    )
    service, ledger = _service(tmp_path / "d", api, _FakeVault({}), records=[still])
    with pytest.raises(AgentError, match="403"):
        service.delete_db(still.name, confirm=True)
    assert ledger.find(still.name).status == "deleting"

    # A protected ref is never closed quietly, whatever the ledger says.
    prod = _owned_record("demo-x-20261002", ref="prodref", status="deleting")
    api = _FakeApi([])
    service, _ = _service(tmp_path / "b", api, _FakeVault({}), records=[prod])
    with pytest.raises(AgentError, match="protected"):
        service.delete_db(prod.name, confirm=True)


def test_active_entry_whose_project_vanished_is_reported_not_closed(tmp_path):
    record = _owned_record()  # active in the ledger, gone at Supabase
    api = _FakeApi([])
    vault = _FakeVault({record.vault_path: _secret()})
    service, ledger = _service(tmp_path, api, vault, records=[record])
    with pytest.raises(AgentError, match="no project abcdef anymore"):
        service.delete_db(record.name, confirm=True)
    with pytest.raises(AgentError, match="no project abcdef anymore"):
        service.extend_db(record.name, 1)
    assert vault.calls == []
    assert ledger.find(record.name).status == "active"

    # The listing tells the developer what an unfinished entry needs.
    stuck = _owned_record("demo-stuck-20261002", ref="zz", status="deleting")
    service, _ = _service(tmp_path / "b", api, vault, records=[record, stuck])
    rows = {row["name"]: row for row in service.list_dbs()}
    assert "delete this database again" in rows[stuck.name]["action_needed"]
    assert "action_needed" not in rows[record.name]


def test_delete_still_works_when_the_marker_is_missing(tmp_path):
    record = _owned_record()
    live = {"ref": "abcdef", "name": record.name, "status": "ACTIVE_HEALTHY"}

    # Marker row absent on a finished (active) database: not ours, refused.
    api = _FakeApi([live])
    vault = _FakeVault({record.vault_path: _secret()})
    service, _ = _service(tmp_path, api, vault, marker=False, records=[record])
    with pytest.raises(AgentError, match="not created by this agent"):
        service.delete_db(record.name, confirm=True)
    assert ("delete", "abcdef") not in api.calls


def test_delete_cleans_up_a_half_provisioned_project(tmp_path):
    # provision_db died after create_project: no Vault entry, no marker row.
    record = _owned_record(status="provisioning")
    live = {"ref": "abcdef", "name": record.name, "status": "ACTIVE_HEALTHY"}
    api = _FakeApi([live])
    vault = _FakeVault({})
    service, ledger = _service(tmp_path, api, vault, marker=False, records=[record])

    # Anything but delete asks for a fresh provision.
    with pytest.raises(AgentError, match="did not finish provisioning"):
        service.extend_db(record.name, 2)
    with pytest.raises(AgentError, match="did not finish provisioning"):
        service.rotate_db_password(record.name)

    # Delete needs neither Vault nor the marker, nor force_unreachable.
    result = service.delete_db(record.name, confirm=True)
    assert result["status"] == "deleted"
    assert ("delete", "abcdef") in api.calls
    assert ledger.find(record.name).status == "deleted"

    # A wrong live name is still refused, provisioning or not.
    other = _owned_record("demo-other-20261002", ref="xyz", status="provisioning")
    api = _FakeApi([{"ref": "xyz", "name": "demo-acme-20261002"}])
    service, _ = _service(tmp_path / "b", api, _FakeVault({}), records=[other])
    with pytest.raises(AgentError, match="does not match"):
        service.delete_db(other.name, confirm=True)
    assert ("delete", "xyz") not in api.calls

    # Protected refs win over everything.
    prod = _owned_record("demo-x-20261002", ref="prodref", status="provisioning")
    api = _FakeApi([{"ref": "prodref", "name": "demo-x-20261002"}])
    service, _ = _service(tmp_path / "c", api, _FakeVault({}), records=[prod])
    with pytest.raises(AgentError, match="protected"):
        service.delete_db(prod.name, confirm=True)


def test_delete_without_a_ref_closes_the_entry_unless_a_live_twin_exists(tmp_path):
    record = _owned_record(ref="", status="provisioning")
    api = _FakeApi([{"ref": "zzz", "name": record.name, "status": "ACTIVE_HEALTHY"}])
    service, _ = _service(tmp_path, api, _FakeVault({}), records=[record])
    with pytest.raises(AgentError, match="cannot prove"):
        service.delete_db(record.name, confirm=True)
    assert not any(call[0] == "delete" for call in api.calls)

    api = _FakeApi([])
    service, ledger = _service(tmp_path / "b", api, _FakeVault({}), records=[record])
    result = service.delete_db(record.name, confirm=True)
    assert result["status"] == "deleted" and "no project ref" in result["note"]
    assert ledger.find(record.name).status == "deleted"


def test_lost_vault_entry_needs_force_unreachable_and_only_for_delete(tmp_path):
    record = _owned_record()  # active, but its Vault entry is gone
    live = {"ref": "abcdef", "name": record.name, "status": "ACTIVE_HEALTHY"}
    api = _FakeApi([live])
    service, _ = _service(tmp_path, api, _FakeVault({}), records=[record])

    with pytest.raises(AgentError, match="Vault has no secret"):
        service.rotate_db_password(record.name)
    with pytest.raises(AgentError, match="force_unreachable"):
        service.delete_db(record.name, confirm=True)
    assert ("delete", "abcdef") not in api.calls
    result = service.delete_db(record.name, confirm=True, force_unreachable=True)
    assert result["status"] == "deleted"

    # An expired Vault login is not "unverifiable": it is raised as such.
    class ExpiredVault(_FakeVault):
        def kv_peek(self, mount, path):
            from supabase_charter.vault import LOGIN_HINT

            raise AgentError(f"{LOGIN_HINT} Vault said: permission denied")

    again = _owned_record()
    service, _ = _service(
        tmp_path / "b", _FakeApi([live]), ExpiredVault(), records=[again]
    )
    with pytest.raises(AgentError, match="vault login"):
        service.delete_db(again.name, confirm=True, force_unreachable=True)


def test_provision_refuses_name_clashes_and_writes_vault_with_cas(tmp_path):
    from supabase_charter.names import project_name

    name = project_name("test", "smoke")
    vault_key = f"staging/sandbox/{name}"
    kwargs = dict(
        kind="test", purpose="smoke", environment="staging", secret_path="sandbox"
    )

    # Same name already live in the organization (someone else's unpublished DB).
    api = _FakeApi([{"ref": "theirs", "name": name, "status": "ACTIVE_HEALTHY"}])
    service, ledger = _service(tmp_path, api, _FakeVault({}))
    with pytest.raises(AgentError, match="already exists \\(ref theirs\\)"):
        service.provision_db(**kwargs)
    assert not any(call[0] == "create" for call in api.calls)
    assert ledger.find(name) is None

    # Vault path already holds a secret: refused before anything is created.
    vault = _FakeVault({vault_key: _secret(version=3)})
    service, ledger = _service(tmp_path / "b", _FakeApi([]), vault)
    with pytest.raises(AgentError, match="already holds a secret"):
        service.provision_db(**kwargs)
    assert vault.calls == [] and ledger.find(name) is None

    # Happy path on an empty path: written with cas=0, marker stamped, active.
    api, vault = _FakeApi([]), _FakeVault({})
    service, ledger = _service(tmp_path / "c", api, vault)
    created = service.provision_db(**kwargs)
    assert created["status"] == "active" and created["ref"] == "newref"
    assert vault.calls == [
        (
            "put",
            vault_key,
            0,
            sorted(
                [
                    "DATABASE_URL",
                    "POSTGRES_PASSWORD",
                    "PROJECT_REF",
                    "SUPABASE_ANON_KEY",
                    "SUPABASE_SERVICE_ROLE_KEY",
                    "SUPABASE_URL",
                ]
            ),
        )
    ]
    assert "password" not in str(created).lower()

    # A tombstone from an earlier delete is reused with its version as cas.
    from supabase_charter.vault import VaultSecret

    vault = _FakeVault({vault_key: VaultSecret({"STATUS": "deleted"}, version=4)})
    service, _ = _service(tmp_path / "d", _FakeApi([]), vault)
    service.provision_db(**kwargs)
    assert vault.calls[0][2] == 4

    # Lost the race after the check: the CAS failure is reported, not hidden.
    class RacedVault(_FakeVault):
        def kv_peek(self, mount, path):
            return None

    vault = RacedVault({vault_key: _secret(version=1)})
    service, ledger = _service(tmp_path / "e", _FakeApi([]), vault)
    with pytest.raises(AgentError, match="check-and-set"):
        service.provision_db(**kwargs)
    assert ledger.find(name).status == "provisioning"  # deletable, see above


def test_rotate_updates_vault_without_returning_the_password(tmp_path):
    record = _owned_record()
    live = {"ref": "abcdef", "name": record.name, "status": "ACTIVE_HEALTHY"}
    api = _FakeApi([live])
    vault = _FakeVault({record.vault_path: _secret(url="postgresql://u:old@h:5432/d")})
    service, _ = _service(tmp_path, api, vault, records=[record])
    result = service.rotate_db_password(record.name)
    assert result == {
        "name": record.name,
        "ref": "abcdef",
        "vault_path": record.vault_path,
        "password_rotated": True,
    }
    assert ("password", "abcdef") in api.calls
    stored = vault.store[record.vault_path].data
    assert stored["POSTGRES_PASSWORD"] not in ("p", "old")
    assert (
        stored["POSTGRES_PASSWORD"] in stored["DATABASE_URL"]
        or "%" in stored["DATABASE_URL"]
    )
    assert "old" not in stored["DATABASE_URL"]


def test_rotate_writes_vault_before_supabase_and_puts_it_back_on_failure(tmp_path):
    record = _owned_record()
    live = {"ref": "abcdef", "name": record.name, "status": "ACTIVE_HEALTHY"}
    vault = _FakeVault({record.vault_path: _secret(url="postgresql://u:old@h:5432/d")})

    class OrderedApi(_FakeApi):
        def update_password(self, ref, password):
            # By the time Supabase is asked, Vault already holds the new password.
            assert vault.store[record.vault_path].data["POSTGRES_PASSWORD"] == password
            super().update_password(ref, password)
            if self.password_error:
                raise AgentError(self.password_error)

    api = OrderedApi([live])
    api.password_error = None
    service, _ = _service(tmp_path, api, vault, records=[record])

    service.rotate_db_password(record.name)
    put = [call for call in vault.calls if call[0] == "put"]
    assert put[-1][2] == 1  # check-and-set on the version read before rotating
    assert vault.store[record.vault_path].version == 2

    # Supabase refuses: Vault goes back to what it was (as a new version).
    api.password_error = "Supabase API PATCH failed (500): boom"
    with pytest.raises(AgentError, match="Vault was put back") as caught:
        service.rotate_db_password(record.name)
    assert "boom" in caught.value.message
    rotated_once = vault.history[(record.vault_path, 2)].data
    assert vault.store[record.vault_path].data == rotated_once
    assert vault.store[record.vault_path].version == 4  # write, then revert

    # Supabase refuses and Vault cannot be reverted: say where the good one is.
    class HalfVault(_FakeVault):
        def kv_put(self, mount, path, data, *, cas=None):
            super().kv_put(mount, path, data, cas=cas)
            self.put_error = "Vault said: connection refused"  # fail from now on

    class RefusingApi(_FakeApi):
        def update_password(self, ref, password):
            raise AgentError("Supabase API PATCH failed (500): boom")

    broken = HalfVault({record.vault_path: _secret(version=5)})
    service, _ = _service(tmp_path / "b", RefusingApi([live]), broken, records=[record])
    with pytest.raises(AgentError, match="Vault version 5 still holds"):
        service.rotate_db_password(record.name)


def test_rotate_recovers_when_an_earlier_rotation_died_after_the_vault_write(
    tmp_path,
):
    # Vault version 2 holds a password the database never received; version 1
    # is the one the database still accepts.
    record = _owned_record()
    live = {"ref": "abcdef", "name": record.name, "status": "ACTIVE_HEALTHY"}
    api = _FakeApi([live])
    vault = _FakeVault({record.vault_path: _secret(url="postgresql://u:good@h/d")})
    vault.kv_put(
        "staging",
        record.vault_path.split("/", 1)[1],
        {"DATABASE_URL": "postgresql://u:unknown@h/d", "POSTGRES_PASSWORD": "x"},
    )
    assert vault.store[record.vault_path].version == 2

    def connect(dsn, readonly=False):
        if "unknown" in dsn:
            raise psycopg.OperationalError(
                'FATAL:  password authentication failed for user "u"'
            )
        return _MarkerConn(True)

    from supabase_charter.provision import ProvisionService

    ledger = Ledger(tmp_path / "databases.yaml", "manual")
    ledger.load()
    ledger.add(record)
    ledger.save()
    settings = Settings(DB_AGENT_PROTECTED_REFS="prodref", DB_AGENT_ORG_PLAN="free")
    service = ProvisionService(settings, vault, api, ledger, connect)

    # Anything else still refuses: the current credentials do not work.
    with pytest.raises(AgentError, match="could not be verified"):
        service.extend_db(record.name, 1)

    # Rotation proves ownership through the previous version and completes.
    result = service.rotate_db_password(record.name)
    assert result["password_rotated"] is True
    assert ("password", "abcdef") in api.calls
    assert vault.store[record.vault_path].version == 3
    assert "unknown" not in vault.store[record.vault_path].data["DATABASE_URL"]

    # A plain outage (not a rejected password) gets no such fallback.
    def down(dsn, readonly=False):
        raise psycopg.OperationalError("connection timed out")

    service = ProvisionService(settings, vault, api, ledger, down)
    with pytest.raises(AgentError, match="could not be verified"):
        service.rotate_db_password(record.name)


def test_vault_cli_wrapper_uses_cas_and_peeks_without_raising():
    seen = []

    def runner(args, stdin=None, vault_addr=""):
        seen.append(args)
        if args[:2] == ["kv", "get"]:
            raise AgentError(f"No value found at staging/data/{args[-1]}")
        if "-cas=0" in args:
            raise AgentError(
                "check-and-set parameter did not match the current version"
            )
        return {}

    client = VaultClient(Settings(), runner=runner)
    assert client.kv_peek("staging", "ns/db") is None
    with pytest.raises(AgentError, match="written by someone else"):
        client.kv_put("staging", "ns/db", {"A": "b"}, cas=0)
    assert seen[-1][:4] == ["kv", "put", "-mount=staging", "-cas=0"]
    client.kv_put("staging", "ns/db", {"A": "b"})  # rotate: plain overwrite
    assert "-cas" not in " ".join(seen[-1])

    def versioned(args, stdin=None, vault_addr=""):
        seen.append(args)
        return {"data": {"data": {"DATABASE_URL": "x"}, "metadata": {"version": 7}}}

    client = VaultClient(Settings(), runner=versioned)
    assert client.kv_get("m", "p").version == 7
    assert "-version=6" not in seen[-1]
    client.kv_get("m", "p", version=6)
    assert seen[-1] == ["kv", "get", "-mount=m", "-format=json", "-version=6", "p"]


def test_settings_find_env_next_to_the_package_and_psql_next_to_pg_dump(tmp_path):
    from supabase_charter.config import PACKAGE_ROOT

    assert Settings.model_config["env_file"] == PACKAGE_ROOT / ".env"

    explicit = Settings(DB_AGENT_PG_DUMP="/usr/lib/postgresql/17/bin/pg_dump")
    assert explicit.pg_dump == "/usr/lib/postgresql/17/bin/pg_dump"
    assert explicit.psql_binary() == "/usr/lib/postgresql/17/bin/psql"

    chosen = Settings(DB_AGENT_PG_DUMP="/x/pg_dump", DB_AGENT_PSQL="/y/psql")
    assert chosen.psql_binary() == "/y/psql"

    # Bare names: psql is taken from the directory where pg_dump is found.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for tool in ("pg_dump", "psql"):
        (fake_bin / tool).write_text("#!/bin/sh\n")
        (fake_bin / tool).chmod(0o755)
    import os

    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{fake_bin}{os.pathsep}{old}"
    try:
        assert Settings(DB_AGENT_PG_DUMP="pg_dump").psql_binary() == str(
            fake_bin / "psql"
        )
    finally:
        os.environ["PATH"] = old


def test_schema_dependencies_are_found_filtered_and_recreated():
    from supabase_charter import schema_deps as sd

    class CatalogConn:
        """Answers the catalog queries of schema_deps from canned rows."""

        def __init__(self, target=False):
            self.target = target

        def cursor(self):
            class Cursor:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def execute(self, query, params=()):
                    q = " ".join(query.split())
                    if "typcategory" in q:  # TYPES_SQL
                        self.description = [
                            ("oid",),
                            ("name",),
                            ("schema",),
                            ("kind",),
                            ("extension",),
                        ]
                        self.rows = [
                            (1, "order_status", "public", "e", None),
                            (2, "email", "public", "d", None),
                            (3, "citext", "extensions", "b", "citext"),
                            (4, "address", "public", "c", None),
                        ]
                    elif "pg_get_function_identity_arguments" in q and "procs" in q:
                        self.description = [
                            ("oid",),
                            ("name",),
                            ("schema",),
                            ("args",),
                            ("extension",),
                        ]
                        self.rows = [
                            (10, "set_updated_at", "public", "", None),
                            (11, "uuid_generate_v4", "extensions", "", "uuid-ossp"),
                        ]
                    elif "pg_extension e" in q:  # EXTENSION_SCHEMA_SQL
                        self.description = [("schema",)]
                        self.rows = [("extensions",)]
                    elif "pg_enum" in q:
                        self.description = [("enumlabel",)]
                        self.rows = [("new",), ("paid",)]
                    elif "typbasetype" in q:
                        self.description = [("base",), ("not_null",), ("default_expr",)]
                        self.rows = [("text", True, None)]
                    elif "contypid" in q:
                        self.description = [("name",), ("definition",)]
                        self.rows = [("email_check", "CHECK ((VALUE ~ '@'::text))")]
                    elif "pg_get_functiondef" in q:
                        self.description = [("definition",)]
                        self.rows = [
                            (
                                "CREATE OR REPLACE FUNCTION public.set_updated_at()\n"
                                " RETURNS trigger\n LANGUAGE plpgsql\nAS $function$\n"
                                "BEGIN NEW.updated_at = now(); RETURN NEW; END;\n$function$\n",
                            )
                        ]
                    elif q.startswith("SELECT extname"):  # target extensions
                        self.description = [("name",)]
                        self.rows = [("uuid-ossp",)]
                    elif "FROM pg_type t JOIN pg_namespace" in q:  # target types
                        self.description = [("name",), ("schema",)]
                        self.rows = [("email", "public")]
                    elif "FROM pg_proc p JOIN pg_namespace" in q:  # target functions
                        self.description = [("name",), ("schema",), ("args",)]
                        self.rows = []
                    else:
                        raise AssertionError(f"unexpected query: {q[:80]}")

                def fetchall(self):
                    return self.rows

            return Cursor()

    source = CatalogConn()
    deps = sd.find_dependencies(source, ["orders"])
    assert deps.public() == {
        "extensions": ["citext", "uuid-ossp"],
        "types": [
            "public.order_status (enum)",
            "public.email (domain)",
            "public.address (composite)",
        ],
        "functions": ["public.set_updated_at()"],
    }

    # The target already has uuid-ossp and the email domain.
    missing = sd.missing_in_target(CatalogConn(target=True), deps)
    assert missing.public() == {
        "extensions": ["citext"],
        "types": ["public.order_status (enum)", "public.address (composite)"],
        "functions": ["public.set_updated_at()"],
    }

    # A composite type cannot be recreated from here: refuse, name it.
    with pytest.raises(AgentError, match="public.address \\(composite\\)"):
        sd.dependency_ddl(source, missing)

    missing.types = [t for t in missing.types if t["kind"] != "composite"]
    ddl = sd.dependency_ddl(source, missing)
    statements = [line for line in ddl.splitlines() if not line.startswith("--")]
    assert (
        statements[0]
        == 'CREATE EXTENSION IF NOT EXISTS "citext" WITH SCHEMA "extensions";'
    )
    assert (
        statements[1]
        == "CREATE TYPE \"public\".\"order_status\" AS ENUM ('new', 'paid');"
    )
    assert statements[2].startswith(
        "CREATE OR REPLACE FUNCTION public.set_updated_at()"
    )
    assert ddl.rstrip().endswith("$function$;")

    # Domains carry base type, NOT NULL and their constraints.
    domain = sd._domain_ddl(source, {"oid": 2, "name": "email", "schema": "public"})
    assert domain == (
        'CREATE DOMAIN "public"."email" AS text NOT NULL '
        "CONSTRAINT \"email_check\" CHECK ((VALUE ~ '@'::text));"
    )
    assert sd.Dependencies().empty()


# -- remaining safety paths: bearer middleware, vault CLI errors, streaming ---


def test_bearer_middleware_rejects_missing_or_wrong_tokens():
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from supabase_charter.__main__ import BearerMiddleware

    async def mcp(_request):
        return PlainTextResponse("tool result")

    def client(token):
        app = Starlette(routes=[Route("/mcp", mcp, methods=["GET", "POST"])])
        app.add_middleware(BearerMiddleware, token=token)
        return TestClient(app)

    # No token configured: nothing is served, whatever the caller sends.
    unset = client("")
    assert unset.post("/mcp", headers={"Authorization": "Bearer x"}).status_code == 503

    guarded = client("s3cret")
    assert guarded.post("/mcp").status_code == 401
    assert (
        guarded.post("/mcp", headers={"Authorization": "Bearer nope"}).status_code
        == 401
    )
    assert guarded.post("/mcp", headers={"Authorization": "s3cret"}).status_code == 401
    assert (
        guarded.post("/mcp", headers={"Authorization": "Bearer s3cret "}).status_code
        == 401
    )
    ok = guarded.post("/mcp", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200 and ok.text == "tool result"
    # The token never appears in a refusal.
    assert "s3cret" not in guarded.post("/mcp").text


def test_vault_cli_errors_are_classified_by_the_real_wrapper(monkeypatch):
    import subprocess

    from supabase_charter import vault as vault_module

    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs["env"]
        stderr, code, stdout = seen["reply"]
        return subprocess.CompletedProcess(command, code, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(vault_module.subprocess, "run", fake_run)
    client = VaultClient(Settings(VAULT_ADDR="https://vault.example"))

    # Expired or missing token: the login hint is added by the wrapper itself.
    for detail in (
        "Error making API request.\nCode: 403. Errors:\n* permission denied\n* invalid token",
        "Error authenticating: missing client token",
    ):
        seen["reply"] = (detail, 2, "")
        with pytest.raises(AgentError, match="Export VAULT_TOKEN") as caught:
            client.kv_get("secret", "supabase")
        assert detail.splitlines()[-1] in caught.value.message
    assert seen["command"][:3] == ["vault", "kv", "get"]
    assert seen["env"]["VAULT_ADDR"] == "https://vault.example"

    # Any other failure is passed through as is, without the login hint.
    seen["reply"] = ("No value found at staging/data/ns/db", 2, "")
    with pytest.raises(AgentError, match="No value found") as caught:
        client.kv_get("staging", "ns/db")
    assert "vault login" not in caught.value.message
    assert client.kv_peek("staging", "ns/db") is None

    # `kv list -format=json` prints a bare array; the wrapper normalises it.
    seen["reply"] = ("", 0, '["dev-a-20261001", "demo-b-20261002", "sub/"]')
    assert client.kv_list("prod", "ns") == ["demo-b-20261002", "dev-a-20261001"]

    # Binary missing: install hint instead of a traceback.
    def no_binary(command, **kwargs):
        raise FileNotFoundError("vault")

    monkeypatch.setattr(vault_module.subprocess, "run", no_binary)
    with pytest.raises(AgentError, match="not installed"):
        client.kv_get("secret", "supabase")


class _StreamConn:
    """Connection fake for _stream_table: records statements and COPY traffic."""

    def __init__(self, readonly, chunks=(), rowcount=-1, set_local_fails=False):
        self.readonly = readonly
        self.chunks = list(chunks)
        self.rowcount = rowcount
        self.set_local_fails = set_local_fails
        self.statements: list[str] = []
        self.copies: list[str] = []
        self.written = bytearray()
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def cursor(self):
        conn = self

        class Copy:
            def __init__(self, query):
                conn.copies.append(query.as_string(None))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __iter__(self):
                return iter(memoryview(chunk) for chunk in conn.chunks)

            def write(self, data):
                conn.written.extend(bytes(data))

        class Cursor:
            rowcount = conn.rowcount

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, statement, params=None):
                text = (
                    statement
                    if isinstance(statement, str)
                    else statement.as_string(None)
                )
                if conn.set_local_fails and text.startswith("SET LOCAL"):
                    raise psycopg.errors.InsufficientPrivilege("permission denied")
                conn.statements.append(text)

            def copy(self, query):
                return Copy(query)

        return Cursor()


def test_stream_table_copies_listed_columns_read_only_and_counts_rows():
    from supabase_charter.copy import _stream_table

    rows = [b"1\tnew\t2026-07-01\n2\tpaid\t2026-07-02\n", b"3\tnew\t2026-07-03\n"]
    conns = {}

    def connect(dsn, readonly=False):
        kind = "source" if dsn == "postgresql://source" else "target"
        conns[kind] = _StreamConn(
            readonly, chunks=rows if kind == "source" else (), rowcount=3
        )
        return conns[kind]

    written = _stream_table(
        connect,
        "postgresql://source",
        "postgresql://target",
        "orders",
        ["id", "status", "created_at"],
        "created_at",
        "2026-07-01",
        "2026-07-31",
        None,
    )
    assert written == 3  # from COPY FROM STDIN's rowcount
    assert conns["source"].readonly is True and conns["target"].readonly is False
    assert conns["source"].copies == [
        'COPY (SELECT "id", "status", "created_at" FROM "public"."orders" '
        "WHERE \"created_at\" >= '2026-07-01' AND \"created_at\" < '2026-08-01') TO STDOUT"
    ]
    assert conns["target"].copies == [
        'COPY "public"."orders" ("id", "status", "created_at") FROM STDIN'
    ]
    assert bytes(conns["target"].written) == b"".join(rows)
    assert conns["target"].statements == [
        "SET LOCAL session_replication_role = replica"
    ]
    assert conns["source"].statements == []  # nothing is ever executed on the source
    assert conns["target"].committed is True

    # Without a usable rowcount the newline count is used; a refused SET LOCAL
    # is rolled back and the copy still proceeds.
    def connect_plain(dsn, readonly=False):
        kind = "source" if dsn == "postgresql://source" else "target"
        conns[kind] = _StreamConn(
            readonly,
            chunks=rows if kind == "source" else (),
            rowcount=-1,
            set_local_fails=kind == "target",
        )
        return conns[kind]

    written = _stream_table(
        connect_plain,
        "postgresql://source",
        "postgresql://target",
        "orders",
        ["id"],
        None,
        None,
        None,
        2,
    )
    assert written == 3
    assert conns["target"].rolled_back is True and conns["target"].committed is True
    assert (
        conns["source"].copies[0].endswith('FROM "public"."orders" LIMIT 2) TO STDOUT')
    )


def test_copy_service_opens_sources_read_only_only():
    from supabase_charter.copy import CopyService

    opened = []
    tables = ["regions"]
    columns = [("regions", "code", "text")]

    def connect(dsn, readonly=False):
        opened.append((dsn, readonly))
        return _FakeSourceConn(tables, columns, [])

    class Resolver:
        def resolve(self, source):
            return "postgresql://source"

    service = CopyService(Resolver(), connect)
    service.inspect_source_tables("s")
    service.plan_copy(
        source="s",
        tables=["regions"],
        column=None,
        start=None,
        end=None,
        limit=None,
        mode="append",
        schema_only=False,
    )
    assert opened and all(readonly for dsn, readonly in opened)
    assert {dsn for dsn, _ in opened} == {"postgresql://source"}


# -- configurable vocabulary: kinds, environments, secret path ----------------


def _service_with(tmp_path, settings, api, vault, *, marker=True):
    from supabase_charter.provision import ProvisionService

    ledger = Ledger(tmp_path / "databases.yaml", "manual")
    ledger.load()
    ledger.save()
    connect = lambda dsn, readonly=False: _MarkerConn(marker)  # noqa: E731
    return ProvisionService(settings, vault, api, ledger, connect), ledger


def _settings(**overrides):
    base = dict(
        DB_AGENT_KINDS="demo,prod",
        DB_AGENT_PERMANENT_KINDS="prod",
        DB_AGENT_VAULT_MOUNTS="dev=dev,prod=prod",
        DB_AGENT_DEFAULT_ENVIRONMENT="prod",
        DB_AGENT_ORG_PLAN="free",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_default_ttl_depends_on_the_kind():
    settings = _settings(DB_AGENT_DEFAULT_TTL_DAYS=3)
    assert settings.default_ttl_for("demo") == 3
    assert settings.default_ttl_for("prod") is None
    assert settings.environments() == ["dev", "prod"]


def test_provision_gives_permanent_kinds_no_expiry(tmp_path):
    settings = _settings(DB_AGENT_SECRET_PATH="billing")

    service, _ = _service_with(tmp_path, settings, _FakeApi([]), _FakeVault({}))
    permanent = service.provision_db(
        kind="prod", purpose="billing api", environment=None
    )
    assert permanent["expires_at"] is None
    assert permanent["secret_path"] == "billing"
    assert permanent["vault_path"].startswith("prod/billing/prod-billing-api-")

    service, _ = _service_with(tmp_path / "b", settings, _FakeApi([]), _FakeVault({}))
    disposable = service.provision_db(kind="demo", purpose="pilot", environment=None)
    assert disposable["expires_at"] is not None

    # An explicit duration still wins over the kind's default.
    service, _ = _service_with(tmp_path / "c", settings, _FakeApi([]), _FakeVault({}))
    bounded = service.provision_db(
        kind="prod", purpose="temporary", environment=None, ttl_days=2
    )
    assert bounded["expires_at"] is not None


def test_provision_requires_a_secret_path_and_keeps_it_inside_the_mount(tmp_path):
    settings = _settings()  # no DB_AGENT_SECRET_PATH configured
    api, vault = _FakeApi([]), _FakeVault({})
    service, ledger = _service_with(tmp_path, settings, api, vault)

    with pytest.raises(AgentError, match="secret_path is required"):
        service.provision_db(kind="demo", purpose="pilot", environment=None)

    with pytest.raises(ValueError, match="not a valid Vault path"):
        service.provision_db(
            kind="demo", purpose="pilot", environment=None, secret_path="../prod"
        )

    # Nothing reached Supabase, Vault, or the ledger.
    assert api.calls == [] and vault.calls == []
    assert ledger.doc.databases == []

    # A nested path is accepted and used verbatim under the mount.
    service, _ = _service_with(tmp_path / "b", settings, _FakeApi([]), _FakeVault({}))
    nested = service.provision_db(
        kind="demo", purpose="pilot", environment=None, secret_path="acme/eu"
    )
    assert nested["vault_path"].startswith("prod/acme/eu/demo-pilot-")


def test_agent_config_reports_the_vocabulary_without_secrets(tmp_path):
    from supabase_charter.app import AgentApp

    settings = _settings(DB_AGENT_SECRET_PATH="teams/acme")
    app = AgentApp(
        settings,
        vault=_FakeVault({}),
        api=_FakeApi([]),
        ledger=Ledger(tmp_path / "databases.yaml", "manual"),
        connect=lambda dsn, readonly=False: _MarkerConn(True),
    )
    config = app.describe_config()

    assert config["kinds"] == ["demo", "prod"]
    assert config["permanent_kinds"] == ["prod"]
    assert config["environments"] == ["dev", "prod"]
    assert config["default_secret_path"] == "teams/acme"
    assert config["token_vault_path"] == settings.token_vault_path
    assert config["ledger_mode"] == "manual"
