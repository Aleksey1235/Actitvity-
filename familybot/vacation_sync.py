from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class VacationRolePartition:
    """Pure reconciliation view between the Discord vacation role and bot roster.

    The Discord role can contain people that are not yet present in Family Activity's
    active roster. Those people must never silently enter analytics because the bot
    does not know their Static ID / membership history. They are reported separately
    so staff can import or add them intentionally.
    """

    role_holder_ids: frozenset[int]
    active_roster_ids: frozenset[int]

    @property
    def linked_ids(self) -> frozenset[int]:
        return self.role_holder_ids & self.active_roster_ids

    @property
    def unlinked_role_ids(self) -> frozenset[int]:
        return self.role_holder_ids - self.active_roster_ids


def partition_vacation_role(
    role_holder_ids: Iterable[int],
    active_roster_ids: Iterable[int],
) -> VacationRolePartition:
    return VacationRolePartition(
        role_holder_ids=frozenset(int(x) for x in role_holder_ids),
        active_roster_ids=frozenset(int(x) for x in active_roster_ids),
    )


@dataclass(slots=True)
class VacationSyncResult:
    opened: int = 0
    closed: int = 0
    missing_discord_profiles: int = 0
    discord_role_holders: int = 0
    linked_role_holders: int = 0
    unlinked_role_member_ids: tuple[int, ...] = ()

    @property
    def unlinked_role_holders(self) -> int:
        return len(self.unlinked_role_member_ids)
