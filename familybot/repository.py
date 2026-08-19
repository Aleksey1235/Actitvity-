from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import string
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Iterable

from .db import Database
from .timeutil import iso, parse_iso, utcnow


class DomainError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class GuildSettings:
    guild_id: int
    family_role_id: int | None
    staff_role_id: int | None
    leader_role_id: int | None
    log_channel_id: int | None
    dashboard_channel_id: int | None  # legacy v0.4 panel; retired by v1.0
    dashboard_message_id: int | None  # legacy v0.4 panel; retired by v1.0
    report_channel_id: int | None
    activity_report_channel_id: int | None
    vacation_role_id: int | None
    vacation_role_cutover_at: str | None
    academy_role_id: int | None
    main_role_id: int | None
    group_role_cutover_at: str | None
    public_dashboard_channel_id: int | None
    public_dashboard_message_id: int | None
    staff_dashboard_channel_id: int | None
    staff_dashboard_message_id: int | None
    timezone: str
    notice_minutes: int
    newcomer_days: int
    member_eval_days: int
    min_member_opportunities: int
    weekly_min_opportunities: int


@dataclass(slots=True, frozen=True)
class RegistrationCode:
    code: str
    expires_at: datetime
    window_id: int


class Repository:
    def __init__(self, db: Database):
        self.db = db

    async def audit(
        self,
        guild_id: int,
        actor_user_id: int | None,
        action: str,
        entity_type: str,
        entity_id: str | int | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO audit_log(guild_id, actor_user_id, action, entity_type, entity_id, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                actor_user_id,
                action,
                entity_type,
                str(entity_id) if entity_id is not None else None,
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                iso(utcnow()),
            ),
        )

    # ---------- guild settings ----------

    async def ensure_guild_settings(self, guild_id: int) -> GuildSettings:
        now = iso(utcnow())
        await self.db.execute(
            """
            INSERT OR IGNORE INTO guild_settings(guild_id, created_at, updated_at)
            VALUES (?, ?, ?)
            """,
            (guild_id, now, now),
        )
        settings = await self.get_guild_settings(guild_id)
        assert settings is not None
        return settings

    async def get_guild_settings(self, guild_id: int) -> GuildSettings | None:
        row = await self.db.fetchone(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )
        if not row:
            return None
        return GuildSettings(
            guild_id=int(row["guild_id"]),
            family_role_id=row["family_role_id"],
            staff_role_id=row["staff_role_id"],
            leader_role_id=row["leader_role_id"],
            log_channel_id=row["log_channel_id"],
            dashboard_channel_id=row["dashboard_channel_id"],
            dashboard_message_id=row["dashboard_message_id"],
            report_channel_id=row["report_channel_id"],
            activity_report_channel_id=row["activity_report_channel_id"],
            vacation_role_id=row["vacation_role_id"],
            vacation_role_cutover_at=row["vacation_role_cutover_at"],
            academy_role_id=row["academy_role_id"],
            main_role_id=row["main_role_id"],
            group_role_cutover_at=row["group_role_cutover_at"],
            public_dashboard_channel_id=row["public_dashboard_channel_id"],
            public_dashboard_message_id=row["public_dashboard_message_id"],
            staff_dashboard_channel_id=row["staff_dashboard_channel_id"],
            staff_dashboard_message_id=row["staff_dashboard_message_id"],
            timezone=row["timezone"],
            notice_minutes=int(row["notice_minutes"]),
            newcomer_days=int(row["newcomer_days"]),
            member_eval_days=int(row["member_eval_days"]),
            min_member_opportunities=int(row["min_member_opportunities"]),
            weekly_min_opportunities=int(row["weekly_min_opportunities"]),
        )

    async def update_guild_settings(self, guild_id: int, **fields: Any) -> None:
        allowed = {
            "family_role_id",
            "staff_role_id",
            "leader_role_id",
            "log_channel_id",
            "dashboard_channel_id",
            "dashboard_message_id",
            "report_channel_id",
            "activity_report_channel_id",
            "vacation_role_id",
            "vacation_role_cutover_at",
            "academy_role_id",
            "main_role_id",
            "group_role_cutover_at",
            "public_dashboard_channel_id",
            "public_dashboard_message_id",
            "staff_dashboard_channel_id",
            "staff_dashboard_message_id",
            "timezone",
            "notice_minutes",
            "newcomer_days",
            "member_eval_days",
            "min_member_opportunities",
            "weekly_min_opportunities",
        }
        if not fields:
            return
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown settings fields: {unknown}")
        await self.ensure_guild_settings(guild_id)
        fields["updated_at"] = iso(utcnow())
        assignments = ", ".join(f"{key} = ?" for key in fields)
        await self.db.execute(
            f"UPDATE guild_settings SET {assignments} WHERE guild_id = ?",
            [*fields.values(), guild_id],
        )

    # ---------- roster lifecycle ----------

    async def get_person_by_discord(self, guild_id: int, discord_user_id: int):
        return await self.db.fetchone(
            "SELECT * FROM people WHERE guild_id = ? AND discord_user_id = ?",
            (guild_id, discord_user_id),
        )

    async def get_person_by_static(self, guild_id: int, static_id: str):
        return await self.db.fetchone(
            "SELECT * FROM people WHERE guild_id = ? AND static_id = ?",
            (guild_id, static_id.strip()),
        )

    async def get_active_membership_by_discord(self, guild_id: int, discord_user_id: int):
        return await self.db.fetchone(
            """
            SELECT m.*, p.discord_user_id, p.static_id, p.nickname
            FROM memberships m
            JOIN people p ON p.id = m.person_id
            WHERE m.guild_id = ? AND p.discord_user_id = ? AND m.status = 'active'
            """,
            (guild_id, discord_user_id),
        )

    async def get_active_membership_by_static(self, guild_id: int, static_id: str):
        return await self.db.fetchone(
            """
            SELECT m.*, p.discord_user_id, p.static_id, p.nickname
            FROM memberships m
            JOIN people p ON p.id = m.person_id
            WHERE m.guild_id = ? AND p.static_id = ? AND m.status = 'active'
            """,
            (guild_id, static_id.strip()),
        )

    async def get_membership(self, membership_id: int):
        return await self.db.fetchone(
            """
            SELECT m.*, p.discord_user_id, p.static_id, p.nickname
            FROM memberships m
            JOIN people p ON p.id = m.person_id
            WHERE m.id = ?
            """,
            (membership_id,),
        )

    async def list_active_members(self, guild_id: int):
        return await self.db.fetchall(
            """
            SELECT m.*, p.discord_user_id, p.static_id, p.nickname
            FROM memberships m
            JOIN people p ON p.id = m.person_id
            WHERE m.guild_id = ? AND m.status = 'active'
            ORDER BY m.rank DESC, p.nickname COLLATE NOCASE
            """,
            (guild_id,),
        )

    async def list_membership_history_for_person(self, person_id: int):
        return await self.db.fetchall(
            """
            SELECT * FROM memberships
            WHERE person_id = ?
            ORDER BY joined_at DESC
            """,
            (person_id,),
        )

    async def add_member(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        static_id: str,
        nickname: str,
        rank: int,
        joined_at: datetime,
        actor_user_id: int,
        audit_action: str = "member.added",
    ) -> int:
        static_id = static_id.strip()
        nickname = nickname.strip()
        if not static_id or not nickname:
            raise DomainError("Static ID and nickname are required")
        if rank < 0:
            raise DomainError("Rank cannot be negative")
        if joined_at > utcnow() + timedelta(minutes=5):
            raise DomainError("Join date cannot be in the future")
        now = iso(utcnow())

        def tx(conn: sqlite3.Connection) -> int:
            person = conn.execute(
                "SELECT * FROM people WHERE guild_id = ? AND static_id = ?",
                (guild_id, static_id),
            ).fetchone()
            discord_owner = conn.execute(
                "SELECT * FROM people WHERE guild_id = ? AND discord_user_id = ?",
                (guild_id, discord_user_id),
            ).fetchone()

            if person and discord_owner and int(person["id"]) != int(discord_owner["id"]):
                raise DomainError("Discord account is already linked to another Static ID")
            if not person and discord_owner:
                raise DomainError(
                    f"Discord account is already linked to Static ID {discord_owner['static_id']}"
                )

            if person:
                active = conn.execute(
                    "SELECT id FROM memberships WHERE guild_id = ? AND person_id = ? AND status = 'active'",
                    (guild_id, person["id"]),
                ).fetchone()
                if active:
                    raise DomainError("This person is already in the active roster")
                person_id = int(person["id"])
                conn.execute(
                    "UPDATE people SET discord_user_id = ?, nickname = ?, updated_at = ? WHERE id = ?",
                    (discord_user_id, nickname, now, person_id),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO people(guild_id, discord_user_id, static_id, nickname, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (guild_id, discord_user_id, static_id, nickname, now, now),
                )
                person_id = int(cur.lastrowid)

            cur = conn.execute(
                """
                INSERT INTO memberships(
                    guild_id, person_id, rank, status, joined_at, created_by_user_id, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (guild_id, person_id, rank, iso(joined_at), actor_user_id, now, now),
            )
            membership_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO rank_history(membership_id, old_rank, new_rank, changed_by_user_id, changed_at, reason)
                VALUES (?, NULL, ?, ?, ?, 'Вступление в семью')
                """,
                (membership_id, rank, actor_user_id, now),
            )
            return membership_id

        membership_id = await self.db.transaction(tx)
        await self.audit(
            guild_id,
            actor_user_id,
            audit_action,
            "membership",
            membership_id,
            {"static_id": static_id, "nickname": nickname, "rank": rank},
        )
        return membership_id

    async def rejoin_member(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        static_id: str,
        nickname: str,
        rank: int,
        joined_at: datetime,
        actor_user_id: int,
    ) -> int:
        # add_member intentionally handles an existing departed person and creates a new membership episode.
        membership_id = await self.add_member(
            guild_id=guild_id,
            discord_user_id=discord_user_id,
            static_id=static_id,
            nickname=nickname,
            rank=rank,
            joined_at=joined_at,
            actor_user_id=actor_user_id,
            audit_action="member.rejoined",
        )
        return membership_id

    async def leave_member(
        self,
        *,
        membership_id: int,
        exit_type: str,
        reason: str,
        actor_user_id: int,
        left_at: datetime | None = None,
    ) -> None:
        if exit_type not in {"voluntary", "kicked", "other"}:
            raise DomainError("Invalid exit type")
        if not reason.strip():
            raise DomainError("Exit reason is required")
        left_at = left_at or utcnow()
        now = iso(utcnow())

        def tx(conn: sqlite3.Connection) -> tuple[int, int]:
            row = conn.execute(
                "SELECT * FROM memberships WHERE id = ?", (membership_id,)
            ).fetchone()
            if not row:
                raise DomainError("Membership not found")
            if row["status"] != "active":
                raise DomainError("Membership is already closed")
            joined_at = parse_iso(row["joined_at"])
            if left_at < joined_at:
                raise DomainError("Leave time cannot be before join time")
            if left_at > utcnow() + timedelta(minutes=5):
                raise DomainError("Leave time cannot be in the future")
            # Pending vacation requests no longer make sense after the membership closes.
            # Approved vacations are preserved because changing them would rewrite historical analytics.
            conn.execute(
                """
                UPDATE vacations
                SET status='cancelled', decided_by_user_id=?, decision_reason='Членство закрыто', decided_at=?
                WHERE membership_id=? AND status='pending'
                """,
                (actor_user_id, now, membership_id),
            )
            conn.execute(
                """
                UPDATE memberships
                SET status='departed', left_at=?, exit_type=?, exit_reason=?, ended_by_user_id=?, updated_at=?
                WHERE id=?
                """,
                (iso(left_at), exit_type, reason.strip(), actor_user_id, now, membership_id),
            )
            # Close open availability windows so historical availability remains immutable.
            conn.execute(
                """
                UPDATE availability
                SET effective_to = ?
                WHERE membership_id = ? AND effective_to IS NULL AND effective_from < ?
                """,
                (iso(left_at), membership_id, iso(left_at)),
            )
            conn.execute(
                """
                UPDATE membership_group_periods
                SET ends_at=?
                WHERE membership_id=? AND ends_at IS NULL AND starts_at<=?
                """,
                (iso(left_at), membership_id, iso(left_at)),
            )
            return int(row["guild_id"]), int(row["person_id"])

        guild_id, person_id = await self.db.transaction(tx)
        settings = await self.get_guild_settings(guild_id)
        if settings:
            await self.close_role_vacation(
                membership_id=membership_id,
                ends_at=left_at,
                source="manual_sync",
                actor_user_id=actor_user_id,
            )
        await self.audit(
            guild_id,
            actor_user_id,
            "member.left",
            "membership",
            membership_id,
            {"exit_type": exit_type, "reason": reason, "person_id": person_id},
        )

    async def change_rank(
        self,
        membership_id: int,
        new_rank: int,
        actor_user_id: int,
        reason: str | None = None,
    ) -> None:
        if new_rank < 0:
            raise DomainError("Rank cannot be negative")
        now = iso(utcnow())

        def tx(conn: sqlite3.Connection) -> tuple[int, int]:
            row = conn.execute(
                "SELECT guild_id, rank, status FROM memberships WHERE id = ?", (membership_id,)
            ).fetchone()
            if not row:
                raise DomainError("Membership not found")
            if row["status"] != "active":
                raise DomainError("Cannot change rank of departed member")
            old_rank = int(row["rank"])
            conn.execute(
                "UPDATE memberships SET rank=?, updated_at=? WHERE id=?",
                (new_rank, now, membership_id),
            )
            conn.execute(
                """
                INSERT INTO rank_history(membership_id, old_rank, new_rank, changed_by_user_id, changed_at, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (membership_id, old_rank, new_rank, actor_user_id, now, reason),
            )
            return int(row["guild_id"]), old_rank

        guild_id, old_rank = await self.db.transaction(tx)
        await self.audit(
            guild_id,
            actor_user_id,
            "member.rank_changed",
            "membership",
            membership_id,
            {"old_rank": old_rank, "new_rank": new_rank, "reason": reason},
        )

    # ---------- availability ----------

    async def set_availability(
        self,
        *,
        membership_id: int,
        weekday_segments: Iterable[str],
        weekend_segments: Iterable[str],
        effective_from: datetime,
        actor_user_id: int,
    ) -> None:
        weekdays = sorted(set(weekday_segments))
        weekends = sorted(set(weekend_segments))
        valid = {"morning", "day", "evening", "night", "floating"}
        if not weekdays or not weekends:
            raise DomainError("Weekday and weekend availability must both be configured")
        if not set(weekdays) <= valid or not set(weekends) <= valid:
            raise DomainError("Invalid availability segment")
        if ("floating" in weekdays and len(weekdays) > 1) or (
            "floating" in weekends and len(weekends) > 1
        ):
            raise DomainError("Floating cannot be combined with fixed segments")

        now = iso(utcnow())
        effective_iso = iso(effective_from)

        def tx(conn: sqlite3.Connection) -> int:
            membership = conn.execute(
                "SELECT guild_id, status FROM memberships WHERE id = ?", (membership_id,)
            ).fetchone()
            if not membership or membership["status"] != "active":
                raise DomainError("Active membership not found")

            # Any configuration that would still be active at effective_from is closed exactly there.
            conn.execute(
                """
                UPDATE availability
                SET effective_to = ?
                WHERE membership_id = ?
                  AND effective_from < ?
                  AND (effective_to IS NULL OR effective_to > ?)
                """,
                (effective_iso, membership_id, effective_iso, effective_iso),
            )
            # Remove a future config starting at the same time so retries are idempotent.
            conn.execute(
                "DELETE FROM availability WHERE membership_id = ? AND effective_from = ?",
                (membership_id, effective_iso),
            )
            rows = []
            for segment in weekdays:
                rows.append((membership_id, "weekday", segment, effective_iso, actor_user_id, now))
            for segment in weekends:
                rows.append((membership_id, "weekend", segment, effective_iso, actor_user_id, now))
            conn.executemany(
                """
                INSERT INTO availability(membership_id, day_group, segment, effective_from, created_by_user_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            return int(membership["guild_id"])

        guild_id = await self.db.transaction(tx)
        await self.audit(
            guild_id,
            actor_user_id,
            "member.availability_changed",
            "membership",
            membership_id,
            {
                "weekday": weekdays,
                "weekend": weekends,
                "effective_from": effective_iso,
            },
        )

    async def availability_at(self, membership_id: int, when: datetime):
        return await self.db.fetchall(
            """
            SELECT * FROM availability
            WHERE membership_id = ?
              AND effective_from <= ?
              AND (effective_to IS NULL OR effective_to > ?)
            """,
            (membership_id, iso(when), iso(when)),
        )

    async def has_any_availability(self, membership_id: int) -> bool:
        row = await self.db.fetchone(
            "SELECT 1 FROM availability WHERE membership_id = ? LIMIT 1",
            (membership_id,),
        )
        return bool(row)

    # ---------- Academy / Main role groups ----------

    async def get_open_group_period(self, membership_id: int):
        return await self.db.fetchone(
            """
            SELECT * FROM membership_group_periods
            WHERE membership_id=? AND ends_at IS NULL
            ORDER BY starts_at DESC, id DESC LIMIT 1
            """,
            (membership_id,),
        )

    async def set_membership_group_state(
        self,
        *,
        membership_id: int,
        group_name: str,
        starts_at: datetime,
        source: str,
        actor_user_id: int | None = None,
    ) -> bool:
        if group_name not in {"academy", "main", "unclassified", "conflict"}:
            raise DomainError("Invalid membership group")
        if source not in {"role_event", "startup_sync", "periodic_sync", "manual_sync", "setup_sync"}:
            raise DomainError("Invalid group sync source")
        membership = await self.get_membership(membership_id)
        if not membership or membership["status"] != "active":
            raise DomainError("Active membership not found")
        at = iso(starts_at)
        uncertain = int(source not in {"role_event", "setup_sync"})

        def tx(conn: sqlite3.Connection) -> bool:
            current = conn.execute(
                """
                SELECT * FROM membership_group_periods
                WHERE membership_id=? AND ends_at IS NULL
                ORDER BY starts_at DESC, id DESC LIMIT 1
                """,
                (membership_id,),
            ).fetchone()
            if current and str(current["group_name"]) == group_name:
                return False
            if current:
                current_start = parse_iso(str(current["starts_at"]))
                end_at = max(starts_at, current_start)
                conn.execute(
                    "UPDATE membership_group_periods SET ends_at=? WHERE id=?",
                    (iso(end_at), int(current["id"])),
                )
            conn.execute(
                """
                INSERT INTO membership_group_periods(
                    membership_id, group_name, starts_at, source, sync_uncertain, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (membership_id, group_name, at, source, uncertain, iso(utcnow())),
            )
            return True

        changed = await self.db.transaction(tx)
        if changed:
            await self.audit(
                int(membership["guild_id"]),
                actor_user_id,
                "membership.group_changed",
                "membership",
                membership_id,
                {
                    "group": group_name,
                    "starts_at": at,
                    "source": source,
                    "sync_uncertain": bool(uncertain),
                },
            )
        return bool(changed)

    async def close_all_open_group_periods_for_guild(
        self,
        guild_id: int,
        *,
        ends_at: datetime,
        actor_user_id: int | None = None,
    ) -> int:
        rows = await self.db.fetchall(
            """
            SELECT gp.id, gp.membership_id, gp.group_name
            FROM membership_group_periods gp
            JOIN memberships m ON m.id=gp.membership_id
            WHERE m.guild_id=? AND gp.ends_at IS NULL
            """,
            (guild_id,),
        )
        closed = 0
        for row in rows:
            await self.db.execute(
                "UPDATE membership_group_periods SET ends_at=? WHERE id=?",
                (iso(ends_at), int(row["id"])),
            )
            closed += 1
        if closed:
            await self.audit(
                guild_id,
                actor_user_id,
                "guild.group_periods_closed",
                "guild",
                guild_id,
                {"count": closed, "ends_at": iso(ends_at)},
            )
        return closed

    async def group_periods_for_memberships_period(
        self,
        membership_ids: Iterable[int],
        start: datetime,
        end: datetime,
    ):
        ids = sorted(set(int(x) for x in membership_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return await self.db.fetchall(
            f"""
            SELECT * FROM membership_group_periods
            WHERE membership_id IN ({placeholders})
              AND starts_at < ?
              AND (ends_at IS NULL OR ends_at > ?)
            ORDER BY membership_id, starts_at, id
            """,
            [*ids, iso(end), iso(start)],
        )

    async def current_group_rows(self, guild_id: int):
        return await self.db.fetchall(
            """
            SELECT gp.*, p.nickname, p.static_id, p.discord_user_id, m.rank
            FROM membership_group_periods gp
            JOIN memberships m ON m.id=gp.membership_id
            JOIN people p ON p.id=m.person_id
            WHERE m.guild_id=? AND m.status='active' AND gp.ends_at IS NULL
            ORDER BY gp.group_name, m.rank DESC, p.nickname COLLATE NOCASE
            """,
            (guild_id,),
        )

    async def list_group_history(self, membership_id: int):
        return await self.db.fetchall(
            """
            SELECT * FROM membership_group_periods
            WHERE membership_id=?
            ORDER BY starts_at, id
            """,
            (membership_id,),
        )

    async def group_at(self, membership_id: int, when: datetime) -> str | None:
        row = await self.db.fetchone(
            """
            SELECT group_name FROM membership_group_periods
            WHERE membership_id=? AND starts_at<=?
              AND (ends_at IS NULL OR ends_at>?)
            ORDER BY starts_at DESC, id DESC LIMIT 1
            """,
            (membership_id, iso(when), iso(when)),
        )
        return str(row["group_name"]) if row else None

    # ---------- vacations ----------

    async def request_vacation(
        self,
        *,
        membership_id: int,
        starts_on: date,
        ends_on: date,
        reason: str,
        requested_by_user_id: int,
    ) -> int:
        if starts_on > ends_on:
            raise DomainError("Vacation start cannot be after end")
        if not reason.strip():
            raise DomainError("Vacation reason is required")
        membership = await self.get_membership(membership_id)
        if not membership or membership["status"] != "active":
            raise DomainError("Active membership not found")
        overlap = await self.db.fetchone(
            """
            SELECT id FROM vacations
            WHERE membership_id=? AND status IN ('pending','approved')
              AND starts_on <= ? AND ends_on >= ?
            LIMIT 1
            """,
            (membership_id, ends_on.isoformat(), starts_on.isoformat()),
        )
        if overlap:
            raise DomainError(f"Vacation overlaps existing request #{overlap['id']}")
        vacation_id = await self.db.execute(
            """
            INSERT INTO vacations(
                membership_id, starts_on, ends_on, reason, status, requested_by_user_id, created_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                membership_id,
                starts_on.isoformat(),
                ends_on.isoformat(),
                reason.strip(),
                requested_by_user_id,
                iso(utcnow()),
            ),
        )
        await self.audit(
            int(membership["guild_id"]), requested_by_user_id, "vacation.requested",
            "vacation", vacation_id,
            {"starts_on": starts_on.isoformat(), "ends_on": ends_on.isoformat(), "reason": reason},
        )
        return vacation_id

    async def decide_vacation(
        self,
        vacation_id: int,
        approve: bool,
        actor_user_id: int,
        reason: str | None = None,
    ) -> None:
        row = await self.db.fetchone(
            """
            SELECT v.*, m.guild_id, m.status AS membership_status FROM vacations v
            JOIN memberships m ON m.id = v.membership_id
            WHERE v.id = ?
            """,
            (vacation_id,),
        )
        if not row:
            raise DomainError("Vacation request not found")
        if row["status"] != "pending":
            raise DomainError("Vacation request is already decided")
        if approve and row["membership_status"] != "active":
            raise DomainError("Cannot approve vacation for a departed membership")
        status = "approved" if approve else "rejected"
        await self.db.execute(
            """
            UPDATE vacations
            SET status=?, decided_by_user_id=?, decision_reason=?, decided_at=?
            WHERE id=?
            """,
            (status, actor_user_id, reason, iso(utcnow()), vacation_id),
        )
        await self.audit(
            int(row["guild_id"]), actor_user_id, f"vacation.{status}", "vacation", vacation_id,
            {"decision_reason": reason},
        )

    async def cancel_vacation(self, vacation_id: int, actor_user_id: int) -> None:
        row = await self.db.fetchone(
            """
            SELECT v.*, m.guild_id FROM vacations v
            JOIN memberships m ON m.id=v.membership_id WHERE v.id=?
            """,
            (vacation_id,),
        )
        if not row:
            raise DomainError("Vacation not found")
        if row["status"] not in {"pending", "approved"}:
            raise DomainError("Vacation cannot be cancelled")
        await self.db.execute(
            "UPDATE vacations SET status='cancelled', decided_by_user_id=?, decided_at=? WHERE id=?",
            (actor_user_id, iso(utcnow()), vacation_id),
        )
        await self.audit(int(row["guild_id"]), actor_user_id, "vacation.cancelled", "vacation", vacation_id)

    async def approved_vacations_for_period(
        self, membership_id: int, start_date: date, end_date: date
    ):
        return await self.db.fetchall(
            """
            SELECT * FROM vacations
            WHERE membership_id = ? AND status = 'approved'
              AND starts_on <= ? AND ends_on >= ?
            ORDER BY starts_on
            """,
            (membership_id, end_date.isoformat(), start_date.isoformat()),
        )

    # ---------- activities ----------

    async def create_activity(
        self,
        *,
        guild_id: int,
        category: str,
        title: str,
        description: str | None,
        analytical: bool,
        audience_type: str,
        min_rank: int | None,
        max_rank: int | None,
        scheduled_for: datetime,
        organizer_membership_id: int,
        notice_threshold_minutes: int,
        actor_user_id: int,
        classification_mode: str = "auto",
        audience_group: str | None = None,
        custom_membership_ids: Iterable[int] | None = None,
    ) -> int:
        custom_ids = sorted(set(custom_membership_ids or []))
        if not title.strip():
            raise DomainError("Activity title cannot be empty")
        if category not in {"training", "family", "faction"}:
            raise DomainError("Invalid activity category")
        if audience_type not in {"all", "rank_range", "custom"}:
            raise DomainError("Invalid audience type")
        if audience_group not in {None, "academy", "main"}:
            raise DomainError("Invalid audience group")
        if classification_mode not in {"auto", "planned", "spontaneous"}:
            raise DomainError("Invalid activity classification mode")
        if audience_type == "rank_range" and (
            min_rank is None or max_rank is None or min_rank < 0 or max_rank < 0 or min_rank > max_rank
        ):
            raise DomainError("Rank-range audience requires 0 <= min_rank <= max_rank")
        if audience_type == "custom" and not custom_ids:
            raise DomainError("Custom audience cannot be empty")

        announced = utcnow()
        if scheduled_for < announced - timedelta(minutes=5):
            raise DomainError("Activity start time cannot be in the past. Use the current/future time.")
        notice = max(0, int((scheduled_for - announced).total_seconds() // 60))
        if classification_mode == "planned":
            if notice < notice_threshold_minutes:
                raise DomainError(
                    f"Cannot mark activity as planned: only {notice} min notice; minimum is {notice_threshold_minutes} min"
                )
            spontaneous = False
        elif classification_mode == "spontaneous":
            spontaneous = True
        else:
            spontaneous = notice < notice_threshold_minutes
        now_iso = iso(announced)
        def tx(conn: sqlite3.Connection) -> int:
            organizer = conn.execute(
                "SELECT status FROM memberships WHERE id=? AND guild_id=?",
                (organizer_membership_id, guild_id),
            ).fetchone()
            if not organizer or organizer["status"] != "active":
                raise DomainError("Organizer is not an active family member")
            if custom_ids:
                placeholders = ",".join("?" for _ in custom_ids)
                valid_custom = conn.execute(
                    f"""
                    SELECT COUNT(*) AS c FROM memberships
                    WHERE id IN ({placeholders}) AND guild_id=? AND status='active'
                    """,
                    [*custom_ids, guild_id],
                ).fetchone()
                if int(valid_custom["c"]) != len(custom_ids):
                    raise DomainError("Custom audience contains a member outside the active guild roster")
            cur = conn.execute(
                """
                INSERT INTO activities(
                    guild_id, category, title, description, analytical, audience_type, audience_group,
                    min_rank, max_rank, announced_at, scheduled_for, status,
                    is_spontaneous, notice_minutes, organizer_membership_id, classification_mode, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id, category, title.strip(), description,
                    int(analytical), audience_type, audience_group, min_rank, max_rank,
                    now_iso, iso(scheduled_for), int(spontaneous), notice,
                    organizer_membership_id, classification_mode, now_iso, now_iso,
                ),
            )
            activity_id = int(cur.lastrowid)
            if custom_ids:
                conn.executemany(
                    "INSERT INTO activity_audience_members(activity_id, membership_id) VALUES (?, ?)",
                    [(activity_id, mid) for mid in custom_ids],
                )
            return activity_id

        activity_id = await self.db.transaction(tx)
        await self.audit(
            guild_id, actor_user_id, "activity.created", "activity", activity_id,
            {
                "category": category,
                "title": title,
                "scheduled_for": iso(scheduled_for),
                "analytical": analytical,
                "audience_type": audience_type,
                "audience_group": audience_group,
                "is_spontaneous": spontaneous,
                "notice_minutes": notice,
                "classification_mode": classification_mode,
            },
        )
        return activity_id

    async def set_activity_panel(self, activity_id: int, channel_id: int, message_id: int) -> None:
        await self.db.execute(
            "UPDATE activities SET panel_channel_id=?, panel_message_id=?, updated_at=? WHERE id=?",
            (channel_id, message_id, iso(utcnow()), activity_id),
        )

    async def add_activity_evidence(
        self,
        *,
        activity_id: int,
        url: str,
        filename: str | None,
        content_type: str | None,
        actor_user_id: int,
    ) -> int:
        activity = await self.get_activity(activity_id)
        if not activity or activity["status"] not in {"running", "closed"}:
            raise DomainError("Evidence can be added only while an activity is running or in the 24-hour edit window")
        if not url.strip():
            raise DomainError("Evidence URL is empty")
        evidence_count_row = await self.db.fetchone(
            "SELECT COUNT(*) AS c FROM activity_evidence WHERE activity_id=?",
            (activity_id,),
        )
        if evidence_count_row and int(evidence_count_row["c"]) >= 8:
            raise DomainError("К одной активности можно прикрепить максимум 8 файлов-доказательств")
        evidence_id = await self.db.execute(
            """
            INSERT INTO activity_evidence(activity_id, url, filename, content_type, added_by_user_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (activity_id, url.strip(), filename, content_type, actor_user_id, iso(utcnow())),
        )
        await self.audit(
            int(activity["guild_id"]), actor_user_id, "activity.evidence_added", "activity", activity_id,
            {"evidence_id": evidence_id, "filename": filename, "content_type": content_type},
        )
        return evidence_id

    async def list_activity_evidence(self, activity_id: int):
        return await self.db.fetchall(
            "SELECT * FROM activity_evidence WHERE activity_id=? ORDER BY created_at, id",
            (activity_id,),
        )

    async def mark_activity_evidence_mirrored(
        self,
        evidence_id: int,
        *,
        mirrored_url: str,
        channel_id: int,
        message_id: int,
    ) -> None:
        await self.db.execute(
            """
            UPDATE activity_evidence
            SET mirrored_url=?, mirror_channel_id=?, mirror_message_id=?
            WHERE id=?
            """,
            (mirrored_url, channel_id, message_id, evidence_id),
        )

    async def mark_activity_report_data_message(
        self, activity_id: int, message_id: int | None
    ) -> None:
        await self.db.execute(
            "UPDATE activities SET report_data_message_id=?, updated_at=? WHERE id=?",
            (message_id, iso(utcnow()), activity_id),
        )

    async def mark_activity_report_posted(
        self, activity_id: int, channel_id: int, message_id: int
    ) -> None:
        await self.db.execute(
            """
            UPDATE activities
            SET report_channel_id=?, report_message_id=?, report_posted_at=?, updated_at=?
            WHERE id=?
            """,
            (channel_id, message_id, iso(utcnow()), iso(utcnow()), activity_id),
        )

    async def list_recent_closed_activities(self, guild_id: int, limit: int = 20):
        limit = max(1, min(int(limit), 50))
        return await self.db.fetchall(
            """
            SELECT a.*, p.nickname AS organizer_nickname
            FROM activities a
            JOIN memberships m ON m.id=a.organizer_membership_id
            JOIN people p ON p.id=m.person_id
            WHERE a.guild_id=? AND a.status IN ('closed','finalized')
            ORDER BY a.ended_at DESC
            LIMIT ?
            """,
            (guild_id, limit),
        )

    async def list_unposted_activity_reports(self, guild_id: int, limit: int = 20):
        limit = max(1, min(int(limit), 50))
        return await self.db.fetchall(
            """
            SELECT id FROM activities
            WHERE guild_id=? AND status IN ('closed','finalized')
              AND report_message_id IS NULL
            ORDER BY ended_at
            LIMIT ?
            """,
            (guild_id, limit),
        )

    async def get_activity(self, activity_id: int):
        return await self.db.fetchone(
            """
            SELECT a.*, p.nickname AS organizer_nickname, p.static_id AS organizer_static_id,
                   p.discord_user_id AS organizer_discord_user_id
            FROM activities a
            JOIN memberships m ON m.id = a.organizer_membership_id
            JOIN people p ON p.id = m.person_id
            WHERE a.id = ?
            """,
            (activity_id,),
        )

    async def list_open_activities(self, guild_id: int | None = None):
        if guild_id is None:
            return await self.db.fetchall(
                "SELECT * FROM activities WHERE status IN ('scheduled', 'running') ORDER BY scheduled_for"
            )
        return await self.db.fetchall(
            "SELECT * FROM activities WHERE guild_id=? AND status IN ('scheduled','running') ORDER BY scheduled_for",
            (guild_id,),
        )

    async def list_activities_period(self, guild_id: int, start: datetime, end: datetime):
        return await self.db.fetchall(
            """
            SELECT * FROM activities
            WHERE guild_id=? AND scheduled_for>=? AND scheduled_for<?
              AND status IN ('closed','finalized')
            ORDER BY scheduled_for
            """,
            (guild_id, iso(start), iso(end)),
        )

    async def start_activity(self, activity_id: int, actor_user_id: int) -> None:
        row = await self.get_activity(activity_id)
        if not row:
            raise DomainError("Activity not found")
        if row["status"] != "scheduled":
            raise DomainError("Only scheduled activity can be started")
        now = iso(utcnow())
        await self.db.execute(
            "UPDATE activities SET status='running', started_at=?, updated_at=? WHERE id=?",
            (now, now, activity_id),
        )
        await self.audit(int(row["guild_id"]), actor_user_id, "activity.started", "activity", activity_id)

    @staticmethod
    def _generate_code(length: int = 6) -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "".join(secrets.choice(alphabet) for _ in range(length))

    @staticmethod
    def hash_code(code: str) -> str:
        return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()

    async def open_registration(
        self,
        *,
        activity_id: int,
        kind: str,
        actor_user_id: int,
        ttl_minutes: int = 7,
    ) -> RegistrationCode:
        if kind not in {"primary", "late", "control"}:
            raise DomainError("Invalid registration kind")
        row = await self.get_activity(activity_id)
        if not row or row["status"] != "running":
            raise DomainError("Activity must be running")
        now = utcnow()
        expires = now + timedelta(minutes=ttl_minutes)
        code = self._generate_code()
        code_hash = self.hash_code(code)

        def tx(conn: sqlite3.Connection) -> int:
            # Only one active code window per activity. Old ones remain in history.
            conn.execute(
                """
                UPDATE registration_windows
                SET closed_at=?
                WHERE activity_id=? AND closed_at IS NULL
                """,
                (iso(now), activity_id),
            )
            cur = conn.execute(
                """
                INSERT INTO registration_windows(
                    activity_id, kind, code_hash, opened_at, expires_at, opened_by_user_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (activity_id, kind, code_hash, iso(now), iso(expires), actor_user_id),
            )
            return int(cur.lastrowid)

        window_id = await self.db.transaction(tx)
        await self.audit(
            int(row["guild_id"]), actor_user_id, "activity.registration_opened", "activity", activity_id,
            {"kind": kind, "expires_at": iso(expires), "window_id": window_id},
        )
        return RegistrationCode(code=code, expires_at=expires, window_id=window_id)

    async def checkin_with_code(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        code: str,
    ) -> tuple[int, str, bool]:
        now = utcnow()
        code_hash = self.hash_code(code)

        def tx(conn: sqlite3.Connection) -> tuple[int, str, bool]:
            membership = conn.execute(
                """
                SELECT m.* FROM memberships m
                JOIN people p ON p.id=m.person_id
                WHERE m.guild_id=? AND p.discord_user_id=? AND m.status='active'
                """,
                (guild_id, discord_user_id),
            ).fetchone()
            if not membership:
                raise DomainError("You are not in the active family roster")

            window = conn.execute(
                """
                SELECT rw.*, a.guild_id, a.status, a.audience_type, a.audience_group,
                       a.min_rank, a.max_rank, a.scheduled_for
                FROM registration_windows rw
                JOIN activities a ON a.id=rw.activity_id
                WHERE rw.code_hash=? AND a.guild_id=?
                  AND rw.closed_at IS NULL AND rw.expires_at>? AND a.status='running'
                ORDER BY rw.opened_at DESC LIMIT 1
                """,
                (code_hash, guild_id, iso(now)),
            ).fetchone()
            if not window:
                raise DomainError("Code is invalid or expired")

            activity_id = int(window["activity_id"])
            audience_group = window["audience_group"]
            if audience_group is not None:
                scheduled_for = str(window["scheduled_for"])
                group_row = conn.execute(
                    """
                    SELECT group_name FROM membership_group_periods
                    WHERE membership_id=? AND starts_at<=?
                      AND (ends_at IS NULL OR ends_at>?)
                    ORDER BY starts_at DESC, id DESC LIMIT 1
                    """,
                    (membership["id"], scheduled_for, scheduled_for),
                ).fetchone()
                actual_group = str(group_row["group_name"]) if group_row else None
                if actual_group != str(audience_group):
                    expected = "Academy" if str(audience_group) == "academy" else "основного состава"
                    raise DomainError(f"Эта активность доступна только для {expected}")

            audience_type = str(window["audience_type"])
            if audience_type == "rank_range":
                low = int(window["min_rank"]) if window["min_rank"] is not None else -10**9
                high = int(window["max_rank"]) if window["max_rank"] is not None else 10**9
                if not (low <= int(membership["rank"]) <= high):
                    raise DomainError("This activity is not available to your rank")
            elif audience_type == "custom":
                allowed = conn.execute(
                    "SELECT 1 FROM activity_audience_members WHERE activity_id=? AND membership_id=?",
                    (activity_id, membership["id"]),
                ).fetchone()
                if not allowed:
                    raise DomainError("You are not in the audience of this activity")

            existing = conn.execute(
                "SELECT * FROM attendance WHERE activity_id=? AND membership_id=?",
                (activity_id, membership["id"]),
            ).fetchone()
            kind = str(window["kind"])
            source = {
                "primary": "primary_code",
                "late": "late_code",
                "control": "control_code",
            }[kind]
            created = False
            if not existing:
                cur = conn.execute(
                    """
                    INSERT INTO attendance(activity_id, membership_id, first_seen_at, source)
                    VALUES (?, ?, ?, ?)
                    """,
                    (activity_id, membership["id"], iso(now), source),
                )
                attendance_id = int(cur.lastrowid)
                created = True
            else:
                if existing["removed_at"] is not None:
                    raise DomainError(
                        "Attendance was removed by staff; contact leadership for manual restoration"
                    )
                attendance_id = int(existing["id"])

            already_check = conn.execute(
                """
                SELECT 1 FROM attendance_checks
                WHERE attendance_id=? AND registration_window_id=?
                """,
                (attendance_id, window["id"]),
            ).fetchone()
            if not already_check:
                conn.execute(
                    """
                    INSERT INTO attendance_checks(
                        attendance_id, registration_window_id, kind, checked_at, actor_user_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (attendance_id, window["id"], kind, iso(now), discord_user_id),
                )
            return activity_id, kind, created

        activity_id, kind, created = await self.db.transaction(tx)
        await self.audit(
            guild_id, discord_user_id, "attendance.code_checkin", "activity", activity_id,
            {"kind": kind, "new_attendance": created},
        )
        return activity_id, kind, created

    async def manual_add_attendance(
        self,
        *,
        activity_id: int,
        membership_id: int,
        actor_user_id: int,
        reason: str,
    ) -> None:
        if not reason.strip():
            raise DomainError("Manual attendance requires a reason")
        activity = await self.get_activity(activity_id)
        if not activity or activity["status"] not in {"running", "closed"}:
            raise DomainError("Activity is not editable")
        now = iso(utcnow())

        def tx(conn: sqlite3.Connection) -> None:
            member = conn.execute(
                "SELECT guild_id, joined_at, left_at FROM memberships WHERE id=?", (membership_id,)
            ).fetchone()
            if not member or int(member["guild_id"]) != int(activity["guild_id"]):
                raise DomainError("Member does not belong to this guild")
            event_time = parse_iso(activity["scheduled_for"])
            if parse_iso(member["joined_at"]) > event_time:
                raise DomainError("Member had not joined the family at activity time")
            if member["left_at"] and parse_iso(member["left_at"]) <= event_time:
                raise DomainError("Member had already left the family at activity time")
            existing = conn.execute(
                "SELECT * FROM attendance WHERE activity_id=? AND membership_id=?",
                (activity_id, membership_id),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE attendance
                    SET removed_at=NULL, removed_by_user_id=NULL, remove_reason=NULL,
                        added_by_user_id=?, manual_reason=?
                    WHERE id=?
                    """,
                    (actor_user_id, reason.strip(), existing["id"]),
                )
                attendance_id = int(existing["id"])
            else:
                cur = conn.execute(
                    """
                    INSERT INTO attendance(
                        activity_id, membership_id, first_seen_at, source, added_by_user_id, manual_reason
                    ) VALUES (?, ?, ?, 'manual', ?, ?)
                    """,
                    (activity_id, membership_id, now, actor_user_id, reason.strip()),
                )
                attendance_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO attendance_checks(attendance_id, kind, checked_at, actor_user_id)
                VALUES (?, 'manual', ?, ?)
                """,
                (attendance_id, now, actor_user_id),
            )

        await self.db.transaction(tx)
        await self.audit(
            int(activity["guild_id"]), actor_user_id, "attendance.manual_added", "activity", activity_id,
            {"membership_id": membership_id, "reason": reason},
        )

    async def remove_attendance(
        self,
        *,
        activity_id: int,
        membership_id: int,
        actor_user_id: int,
        reason: str,
    ) -> None:
        if not reason.strip():
            raise DomainError("Removal requires a reason")
        activity = await self.get_activity(activity_id)
        if not activity or activity["status"] not in {"running", "closed"}:
            raise DomainError("Activity is not editable")
        row = await self.db.fetchone(
            "SELECT id, removed_at FROM attendance WHERE activity_id=? AND membership_id=?",
            (activity_id, membership_id),
        )
        if not row or row["removed_at"] is not None:
            raise DomainError("Active attendance record not found")
        await self.db.execute(
            """
            UPDATE attendance
            SET removed_at=?, removed_by_user_id=?, remove_reason=?
            WHERE id=?
            """,
            (iso(utcnow()), actor_user_id, reason.strip(), row["id"]),
        )
        await self.audit(
            int(activity["guild_id"]), actor_user_id, "attendance.removed", "activity", activity_id,
            {"membership_id": membership_id, "reason": reason},
        )

    async def close_activity(
        self,
        *,
        activity_id: int,
        actor_user_id: int,
        evidence_url: str | None,
        closing_note: str | None,
    ) -> None:
        activity = await self.get_activity(activity_id)
        if not activity or activity["status"] != "running":
            raise DomainError("Only running activity can be closed")
        now = iso(utcnow())

        def tx(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE registration_windows SET closed_at=?
                WHERE activity_id=? AND closed_at IS NULL
                """,
                (now, activity_id),
            )
            conn.execute(
                """
                UPDATE activities
                SET status='closed', ended_at=?, evidence_url=?, closing_note=?, updated_at=?
                WHERE id=?
                """,
                (now, evidence_url, closing_note, now, activity_id),
            )

        await self.db.transaction(tx)
        await self.audit(
            int(activity["guild_id"]), actor_user_id, "activity.closed", "activity", activity_id,
            {"evidence_url": evidence_url, "closing_note": closing_note},
        )

    async def finalize_old_activities(self, older_than: datetime) -> int:
        rows = await self.db.fetchall(
            "SELECT id, guild_id FROM activities WHERE status='closed' AND ended_at < ?",
            (iso(older_than),),
        )
        for row in rows:
            await self.db.execute(
                "UPDATE activities SET status='finalized', updated_at=? WHERE id=?",
                (iso(utcnow()), row["id"]),
            )
        return len(rows)

    async def attendance_for_activity(self, activity_id: int):
        return await self.db.fetchall(
            """
            SELECT at.*, m.rank, p.nickname, p.static_id, p.discord_user_id,
                   EXISTS(
                       SELECT 1 FROM attendance_checks ac
                       WHERE ac.attendance_id=at.id AND ac.kind='control'
                   ) AS control_confirmed
            FROM attendance at
            JOIN memberships m ON m.id=at.membership_id
            JOIN people p ON p.id=m.person_id
            WHERE at.activity_id=? AND at.removed_at IS NULL
            ORDER BY at.first_seen_at
            """,
            (activity_id,),
        )

    async def attendance_for_membership_period(
        self, membership_id: int, start: datetime, end: datetime
    ):
        return await self.db.fetchall(
            """
            SELECT a.*, at.first_seen_at, at.source,
                   EXISTS(
                       SELECT 1 FROM attendance_checks ac
                       WHERE ac.attendance_id=at.id AND ac.kind='control'
                   ) AS control_confirmed
            FROM attendance at
            JOIN activities a ON a.id=at.activity_id
            WHERE at.membership_id=? AND at.removed_at IS NULL
              AND a.status IN ('closed','finalized')
              AND a.scheduled_for>=? AND a.scheduled_for<?
            ORDER BY a.scheduled_for
            """,
            (membership_id, iso(start), iso(end)),
        )

    async def custom_audience_ids(self, activity_id: int) -> set[int]:
        rows = await self.db.fetchall(
            "SELECT membership_id FROM activity_audience_members WHERE activity_id=?",
            (activity_id,),
        )
        return {int(row["membership_id"]) for row in rows}

    async def save_weekly_report(
        self,
        *,
        guild_id: int,
        week_start: datetime,
        week_end: datetime,
        pulse_score: int,
        metrics: dict[str, Any],
        explanations: list[str],
    ) -> int:
        now = iso(utcnow())
        payload_metrics = json.dumps(metrics, ensure_ascii=False, sort_keys=True)
        payload_explanations = json.dumps(explanations, ensure_ascii=False)

        def tx(conn: sqlite3.Connection) -> int:
            existing = conn.execute(
                "SELECT id FROM weekly_reports WHERE guild_id=? AND week_start=? AND week_end=?",
                (guild_id, iso(week_start), iso(week_end)),
            ).fetchone()
            if existing:
                # Weekly snapshots are immutable once first generated.
                return int(existing["id"])
            cur = conn.execute(
                """
                INSERT INTO weekly_reports(
                    guild_id, week_start, week_end, pulse_score, metrics_json, explanations_json, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id, iso(week_start), iso(week_end), pulse_score,
                    payload_metrics, payload_explanations, now,
                ),
            )
            return int(cur.lastrowid)

        return await self.db.transaction(tx)

    async def get_weekly_report(self, guild_id: int, week_start: datetime, week_end: datetime):
        return await self.db.fetchone(
            "SELECT * FROM weekly_reports WHERE guild_id=? AND week_start=? AND week_end=?",
            (guild_id, iso(week_start), iso(week_end)),
        )

    # ---------- bulk analytics reads ----------

    async def memberships_overlapping_period(self, guild_id: int, start: datetime, end: datetime):
        return await self.db.fetchall(
            """
            SELECT m.*, p.nickname, p.static_id, p.discord_user_id
            FROM memberships m
            JOIN people p ON p.id=m.person_id
            WHERE m.guild_id=?
              AND m.joined_at < ?
              AND (m.left_at IS NULL OR m.left_at > ?)
            ORDER BY p.nickname COLLATE NOCASE
            """,
            (guild_id, iso(end), iso(start)),
        )

    async def availability_for_memberships_period(
        self, membership_ids: Iterable[int], start: datetime, end: datetime
    ):
        ids = sorted(set(int(x) for x in membership_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return await self.db.fetchall(
            f"""
            SELECT * FROM availability
            WHERE membership_id IN ({placeholders})
              AND effective_from < ?
              AND (effective_to IS NULL OR effective_to > ?)
            ORDER BY membership_id, effective_from
            """,
            [*ids, iso(end), iso(start)],
        )

    async def vacations_for_memberships_period(
        self, membership_ids: Iterable[int], start_date: date, end_date: date
    ):
        """Return legacy and role-sourced vacation records for analytics.

        Role vacations created by v1.2 carry exact UTC timestamps. Older role rows
        keep date precision and are treated conservatively by analytics. Legacy
        approved vacations are only authoritative before the guild role cutover.
        """
        ids = sorted(set(int(x) for x in membership_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return await self.db.fetchall(
            f"""
            SELECT membership_id, starts_on, ends_on, NULL AS starts_at, NULL AS ends_at,
                   'legacy' AS source, 0 AS sync_uncertain
            FROM vacations
            WHERE membership_id IN ({placeholders}) AND status='approved'
              AND starts_on <= ? AND ends_on >= ?
            UNION ALL
            SELECT membership_id, starts_on, COALESCE(ends_on, ?) AS ends_on, starts_at, ends_at,
                   'discord_role' AS source, sync_uncertain
            FROM role_vacations
            WHERE membership_id IN ({placeholders})
              AND starts_on <= ? AND COALESCE(ends_on, ?) >= ?
            ORDER BY membership_id, starts_on
            """,
            [
                *ids, end_date.isoformat(), start_date.isoformat(),
                end_date.isoformat(), *ids, end_date.isoformat(), end_date.isoformat(), start_date.isoformat(),
            ],
        )

    async def get_open_role_vacation(self, membership_id: int):
        return await self.db.fetchone(
            "SELECT * FROM role_vacations WHERE membership_id=? AND ends_on IS NULL ORDER BY id DESC LIMIT 1",
            (membership_id,),
        )

    async def open_role_vacation(
        self,
        *,
        membership_id: int,
        role_id: int,
        starts_at: datetime,
        source: str,
        actor_user_id: int | None = None,
    ) -> tuple[int, bool]:
        if source not in {"role_event", "startup_sync", "periodic_sync", "manual_sync"}:
            raise DomainError("Invalid vacation sync source")
        membership = await self.get_membership(membership_id)
        if not membership or membership["status"] != "active":
            raise DomainError("Active membership not found")
        existing = await self.get_open_role_vacation(membership_id)
        if existing:
            return int(existing["id"]), False
        settings = await self.get_guild_settings(int(membership["guild_id"]))
        if not settings:
            raise DomainError("Guild settings not found")
        starts_at = starts_at.astimezone(ZoneInfo("UTC"))
        local_day = starts_at.astimezone(ZoneInfo(settings.timezone)).date()
        now = iso(utcnow())
        uncertain = 0 if source == "role_event" else 1
        vacation_id = await self.db.execute(
            """
            INSERT INTO role_vacations(
                membership_id, role_id, starts_on, ends_on, opened_at, closed_at, source, created_at,
                starts_at, ends_at, sync_uncertain
            ) VALUES (?, ?, ?, NULL, ?, NULL, ?, ?, ?, NULL, ?)
            """,
            (membership_id, role_id, local_day.isoformat(), now, source, now, iso(starts_at), uncertain),
        )
        await self.audit(
            int(membership["guild_id"]), actor_user_id, "vacation_role.opened", "role_vacation", vacation_id,
            {
                "membership_id": membership_id,
                "role_id": role_id,
                "starts_at": iso(starts_at),
                "source": source,
                "sync_uncertain": bool(uncertain),
            },
        )
        return vacation_id, True

    async def close_role_vacation(
        self,
        *,
        membership_id: int,
        ends_at: datetime,
        source: str,
        actor_user_id: int | None = None,
    ) -> bool:
        if source not in {"role_event", "startup_sync", "periodic_sync", "manual_sync"}:
            raise DomainError("Invalid vacation sync source")
        row = await self.get_open_role_vacation(membership_id)
        if not row:
            return False
        membership = await self.get_membership(membership_id)
        if not membership:
            return False
        settings = await self.get_guild_settings(int(membership["guild_id"]))
        if not settings:
            return False
        ends_at = ends_at.astimezone(ZoneInfo("UTC"))
        start_exact = parse_iso(row["starts_at"]) if row["starts_at"] else None
        if start_exact and ends_at < start_exact:
            ends_at = start_exact
        local_day = ends_at.astimezone(ZoneInfo(settings.timezone)).date()
        now = iso(utcnow())
        uncertain = int(row["sync_uncertain"] or 0) or (0 if source == "role_event" else 1)
        await self.db.execute(
            """
            UPDATE role_vacations
            SET ends_on=?, ends_at=?, closed_at=?, sync_uncertain=?
            WHERE id=? AND ends_on IS NULL
            """,
            (local_day.isoformat(), iso(ends_at), now, uncertain, int(row["id"])),
        )
        await self.audit(
            int(membership["guild_id"]), actor_user_id, "vacation_role.closed", "role_vacation", int(row["id"]),
            {
                "membership_id": membership_id,
                "ends_at": iso(ends_at),
                "source": source,
                "sync_uncertain": bool(uncertain),
            },
        )
        return True

    async def close_all_open_role_vacations_for_guild(
        self,
        guild_id: int,
        *,
        ends_at: datetime,
        source: str = "manual_sync",
        actor_user_id: int | None = None,
    ) -> int:
        """Close every currently open external-role vacation for a guild.

        Used when the configured vacation role changes. Keeping the old role period
        open would make a person appear on vacation forever or silently carry an old
        role into a new policy. Each row is closed through the normal audited path.
        """
        rows = await self.list_open_role_vacations(guild_id)
        closed = 0
        for row in rows:
            if await self.close_role_vacation(
                membership_id=int(row["membership_id"]),
                ends_at=ends_at,
                source=source,
                actor_user_id=actor_user_id,
            ):
                closed += 1
        return closed

    async def list_open_role_vacations(self, guild_id: int):
        return await self.db.fetchall(
            """
            SELECT rv.*, p.nickname, p.static_id, p.discord_user_id, m.rank
            FROM role_vacations rv
            JOIN memberships m ON m.id=rv.membership_id
            JOIN people p ON p.id=m.person_id
            WHERE m.guild_id=? AND m.status='active' AND rv.ends_on IS NULL
            ORDER BY rv.starts_on, p.nickname COLLATE NOCASE
            """,
            (guild_id,),
        )

    async def attendance_for_guild_period(self, guild_id: int, start: datetime, end: datetime):
        return await self.db.fetchall(
            """
            SELECT at.*, a.scheduled_for, a.category, a.is_spontaneous, a.analytical,
                   a.status AS activity_status, a.title, a.ended_at,
                   p.nickname, p.static_id, p.discord_user_id
            FROM attendance at
            JOIN activities a ON a.id=at.activity_id
            JOIN memberships m ON m.id=at.membership_id
            JOIN people p ON p.id=m.person_id
            WHERE a.guild_id=? AND a.scheduled_for>=? AND a.scheduled_for<?
              AND a.status IN ('closed','finalized') AND at.removed_at IS NULL
            ORDER BY a.scheduled_for
            """,
            (guild_id, iso(start), iso(end)),
        )

    async def custom_audience_for_activities(self, activity_ids: Iterable[int]):
        ids = sorted(set(int(x) for x in activity_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return await self.db.fetchall(
            f"SELECT activity_id, membership_id FROM activity_audience_members WHERE activity_id IN ({placeholders})",
            ids,
        )

    async def latest_attendance_for_membership(self, membership_id: int):
        return await self.db.fetchone(
            """
            SELECT a.*, at.first_seen_at
            FROM attendance at
            JOIN activities a ON a.id=at.activity_id
            WHERE at.membership_id=? AND at.removed_at IS NULL
              AND a.status IN ('closed','finalized')
            ORDER BY a.scheduled_for DESC LIMIT 1
            """,
            (membership_id,),
        )

    async def recent_audit(self, guild_id: int, limit: int = 50):
        return await self.db.fetchall(
            "SELECT * FROM audit_log WHERE guild_id=? ORDER BY created_at DESC LIMIT ?",
            (guild_id, limit),
        )

    async def rank_history_for_memberships(self, membership_ids: Iterable[int]):
        ids = sorted(set(int(x) for x in membership_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return await self.db.fetchall(
            f"""
            SELECT * FROM rank_history
            WHERE membership_id IN ({placeholders})
            ORDER BY membership_id, changed_at
            """,
            ids,
        )

    async def add_custom_audience_member(
        self, activity_id: int, membership_id: int, actor_user_id: int
    ) -> None:
        activity = await self.get_activity(activity_id)
        if not activity:
            raise DomainError("Activity not found")
        if activity["status"] != "scheduled":
            raise DomainError("Audience can only be changed before activity starts")
        if activity["audience_type"] != "custom":
            raise DomainError("Activity does not use custom audience")
        member = await self.get_membership(membership_id)
        if not member or int(member["guild_id"]) != int(activity["guild_id"]) or member["status"] != "active":
            raise DomainError("Active family member not found")
        await self.db.execute(
            "INSERT OR IGNORE INTO activity_audience_members(activity_id, membership_id) VALUES (?, ?)",
            (activity_id, membership_id),
        )
        await self.audit(
            int(activity["guild_id"]), actor_user_id, "activity.audience_member_added", "activity", activity_id,
            {"membership_id": membership_id},
        )

    async def cancel_activity(self, activity_id: int, actor_user_id: int, reason: str) -> None:
        activity = await self.get_activity(activity_id)
        if not activity:
            raise DomainError("Activity not found")
        if activity["status"] not in {"scheduled", "running"}:
            raise DomainError("Activity cannot be cancelled")
        now = iso(utcnow())
        def tx(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE registration_windows SET closed_at=? WHERE activity_id=? AND closed_at IS NULL",
                (now, activity_id),
            )
            conn.execute(
                "UPDATE activities SET status='cancelled', closing_note=?, updated_at=? WHERE id=?",
                (reason.strip(), now, activity_id),
            )
        await self.db.transaction(tx)
        await self.audit(
            int(activity["guild_id"]), actor_user_id, "activity.cancelled", "activity", activity_id,
            {"reason": reason},
        )

    async def list_configured_guilds(self):
        return await self.db.fetchall(
            "SELECT * FROM guild_settings WHERE family_role_id IS NOT NULL AND staff_role_id IS NOT NULL AND leader_role_id IS NOT NULL"
        )

    async def mark_weekly_report_posted(
        self, report_id: int, channel_id: int, message_id: int
    ) -> None:
        await self.db.execute(
            "UPDATE weekly_reports SET posted_channel_id=?, posted_message_id=? WHERE id=?",
            (channel_id, message_id, report_id),
        )

    async def bulk_add_members(
        self,
        *,
        guild_id: int,
        members: list[dict[str, Any]],
        actor_user_id: int,
    ) -> list[int]:
        """Atomically import multiple active members.

        Expected keys per row: discord_user_id, static_id, nickname, rank, joined_at.
        Any conflict rolls back the entire import.
        """
        if not members:
            raise DomainError("Import is empty")

        static_ids = [str(m["static_id"]).strip() for m in members]
        discord_ids = [int(m["discord_user_id"]) for m in members]
        if len(set(static_ids)) != len(static_ids):
            raise DomainError("CSV contains duplicate Static IDs")
        if len(set(discord_ids)) != len(discord_ids):
            raise DomainError("CSV contains duplicate Discord IDs")
        if any(not sid for sid in static_ids):
            raise DomainError("Static ID cannot be empty")
        if any(not str(m["nickname"]).strip() for m in members):
            raise DomainError("Nickname cannot be empty")
        if any(int(m["rank"]) < 0 for m in members):
            raise DomainError("Rank cannot be negative")
        if any(m["joined_at"] > utcnow() + timedelta(minutes=5) for m in members):
            raise DomainError("Join date cannot be in the future")

        now = iso(utcnow())

        def tx(conn: sqlite3.Connection) -> list[int]:
            result: list[int] = []
            for item in members:
                discord_user_id = int(item["discord_user_id"])
                static_id = str(item["static_id"]).strip()
                nickname = str(item["nickname"]).strip()
                rank = int(item["rank"])
                joined_at = item["joined_at"]
                if not isinstance(joined_at, datetime):
                    raise DomainError("joined_at must be datetime")

                person = conn.execute(
                    "SELECT * FROM people WHERE guild_id=? AND static_id=?",
                    (guild_id, static_id),
                ).fetchone()
                discord_owner = conn.execute(
                    "SELECT * FROM people WHERE guild_id=? AND discord_user_id=?",
                    (guild_id, discord_user_id),
                ).fetchone()
                if person and discord_owner and int(person["id"]) != int(discord_owner["id"]):
                    raise DomainError(
                        f"Discord ID {discord_user_id} is linked to another Static ID"
                    )
                if not person and discord_owner:
                    raise DomainError(
                        f"Discord ID {discord_user_id} is already linked to Static ID {discord_owner['static_id']}"
                    )
                if person:
                    active = conn.execute(
                        "SELECT id FROM memberships WHERE guild_id=? AND person_id=? AND status='active'",
                        (guild_id, person["id"]),
                    ).fetchone()
                    if active:
                        raise DomainError(f"Static ID {static_id} is already active")
                    person_id = int(person["id"])
                    conn.execute(
                        "UPDATE people SET discord_user_id=?, nickname=?, updated_at=? WHERE id=?",
                        (discord_user_id, nickname, now, person_id),
                    )
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO people(guild_id, discord_user_id, static_id, nickname, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (guild_id, discord_user_id, static_id, nickname, now, now),
                    )
                    person_id = int(cur.lastrowid)

                cur = conn.execute(
                    """
                    INSERT INTO memberships(
                        guild_id, person_id, rank, status, joined_at,
                        created_by_user_id, created_at, updated_at
                    ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
                    """,
                    (guild_id, person_id, rank, iso(joined_at), actor_user_id, now, now),
                )
                membership_id = int(cur.lastrowid)
                conn.execute(
                    """
                    INSERT INTO rank_history(
                        membership_id, old_rank, new_rank, changed_by_user_id,
                        changed_at, reason
                    ) VALUES (?, NULL, ?, ?, ?, 'Массовый импорт состава')
                    """,
                    (membership_id, rank, actor_user_id, now),
                )
                result.append(membership_id)
            return result

        ids = await self.db.transaction(tx)
        await self.audit(
            guild_id,
            actor_user_id,
            "member.bulk_imported",
            "membership",
            None,
            {"count": len(ids), "membership_ids": ids},
        )
        return ids
