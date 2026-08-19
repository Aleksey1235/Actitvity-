from __future__ import annotations

from dataclasses import dataclass


GROUP_ACADEMY = "academy"
GROUP_MAIN = "main"
GROUP_UNCLASSIFIED = "unclassified"
GROUP_CONFLICT = "conflict"

GROUP_LABELS = {
    GROUP_ACADEMY: "🎓 Academy",
    GROUP_MAIN: "🏠 Основной состав",
    GROUP_UNCLASSIFIED: "⚪ Не определён",
    GROUP_CONFLICT: "⚠️ Конфликт ролей",
}


def classify_group_state(*, has_academy: bool, has_main: bool) -> str:
    if has_academy and has_main:
        return GROUP_CONFLICT
    if has_academy:
        return GROUP_ACADEMY
    if has_main:
        return GROUP_MAIN
    return GROUP_UNCLASSIFIED


@dataclass(slots=True)
class GroupSyncResult:
    changed: int = 0
    missing_discord_profiles: int = 0
    academy_members: int = 0
    main_members: int = 0
    unclassified_members: int = 0
    conflict_members: int = 0
    academy_role_holders: int = 0
    main_role_holders: int = 0
    unlinked_academy_ids: tuple[int, ...] = ()
    unlinked_main_ids: tuple[int, ...] = ()

    @property
    def linked_members(self) -> int:
        return (
            self.academy_members
            + self.main_members
            + self.unclassified_members
            + self.conflict_members
        )
