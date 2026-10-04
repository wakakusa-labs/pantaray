from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..action_session_temp_paths import resolve_local_runtime_storage_base

SandboxProfile = Literal[
    "agent_generated_python",
    "workspace_process_exec",
]

MemoryTierBytes = Literal[
    8_589_934_592,
    17_179_869_184,
    34_359_738_368,
    68_719_476_736,
]

ONE_MEBIBYTE = 1_048_576
OUTPUT_FLOOR_BYTES = ONE_MEBIBYTE
OUTPUT_CAP_BYTES = 16 * ONE_MEBIBYTE
OUTPUT_MEMORY_RATIO = 0.001
CHILD_COUNT_FLOOR = 8
CHILD_COUNT_MULTIPLIER = 4
CHILD_COUNT_CAP = 128
TEMP_DISK_RATIO = 0.05


@dataclass(frozen=True, slots=True)
class BrokerLocalBudget:
    child_count_limit: int
    open_file_lease_limit: int


@dataclass(frozen=True, slots=True)
class SandboxLaunchBudget:
    timeout_ms: int
    stdout_max_bytes: int
    stderr_max_bytes: int
    temp_storage_limit_bytes: int


@dataclass(frozen=True, slots=True)
class RuntimeBudgetResolution:
    broker_local: BrokerLocalBudget
    sandbox_launch: SandboxLaunchBudget


@dataclass(frozen=True, slots=True)
class TempStorageBounds:
    minimum_bytes: int
    cap_bytes: int


PROFILE_TIMEOUT_MS: dict[SandboxProfile, int] = {
    "agent_generated_python": 30_000,
    "workspace_process_exec": 600_000,
}

PROFILE_TEMP_STORAGE_BOUNDS: dict[SandboxProfile, TempStorageBounds] = {
    "agent_generated_python": TempStorageBounds(
        minimum_bytes=67_108_864,
        cap_bytes=134_217_728,
    ),
    "workspace_process_exec": TempStorageBounds(
        minimum_bytes=268_435_456,
        cap_bytes=1_073_741_824,
    ),
}


def _clamp(value: int, *, lower: int, upper: int) -> int:
    return max(lower, min(value, upper))


def _read_host_memory_bytes() -> int:
    page_size_names = ("SC_PAGE_SIZE", "SC_PHYS_PAGES")
    if not all(hasattr(os, "sysconf") for _ in page_size_names):
        raise RuntimeError("host memory probe is unavailable on this platform")
    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    page_count = int(os.sysconf("SC_PHYS_PAGES"))
    total_bytes = page_size * page_count
    if total_bytes <= 0:
        raise RuntimeError("host memory probe returned a non-positive value")
    return total_bytes


def resolve_memory_tier_bytes() -> MemoryTierBytes:
    total_bytes = _read_host_memory_bytes()
    if total_bytes <= 8_589_934_592:
        return 8_589_934_592
    if total_bytes <= 17_179_869_184:
        return 17_179_869_184
    if total_bytes <= 34_359_738_368:
        return 34_359_738_368
    return 68_719_476_736


def _read_cpu_count() -> int:
    cpu_count = os.cpu_count()
    if cpu_count is None or cpu_count <= 0:
        raise RuntimeError("cpu_count probe returned no usable value")
    return cpu_count


def _read_free_disk_bytes(root: Path) -> int:
    usage = shutil.disk_usage(root)
    if usage.free <= 0:
        raise RuntimeError("disk usage probe returned no free space")
    return usage.free


def resolve_runtime_budget(
    *,
    sandbox_profile: SandboxProfile,
    db_path: Path,
) -> RuntimeBudgetResolution:
    memory_tier_bytes = resolve_memory_tier_bytes()
    cpu_count = _read_cpu_count()
    # Command temp dirs live in private app storage (create_private_temp_dir).
    free_disk_bytes = _read_free_disk_bytes(
        resolve_local_runtime_storage_base(db_path=db_path)
    )
    output_budget = _clamp(
        int(memory_tier_bytes * OUTPUT_MEMORY_RATIO),
        lower=OUTPUT_FLOOR_BYTES,
        upper=OUTPUT_CAP_BYTES,
    )
    temp_bounds = PROFILE_TEMP_STORAGE_BOUNDS[sandbox_profile]
    temp_storage_budget = _clamp(
        int(free_disk_bytes * TEMP_DISK_RATIO),
        lower=temp_bounds.minimum_bytes,
        upper=temp_bounds.cap_bytes,
    )
    child_count_limit = min(
        max(CHILD_COUNT_FLOOR, cpu_count * CHILD_COUNT_MULTIPLIER),
        CHILD_COUNT_CAP,
    )
    return RuntimeBudgetResolution(
        broker_local=BrokerLocalBudget(
            child_count_limit=child_count_limit,
            open_file_lease_limit=child_count_limit,
        ),
        sandbox_launch=SandboxLaunchBudget(
            timeout_ms=PROFILE_TIMEOUT_MS[sandbox_profile],
            stdout_max_bytes=output_budget,
            stderr_max_bytes=output_budget,
            temp_storage_limit_bytes=temp_storage_budget,
        ),
    )
