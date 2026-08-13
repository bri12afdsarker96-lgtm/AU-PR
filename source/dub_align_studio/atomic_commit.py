"""同盘多目标原子提交（v0.7.71 P0-2 假成功彻底消除）。

设计动机：分步提交（先 chunks 再 master 再 manifest）无法整体回滚——第 2 步失败时
新 chunks 已经生效但 master 仍是旧的，出现"半新半旧"状态，用户看到"✅ 完成"
但拿到的产物不一致；redub_chunk / 剪映 / Premiere 都有此病。

方案（受 POSIX/Windows `os.replace` 原生原子性启发）：
    1. 全部候选（文件或目录）预先在同盘 staging 位置准备完毕并**通过基本校验**；
    2. 按注册顺序对每个正式目标 rename 成"backup"（uuid 后缀，避免同进程并发碰撞）；
    3. 按注册顺序 `os.replace(candidate → final)` 逐个提交新内容；
    4. 提交阶段任一 `os.replace` 失败：
         · 已提交的新目标用对应 backup **覆盖回原样**（`os.replace(backup, final)`），
         · 未提交的 backup 也归位（保证零污染），
         · 未使用的候选统一清理，
         · 抛 `TransactionError` 包含事务 id + 失败步骤 + 若回滚过程再出错则**具体
           未恢复路径**（禁止"旧文件已完整保留"这类空口承诺）；
    5. **全部**提交成功后才逐个删除 backup。

关键契约：
    · 候选与正式目标**必须**在同一文件系统（`st_dev` 相同）——`os.replace` 才是原子；
    · 事务 id 用 UUID 而非 PID：同进程内并发多 worker 各自独立；
    · 不把大文件内容读进内存做备份——完全靠 rename（同盘 O(1)）；
    · 目录与文件用同一入口 `add(candidate, final)`——`os.replace` 都能处理；
    · rollback 顺序：新目标先反向删/恢复；backup 归位也反向执行——不产生依赖假设。
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path


class TransactionError(RuntimeError):
    """事务提交失败。message 里必含事务 id 与失败点；若回滚过程又出错，会追加
    "未恢复路径清单"——上层展示给用户，不允许"旧文件已保留"的一刀切声明。"""


def _same_dev(a: Path, b: Path) -> bool:
    """两条路径的父目录必须在同一文件系统（os.replace 只在同盘原子）。
    若 a 已存在，用 a 自己；否则用 a.parent。"""
    def _dev(p: Path) -> int:
        target = p if p.exists() else p.parent
        return os.stat(target).st_dev
    return _dev(a) == _dev(b)


def _rm_any(path: Path) -> None:
    """尽量删掉——无论文件还是目录，静默失败（清理阶段容错）。"""
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists() or path.is_symlink():
            path.unlink()
    except Exception:  # noqa: BLE001
        pass


def _replace_over(src: Path, dst: Path) -> None:
    """把 src rename 成 dst——若 dst 是**非空目录**，先清空再 rename。

    背景：POSIX `rename(2)` 拒绝把目录 rename 到"非空目录"（ENOTEMPTY）；Python
    `os.replace` 沿用此语义。commit 阶段 dst 已被 rename 成 backup（不存在）→ 无
    冲突；但 rollback 阶段 dst 已被本 txn 更早的 commit 覆盖为**新**内容，直接
    `os.replace` 会 ENOTEMPTY。这里先删除新内容再 rename backup 归位——非严格
    "两步都是原子"，但对于 rollback 已经是从错误状态尽力还原，这是可接受折衷。

    对文件 dst：POSIX rename 允许覆盖同名文件，`os.replace` 直接用即可。
    """
    src = str(src); dst_p = Path(dst); dst = str(dst)
    if dst_p.is_dir() and not dst_p.is_symlink():
        # 目录 → 目录：先清空 dst
        shutil.rmtree(dst, ignore_errors=False)
    os.replace(src, dst)


class AtomicMultiCommit:
    """同盘多目标事务提交器。用法：

        txn = AtomicMultiCommit()
        txn.add(cand_master, real_master)
        txn.add(cand_meta,   real_meta)
        txn.add(cand_chunks, real_chunks_dir)
        txn.commit()   # 要么全部成功，要么全部回滚

    add 与 commit 之间可以做任意其它准备；一旦调用 commit()：
    - 若任一 `os.replace` 失败，已提交的新目标被 backup 覆盖回原样，未提交的 backup
      归位；成功回滚 → 抛 TransactionError（含事务 id + 出错步骤）；
    - 若回滚过程本身失败 → 抛 TransactionError 并列出**未恢复的实际路径**，
      让用户明白哪些文件需要手动救援（禁止空口保证）。

    所有候选与正式目标**必须**在同一文件系统。add() 会立即校验并在不满足时抛错。
    """

    def __init__(self, txn_id: str | None = None):
        self.txn_id = txn_id or uuid.uuid4().hex[:12]
        # 注册顺序即提交顺序
        self._pairs: list[tuple[Path, Path]] = []
        # 回滚用状态
        self._backups: list[tuple[Path, Path | None]] = []   # (final, backup_or_None)
        self._committed_indexes: list[int] = []              # commit 成功的下标

    # ---------------- 注册 ----------------
    def add(self, candidate: Path, final: Path) -> None:
        """注册一个 (candidate → final) 提交对。二者必须同盘。"""
        candidate = Path(candidate)
        final = Path(final)
        if not candidate.exists():
            raise TransactionError(
                f"[txn {self.txn_id}] 候选路径不存在，无法提交：{candidate}"
            )
        final.parent.mkdir(parents=True, exist_ok=True)
        if not _same_dev(candidate, final):
            raise TransactionError(
                f"[txn {self.txn_id}] 候选与正式目标不在同一文件系统："
                f"{candidate} vs {final}——os.replace 无法原子。"
            )
        self._pairs.append((candidate, final))

    # ---------------- 提交 ----------------
    def commit(self) -> None:
        """执行事务提交；失败时全量回滚并抛 TransactionError。"""
        if not self._pairs:
            return
        # 阶段 A：把每个 final（若存在）rename 成独立 backup。若 backup rename 失败，
        # 已完成的 backup 归位，抛错（此刻没有 final 已被 candidate 覆盖，回滚成本低）。
        for final, _ in [(f, c) for (c, f) in self._pairs]:
            backup: Path | None = None
            if final.exists():
                backup = final.with_name(
                    f"{final.name}.bak_{self.txn_id}"
                )
                try:
                    os.replace(str(final), str(backup))
                except Exception as exc:
                    # 归位已 backup 的
                    unrecovered = self._restore_backups_reverse()
                    raise self._error(
                        step=f"backup({final})", exc=exc, unrecovered=unrecovered)
            self._backups.append((final, backup))

        # 阶段 B：按顺序 os.replace(candidate → final)。任一步失败即全量回滚。
        for i, (candidate, final) in enumerate(self._pairs):
            try:
                os.replace(str(candidate), str(final))
                self._committed_indexes.append(i)
            except Exception as exc:
                unrecovered = self._rollback_after_commit_failure()
                raise self._error(step=f"commit#{i+1}({final})", exc=exc,
                                   unrecovered=unrecovered)

        # 阶段 C：全部成功——逐个删 backup（失败静默，最多留下 .bak_<id> 供人工清理）
        for _final, backup in self._backups:
            if backup is not None:
                _rm_any(backup)

    # ---------------- 回滚 ----------------
    def _rollback_after_commit_failure(self) -> list[str]:
        """提交阶段某一步失败：
        1) 已提交的新目标 —— 用对应 backup 覆盖回原样；
        2) 未提交的 backup —— 归位到 final；
        3) 剩余未使用的候选 —— 删除。
        返回未成功恢复的路径列表（若全部恢复则为空）。"""
        unrecovered: list[str] = []
        # 逆序恢复：committed_indexes 反着来
        for i in reversed(self._committed_indexes):
            final, backup = self._backups[i]
            if backup is None:
                # 原本 final 不存在——把新写入的 final 删掉即可
                try:
                    _rm_any(final)
                except Exception:  # noqa: BLE001
                    unrecovered.append(str(final))
                continue
            try:
                _replace_over(backup, final)
            except Exception:  # noqa: BLE001
                unrecovered.append(str(final))
        # 未提交的 backup 归位（下标 > 最后 committed 的）
        last_committed = self._committed_indexes[-1] if self._committed_indexes else -1
        for i in range(len(self._backups) - 1, last_committed, -1):
            final, backup = self._backups[i]
            if backup is None:
                continue
            try:
                _replace_over(backup, final)
            except Exception:  # noqa: BLE001
                unrecovered.append(str(final))
        # 剩余未使用的候选 —— 清理
        for _cand, _ in self._pairs[len(self._committed_indexes):]:
            _rm_any(_cand)
        return unrecovered

    def _restore_backups_reverse(self) -> list[str]:
        """backup 阶段某一步失败：把已做的 backup 全部归位。"""
        unrecovered: list[str] = []
        for final, backup in reversed(self._backups):
            if backup is None:
                continue
            try:
                _replace_over(backup, final)
            except Exception:  # noqa: BLE001
                unrecovered.append(str(final))
        return unrecovered

    def _error(self, step: str, exc: Exception, unrecovered: list[str]) -> TransactionError:
        msg = f"[txn {self.txn_id}] 事务提交失败于 {step}：{exc}"
        if unrecovered:
            msg += (
                "；⚠ 回滚过程中以下路径**未成功恢复**（请手动检查文件系统）："
                + "、".join(unrecovered)
            )
        else:
            msg += "；已成功回滚全部旧目标到原字节。"
        return TransactionError(msg)
