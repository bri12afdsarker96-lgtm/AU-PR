"""atomic_commit 事务提交器专项测试（v0.7.71 P0-2）。

覆盖：
    · 全部 commit 成功：新内容生效、backup 删干净；
    · 阶段 A（backup rename）失败：已 backup 归位，final 原字节保留；
    · 阶段 B 第 2 / 第 3 次 os.replace 失败：committed 新目标被 backup 覆盖回原样，
      未提交的 backup 归位，剩余候选清理；
    · 回滚过程再出错：TransactionError 消息里必含具体未恢复路径；
    · 事务 id 使用 UUID（同进程可并发多个事务）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dub_align_studio.atomic_commit import AtomicMultiCommit, TransactionError


class HappyPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="txn_ok_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_files_and_dirs_commit_and_backups_cleaned(self):
        # 三个目标：文件 A + 文件 B + 目录 C
        real_a = self.tmp / "a.txt"; real_a.write_text("OLD-A")
        real_b = self.tmp / "b.txt"; real_b.write_text("OLD-B")
        real_c = self.tmp / "cdir"; real_c.mkdir(); (real_c / "x").write_text("OLD-X")

        cand_a = self.tmp / "a.txt.new"; cand_a.write_text("NEW-A")
        cand_b = self.tmp / "b.txt.new"; cand_b.write_text("NEW-B")
        cand_c = self.tmp / "cdir.new"; cand_c.mkdir(); (cand_c / "x").write_text("NEW-X")

        txn = AtomicMultiCommit()
        txn.add(cand_a, real_a)
        txn.add(cand_b, real_b)
        txn.add(cand_c, real_c)
        txn.commit()

        self.assertEqual(real_a.read_text(), "NEW-A")
        self.assertEqual(real_b.read_text(), "NEW-B")
        self.assertEqual((real_c / "x").read_text(), "NEW-X")
        # 无 backup 残留
        residues = [p.name for p in self.tmp.iterdir() if ".bak_" in p.name]
        self.assertEqual(residues, [], f"backup 残留：{residues}")

    def test_txn_id_is_uuid_not_pid(self):
        t1 = AtomicMultiCommit(); t2 = AtomicMultiCommit()
        self.assertNotEqual(t1.txn_id, t2.txn_id)
        # UUID hex 前 12 位；确保是 hex 且不含 os.getpid()
        self.assertRegex(t1.txn_id, r"^[0-9a-f]{12}$")
        self.assertNotIn(str(os.getpid()), t1.txn_id)

    def test_same_fs_check_rejects_cross_device(self):
        real = self.tmp / "x.txt"; real.write_text("OLD")
        cand = self.tmp / "x.new"; cand.write_text("NEW")
        with mock.patch("dub_align_studio.atomic_commit._same_dev", return_value=False):
            txn = AtomicMultiCommit()
            with self.assertRaises(TransactionError) as ctx:
                txn.add(cand, real)
            self.assertIn("不在同一文件系统", str(ctx.exception))


class CommitFailureRollbackTests(unittest.TestCase):
    """故障注入：第 N 次 os.replace 失败时全部旧字节恢复。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="txn_fail_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, count: int = 3) -> list[tuple[Path, Path, bytes, bytes]]:
        """造 count 个 (real, cand, old_bytes, new_bytes)。混合文件/目录目标。"""
        items = []
        for i in range(count):
            real = self.tmp / f"target{i}.txt"
            cand = self.tmp / f"target{i}.new"
            old = f"OLD-{i}".encode()
            new = f"NEW-{i}".encode()
            real.write_bytes(old)
            cand.write_bytes(new)
            items.append((real, cand, old, new))
        return items

    def test_second_commit_failure_rolls_back_first(self):
        """三个目标；第 2 次 os.replace 失败——第 1 个新目标被 backup 覆盖回旧字节，
        第 2、3 个 backup 归位，候选清理。"""
        items = self._seed(3)
        txn = AtomicMultiCommit()
        for real, cand, _, _ in items:
            txn.add(cand, real)

        real_orig = os.replace
        call_count = {"n": 0, "boom_after": 0}
        # 阶段 A 会先做若干次 replace（backup），我们只想在阶段 B 的第 2 次爆
        # 阶段 A 有 3 次 replace（每个 final 一次 backup）→ 第 4 次是 commit#1，第 5 次是 commit#2 → 让第 5 次爆
        def _flaky(src, dst):
            call_count["n"] += 1
            if call_count["n"] == 5:
                raise OSError("simulated commit#2 failure")
            return real_orig(src, dst)

        with mock.patch("dub_align_studio.atomic_commit.os.replace", side_effect=_flaky):
            with self.assertRaises(TransactionError) as ctx:
                txn.commit()
        # 消息含事务 id 与 "commit#2"
        err = str(ctx.exception)
        self.assertIn(f"[txn {txn.txn_id}]", err)
        self.assertIn("commit#2", err)
        self.assertIn("已成功回滚全部旧目标到原字节", err)
        # 每个 real 都是旧字节
        for real, _cand, old, _new in items:
            self.assertEqual(real.read_bytes(), old, f"{real} 未恢复")
        # 无 backup 残留、无 candidate 残留
        leftovers = [p.name for p in self.tmp.iterdir()
                     if ".bak_" in p.name or p.name.endswith(".new")]
        self.assertEqual(leftovers, [], f"残留：{leftovers}")

    def test_third_commit_failure_rolls_back_first_and_second(self):
        """五个目标；第 3 次 os.replace 失败——前两个新目标必须被 backup 覆盖回旧字节。"""
        items = self._seed(5)
        txn = AtomicMultiCommit()
        for real, cand, _, _ in items:
            txn.add(cand, real)

        real_orig = os.replace
        call_count = {"n": 0}
        # 阶段 A：5 次 backup；阶段 B commit#1 = 6, commit#2 = 7, commit#3 = 8（爆）
        def _flaky(src, dst):
            call_count["n"] += 1
            if call_count["n"] == 8:
                raise OSError("simulated commit#3 failure")
            return real_orig(src, dst)

        with mock.patch("dub_align_studio.atomic_commit.os.replace", side_effect=_flaky):
            with self.assertRaises(TransactionError) as ctx:
                txn.commit()
        err = str(ctx.exception)
        self.assertIn("commit#3", err)
        # 五个 real 全部恢复到旧字节
        for real, _cand, old, _new in items:
            self.assertEqual(real.read_bytes(), old, f"{real} 未恢复")

    def test_rollback_failure_reports_unrecovered_paths(self):
        """回滚阶段自身再出错时，必须列出未恢复的实际路径——禁止空口保证。"""
        items = self._seed(3)
        txn = AtomicMultiCommit()
        for real, cand, _, _ in items:
            txn.add(cand, real)

        real_orig = os.replace
        # 计数：阶段 A 3 次；commit#1 = 4；commit#2 = 5 让它爆；
        # 回滚 committed 反向：restore committed#1 是第 6 次调用——也让它爆
        call_count = {"n": 0}
        def _flaky(src, dst):
            call_count["n"] += 1
            if call_count["n"] in (5, 6):
                raise OSError(f"simulated fail at call {call_count['n']}")
            return real_orig(src, dst)

        with mock.patch("dub_align_studio.atomic_commit.os.replace", side_effect=_flaky):
            with self.assertRaises(TransactionError) as ctx:
                txn.commit()
        err = str(ctx.exception)
        self.assertIn("未成功恢复", err)
        # 具体路径出现在消息里（target0.txt 是 committed#1 恢复失败的那个）
        self.assertIn("target0.txt", err)

    def test_backup_stage_failure_restores_prior_backups(self):
        """阶段 A（backup rename）中途失败：已完成的 backup 全部归位，最终旧字节保留。"""
        items = self._seed(4)
        txn = AtomicMultiCommit()
        for real, cand, _, _ in items:
            txn.add(cand, real)

        real_orig = os.replace
        call_count = {"n": 0}
        # 阶段 A：4 次 backup；让第 3 次爆
        def _flaky(src, dst):
            call_count["n"] += 1
            if call_count["n"] == 3:
                raise OSError("simulated backup#3 failure")
            return real_orig(src, dst)

        with mock.patch("dub_align_studio.atomic_commit.os.replace", side_effect=_flaky):
            with self.assertRaises(TransactionError) as ctx:
                txn.commit()
        self.assertIn("backup(", str(ctx.exception))
        # 全部 real 原字节保留
        for real, _cand, old, _new in items:
            self.assertEqual(real.read_bytes(), old)


if __name__ == "__main__":
    unittest.main()
