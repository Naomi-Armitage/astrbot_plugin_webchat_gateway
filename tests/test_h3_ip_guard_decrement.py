"""Regression: H3 — IpGuard.reset must only DECREMENT on success.

The original `IpGuard.reset` called `storage.reset_ip_failures(ip)`,
which DELETE-d the failure row entirely. Any successful auth from the
same IP — including by an attacker who happens to hold one valid token
— wiped the brute-force counter, so they could probe other tokens at
unlimited rate forever (alternate "1 legit auth → 9 random probes" in
a tight loop, never tripping max_fails).

The fix flips the semantic to "decrement by one credit": legit users
still recover from typos (typo once + correct attempt → counter at 0),
but an attacker on the same IP gets exactly one probe credit per
legitimate auth, which means they need a *separate working bearer*
per probe — defeating the brute-force amplification.

These tests pin both the attack-blocking property and the
typo-recovery property at the storage primitive + IpGuard surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.asyncio
@pytest.mark.usefixtures("tmp_data_dir")
class TestDecrementIpFailure:
    async def _seed(self, tmp_path: Path):
        from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
            SqliteStorage,
        )

        storage = SqliteStorage(str(tmp_path / "guard.db"))
        await storage.initialize()
        return storage

    async def test_decrement_returns_new_count(self, tmp_path: Path):
        storage = await self._seed(tmp_path)
        try:
            # Three failures → counter at 3.
            for _ in range(3):
                await storage.record_ip_failure(
                    "1.2.3.4", now=1_000_000, max_fails=10, block_seconds=60
                )
            assert (await storage.decrement_ip_failure("1.2.3.4")) == 2
            assert (await storage.decrement_ip_failure("1.2.3.4")) == 1
            assert (await storage.decrement_ip_failure("1.2.3.4")) == 0
            # Past zero is a no-op (row already deleted).
            assert (await storage.decrement_ip_failure("1.2.3.4")) == 0
        finally:
            await storage.close()

    async def test_decrement_no_row_is_noop(self, tmp_path: Path):
        storage = await self._seed(tmp_path)
        try:
            assert (await storage.decrement_ip_failure("9.9.9.9")) == 0
        finally:
            await storage.close()

    async def test_attacker_cannot_amplify_via_legit_success(
        self, tmp_path: Path
    ):
        """Attack model: attacker holds one valid bearer on IP X. They
        want to probe other tokens at unlimited rate.

        Pre-fix: legit auth → reset wipes counter → next 10 invalid
        probes don't trip the block. Loop forever.

        Post-fix: legit auth → counter -=1. They must trade one valid
        auth for one probe. To get 10 probes they need 10 distinct
        legitimate auths, which contradicts the attacker model.
        """
        from astrbot_plugin_webchat_gateway.core.ip_guard import IpGuard

        storage = await self._seed(tmp_path)
        try:
            guard = IpGuard(storage, max_fails=10, block_seconds=60)

            # Simulate 9 invalid bearer probes — just under the block
            # threshold.
            for _ in range(9):
                await guard.record_failure("1.2.3.4")
            blocked, _ = await guard.is_blocked("1.2.3.4")
            assert not blocked

            # Attacker uses their valid token successfully (legit auth
            # path calls ip_guard.reset). Pre-fix this would zero the
            # counter; post-fix it decrements to 8.
            await guard.reset("1.2.3.4")

            # Two MORE invalid probes — the 10th and 11th failures of
            # the day. Post-fix: counter goes 8 → 9 → 10, the 10th
            # crosses the threshold and the IP is now blocked.
            await guard.record_failure("1.2.3.4")
            await guard.record_failure("1.2.3.4")
            blocked, _ = await guard.is_blocked("1.2.3.4")
            assert blocked, (
                "After one legit auth + 2 more probes (10 fails total) the "
                "IP must be blocked. Pre-fix the reset-to-0 zeroed out the "
                "counter, letting the attacker probe at full rate forever."
            )
        finally:
            await storage.close()

    async def test_legit_user_typo_recovery_still_works(self, tmp_path: Path):
        """One typo + one correct attempt → counter back to 0.

        We don't want to punish a fat-fingered legitimate user. The
        decrement-by-one semantic keeps single-typo recovery clean.
        """
        from astrbot_plugin_webchat_gateway.core.ip_guard import IpGuard

        storage = await self._seed(tmp_path)
        try:
            guard = IpGuard(storage, max_fails=5, block_seconds=60)
            # Typo once.
            await guard.record_failure("5.5.5.5")
            # Correct attempt — decrements counter to 0.
            await guard.reset("5.5.5.5")
            # Verify row is gone (counter == 0 ⇒ DELETE) so further
            # failures start from 1 again, not from 0+inherited state.
            blocked_state = await storage.is_ip_blocked("5.5.5.5", now=2_000_000)
            assert blocked_state == (False, 0)
            # Next failure should land on count=1 (fresh row), not
            # count=2 (sticky).
            count = await storage.record_ip_failure(
                "5.5.5.5", now=2_000_000, max_fails=5, block_seconds=60
            )
            assert count == 1
        finally:
            await storage.close()
