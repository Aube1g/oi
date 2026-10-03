#!/usr/bin/env python3
"""Tests for `xli snapshot` and the snapshot.* RPC methods.

xli.core.time_machine implemented snapshots, rollback and diffs and nothing
called it. It is now reachable from the CLI and over the kernel protocol.

Snapshotting is deliberately not automatic: taking one on every file edit would
spend the user's disk without them asking. These tests pin the explicit
interface, including the argument handling — the first version of it took
`path nargs="*"`, which swallowed a snapshot id into the path list and made the
obvious `snapshot diff <id> <file>` spelling fail.
"""

import asyncio
import json

import pytest

from xli.cli import EXIT_FAILED, EXIT_OK, EXIT_USAGE, build_parser, main
from xli.core.time_machine import TimeMachine, get_time_machine
from xli.kernel.protocol import Request, encode_line


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """An isolated xli home, so snapshots do not accumulate in the real one."""
    monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path / ".xli"))
    TimeMachine._instance = None
    target = tmp_path / "a.txt"
    target.write_text("original\n", encoding="utf-8")
    yield tmp_path, target
    TimeMachine._instance = None


@pytest.fixture
def kernel(workdir):
    from xli.kernel.methods import build_kernel

    tmp_path, _ = workdir
    return build_kernel(project_root=tmp_path)


def rpc(kernel, method, params=None):
    out = asyncio.run(
        kernel.feed_line(encode_line(Request(method=method, id=1, params=params or {})))
    )
    return out[0]


# ----------------------------------------------------------------- time machine
class TestTimeMachine:
    def test_snapshot_then_rollback_restores_the_file(self, workdir):
        _, target = workdir
        machine = get_time_machine()
        snapshot_id = machine.snapshot([str(target)], "before")

        target.write_text("changed\n", encoding="utf-8")
        assert target.read_text(encoding="utf-8") == "changed\n"

        assert machine.rollback(snapshot_id) is True
        assert target.read_text(encoding="utf-8") == "original\n"

    def test_diff_shows_the_change(self, workdir):
        _, target = workdir
        machine = get_time_machine()
        snapshot_id = machine.snapshot([str(target)], "before")
        target.write_text("changed\n", encoding="utf-8")

        diff = machine.diff_snapshot(snapshot_id, str(target))
        assert "-original" in diff
        assert "+changed" in diff

    def test_list_reports_the_snapshot(self, workdir):
        _, target = workdir
        machine = get_time_machine()
        snapshot_id = machine.snapshot([str(target)], "labelled")

        listed = machine.list_snapshots()
        assert [s["id"] for s in listed] == [snapshot_id]
        assert listed[0]["label"] == "labelled"

    def test_delete_removes_it(self, workdir):
        _, target = workdir
        machine = get_time_machine()
        snapshot_id = machine.snapshot([str(target)], "doomed")

        assert machine.delete_snapshot(snapshot_id) is True
        assert machine.list_snapshots() == []
        assert machine.delete_snapshot(snapshot_id) is False

    def test_rollback_of_an_unknown_id_is_false(self, workdir):
        assert get_time_machine().rollback("no-such-id") is False


# ------------------------------------------------------------------------ cli
class TestSnapshotCli:
    def _run(self, *argv, capsys=None):
        """Run the subcommand and return (exit code, captured output).

        capsys.readouterr() drains the buffer, so this is the only place it is
        called; tests must read from the returned object rather than calling it
        again, which would yield an empty string.
        """
        code = main(["snapshot", *argv])
        return code, (capsys.readouterr() if capsys else None)

    def test_create_needs_a_path(self, workdir, capsys):
        code, out = self._run("create", capsys=capsys)
        assert code == EXIT_USAGE
        assert "usage:" in out.err

    def test_create_rejects_a_missing_file(self, workdir, capsys):
        code, out = self._run("create", "/no/such/file", capsys=capsys)
        assert code == EXIT_USAGE
        assert "not found" in out.err

    def test_create_then_list(self, workdir, capsys):
        _, target = workdir
        assert self._run("create", str(target), "--label", "cli", capsys=capsys)[0] == EXIT_OK

        code, out = self._run("list", "--json", capsys=capsys)
        assert code == EXIT_OK
        listed = json.loads(out.out)
        assert len(listed) == 1
        assert listed[0]["label"] == "cli"

    def test_list_with_no_snapshots(self, workdir, capsys):
        code, out = self._run("list", capsys=capsys)
        assert code == EXIT_OK
        assert "no snapshots" in out.out

    def test_diff_takes_id_then_path_positionally(self, workdir, capsys):
        """The spelling the first implementation broke."""
        _, target = workdir
        self._run("create", str(target), capsys=capsys)
        snapshot_id = json.loads(
            self._run("list", "--json", capsys=capsys)[1].out
        )[0]["id"]

        target.write_text("changed\n", encoding="utf-8")
        code, out = self._run("diff", snapshot_id, str(target), capsys=capsys)
        assert code == EXIT_OK
        assert "+changed" in out.out

    def test_rollback_takes_the_id_positionally(self, workdir, capsys):
        _, target = workdir
        self._run("create", str(target), capsys=capsys)
        snapshot_id = json.loads(self._run("list", "--json", capsys=capsys)[1].out)[0]["id"]

        target.write_text("changed\n", encoding="utf-8")
        assert self._run("rollback", snapshot_id, capsys=capsys)[0] == EXIT_OK
        assert target.read_text(encoding="utf-8") == "original\n"

    def test_rollback_also_accepts_the_flag(self, workdir, capsys):
        _, target = workdir
        self._run("create", str(target), capsys=capsys)
        snapshot_id = json.loads(self._run("list", "--json", capsys=capsys)[1].out)[0]["id"]

        target.write_text("changed\n", encoding="utf-8")
        assert self._run("rollback", "--id", snapshot_id, capsys=capsys)[0] == EXIT_OK
        assert target.read_text(encoding="utf-8") == "original\n"

    def test_rollback_of_an_unknown_id_fails(self, workdir, capsys):
        code, out = self._run("rollback", "no-such-id", capsys=capsys)
        assert code == EXIT_FAILED
        assert "no such snapshot" in out.err

    def test_rollback_without_an_id_is_usage_error(self, workdir, capsys):
        code, out = self._run("rollback", capsys=capsys)
        assert code == EXIT_USAGE
        assert "usage:" in out.err

    def test_delete(self, workdir, capsys):
        _, target = workdir
        self._run("create", str(target), capsys=capsys)
        snapshot_id = json.loads(self._run("list", "--json", capsys=capsys)[1].out)[0]["id"]

        assert self._run("delete", snapshot_id, capsys=capsys)[0] == EXIT_OK
        _, out = self._run("list", "--json", capsys=capsys)
        assert json.loads(out.out) == []

    def test_diff_without_a_path_is_usage_error(self, workdir, capsys):
        _, target = workdir
        self._run("create", str(target), capsys=capsys)
        snapshot_id = json.loads(self._run("list", "--json", capsys=capsys)[1].out)[0]["id"]

        code, out = self._run("diff", snapshot_id, capsys=capsys)
        assert code == EXIT_USAGE
        assert "usage:" in out.err

    def test_create_emits_json(self, workdir, capsys):
        _, target = workdir
        code, out = self._run("create", str(target), "--json", capsys=capsys)
        assert code == EXIT_OK
        payload = json.loads(out.out)
        assert payload["files"] == 1
        assert payload["id"]

    def test_the_subcommand_is_registered(self):
        import argparse

        parser = build_parser()
        actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        assert any("snapshot" in a.choices for a in actions)

    def test_snapshots_land_in_the_configured_directory(self, workdir):
        """Not in the real ~/.xli — see xli/paths.py."""
        tmp_path, target = workdir
        get_time_machine().snapshot([str(target)], "where")
        assert list((tmp_path / ".xli" / "snapshots").iterdir())


# ------------------------------------------------------------------------ rpc
class TestSnapshotRpc:
    def test_create_and_rollback(self, kernel, workdir):
        _, target = workdir
        created = rpc(kernel, "snapshot.create", {"paths": [str(target)], "label": "rpc"})
        assert created.error is None
        assert created.result["files"] == 1

        target.write_text("changed\n", encoding="utf-8")
        rolled = rpc(kernel, "snapshot.rollback", {"id": created.result["id"]})
        assert rolled.error is None
        assert target.read_text(encoding="utf-8") == "original\n"

    def test_list(self, kernel, workdir):
        _, target = workdir
        rpc(kernel, "snapshot.create", {"paths": [str(target)]})
        result = rpc(kernel, "snapshot.list")
        assert len(result.result["snapshots"]) == 1

    def test_diff(self, kernel, workdir):
        _, target = workdir
        created = rpc(kernel, "snapshot.create", {"paths": [str(target)]})
        target.write_text("changed\n", encoding="utf-8")

        result = rpc(kernel, "snapshot.diff", {"id": created.result["id"], "path": str(target)})
        assert result.error is None
        assert "+changed" in result.result["diff"]

    def test_delete(self, kernel, workdir):
        _, target = workdir
        created = rpc(kernel, "snapshot.create", {"paths": [str(target)]})
        assert rpc(kernel, "snapshot.delete", {"id": created.result["id"]}).error is None
        assert rpc(kernel, "snapshot.list").result["snapshots"] == []

    def test_a_single_path_string_is_accepted(self, kernel, workdir):
        """A JSON-RPC client may send one path rather than a list."""
        _, target = workdir
        result = rpc(kernel, "snapshot.create", {"paths": str(target)})
        assert result.error is None
        assert result.result["files"] == 1

    def test_empty_paths_is_rejected(self, kernel):
        error = rpc(kernel, "snapshot.create", {"paths": []}).error
        assert error is not None
        assert "must not be empty" in error["message"]

    def test_rollback_of_an_unknown_id_is_an_error(self, kernel):
        error = rpc(kernel, "snapshot.rollback", {"id": "bogus"}).error
        assert error is not None
        assert "no such snapshot" in error["message"]

    def test_missing_required_params(self, kernel):
        assert rpc(kernel, "snapshot.create", {}).error is not None
        assert rpc(kernel, "snapshot.rollback", {}).error is not None
        assert rpc(kernel, "snapshot.diff", {"id": "x"}).error is not None

    def test_all_five_methods_are_listed(self, kernel):
        names = {m["name"] for m in rpc(kernel, "rpc.methods").result["methods"]}
        assert {
            "snapshot.create",
            "snapshot.delete",
            "snapshot.diff",
            "snapshot.list",
            "snapshot.rollback",
        } <= names
