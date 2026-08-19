from __future__ import annotations

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        r'''
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            family_role_id INTEGER,
            staff_role_id INTEGER,
            leader_role_id INTEGER,
            log_channel_id INTEGER,
            dashboard_channel_id INTEGER,
            dashboard_message_id INTEGER,
            report_channel_id INTEGER,
            timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
            notice_minutes INTEGER NOT NULL DEFAULT 90 CHECK (notice_minutes >= 0),
            newcomer_days INTEGER NOT NULL DEFAULT 7 CHECK (newcomer_days >= 0),
            member_eval_days INTEGER NOT NULL DEFAULT 28 CHECK (member_eval_days >= 7),
            min_member_opportunities INTEGER NOT NULL DEFAULT 5 CHECK (min_member_opportunities >= 1),
            weekly_min_opportunities INTEGER NOT NULL DEFAULT 2 CHECK (weekly_min_opportunities >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            discord_user_id INTEGER,
            static_id TEXT NOT NULL,
            nickname TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (guild_id, static_id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_people_guild_discord
        ON people(guild_id, discord_user_id)
        WHERE discord_user_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS memberships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE RESTRICT,
            rank INTEGER NOT NULL CHECK (rank >= 0),
            status TEXT NOT NULL CHECK (status IN ('active', 'departed')),
            joined_at TEXT NOT NULL,
            left_at TEXT,
            exit_type TEXT CHECK (exit_type IS NULL OR exit_type IN ('voluntary', 'kicked', 'other')),
            exit_reason TEXT,
            created_by_user_id INTEGER,
            ended_by_user_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_active_membership_per_person
        ON memberships(guild_id, person_id)
        WHERE status = 'active';

        CREATE INDEX IF NOT EXISTS idx_memberships_guild_status
        ON memberships(guild_id, status);

        CREATE TABLE IF NOT EXISTS rank_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            membership_id INTEGER NOT NULL REFERENCES memberships(id) ON DELETE CASCADE,
            old_rank INTEGER,
            new_rank INTEGER NOT NULL,
            changed_by_user_id INTEGER,
            changed_at TEXT NOT NULL,
            reason TEXT
        );

        CREATE TABLE IF NOT EXISTS availability (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            membership_id INTEGER NOT NULL REFERENCES memberships(id) ON DELETE CASCADE,
            day_group TEXT NOT NULL CHECK (day_group IN ('weekday', 'weekend')),
            segment TEXT NOT NULL CHECK (segment IN ('morning', 'day', 'evening', 'night', 'floating')),
            effective_from TEXT NOT NULL,
            effective_to TEXT,
            created_by_user_id INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_availability_membership_effective
        ON availability(membership_id, effective_from, effective_to);

        CREATE TABLE IF NOT EXISTS vacations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            membership_id INTEGER NOT NULL REFERENCES memberships(id) ON DELETE CASCADE,
            starts_on TEXT NOT NULL,
            ends_on TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
            requested_by_user_id INTEGER NOT NULL,
            decided_by_user_id INTEGER,
            decision_reason TEXT,
            created_at TEXT NOT NULL,
            decided_at TEXT,
            CHECK (starts_on <= ends_on)
        );

        CREATE INDEX IF NOT EXISTS idx_vacations_membership_status
        ON vacations(membership_id, status, starts_on, ends_on);

        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            category TEXT NOT NULL CHECK (category IN ('training', 'family', 'faction')),
            title TEXT NOT NULL,
            description TEXT,
            analytical INTEGER NOT NULL DEFAULT 1 CHECK (analytical IN (0, 1)),
            audience_type TEXT NOT NULL DEFAULT 'all' CHECK (audience_type IN ('all', 'rank_range', 'custom')),
            min_rank INTEGER,
            max_rank INTEGER,
            announced_at TEXT NOT NULL,
            scheduled_for TEXT NOT NULL,
            started_at TEXT,
            ended_at TEXT,
            status TEXT NOT NULL CHECK (status IN ('scheduled', 'running', 'closed', 'finalized', 'cancelled')),
            is_spontaneous INTEGER NOT NULL CHECK (is_spontaneous IN (0, 1)),
            notice_minutes INTEGER NOT NULL,
            organizer_membership_id INTEGER NOT NULL REFERENCES memberships(id) ON DELETE RESTRICT,
            evidence_url TEXT,
            closing_note TEXT,
            panel_channel_id INTEGER,
            panel_message_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_activities_guild_time
        ON activities(guild_id, scheduled_for);

        CREATE INDEX IF NOT EXISTS idx_activities_guild_status
        ON activities(guild_id, status);

        CREATE TABLE IF NOT EXISTS activity_audience_members (
            activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
            membership_id INTEGER NOT NULL REFERENCES memberships(id) ON DELETE CASCADE,
            PRIMARY KEY (activity_id, membership_id)
        );

        CREATE TABLE IF NOT EXISTS registration_windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('primary', 'late', 'control')),
            code_hash TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            closed_at TEXT,
            opened_by_user_id INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_registration_activity_open
        ON registration_windows(activity_id, expires_at, closed_at);

        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
            membership_id INTEGER NOT NULL REFERENCES memberships(id) ON DELETE RESTRICT,
            first_seen_at TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('primary_code', 'late_code', 'control_code', 'manual')),
            added_by_user_id INTEGER,
            manual_reason TEXT,
            removed_at TEXT,
            removed_by_user_id INTEGER,
            remove_reason TEXT,
            UNIQUE (activity_id, membership_id)
        );

        CREATE INDEX IF NOT EXISTS idx_attendance_membership
        ON attendance(membership_id, removed_at);

        CREATE TABLE IF NOT EXISTS attendance_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attendance_id INTEGER NOT NULL REFERENCES attendance(id) ON DELETE CASCADE,
            registration_window_id INTEGER REFERENCES registration_windows(id) ON DELETE SET NULL,
            kind TEXT NOT NULL CHECK (kind IN ('primary', 'late', 'control', 'manual')),
            checked_at TEXT NOT NULL,
            actor_user_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS weekly_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            week_start TEXT NOT NULL,
            week_end TEXT NOT NULL,
            pulse_score INTEGER NOT NULL,
            metrics_json TEXT NOT NULL,
            explanations_json TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            posted_channel_id INTEGER,
            posted_message_id INTEGER,
            UNIQUE (guild_id, week_start, week_end)
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            actor_user_id INTEGER,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_audit_guild_created
        ON audit_log(guild_id, created_at DESC);
        ''',
    ),
]

# v1.0: split public/staff dashboards and role-based vacation integration.
MIGRATIONS.append(
    (
        2,
        r'''
        ALTER TABLE guild_settings ADD COLUMN vacation_role_id INTEGER;
        ALTER TABLE guild_settings ADD COLUMN public_dashboard_channel_id INTEGER;
        ALTER TABLE guild_settings ADD COLUMN public_dashboard_message_id INTEGER;
        ALTER TABLE guild_settings ADD COLUMN staff_dashboard_channel_id INTEGER;
        ALTER TABLE guild_settings ADD COLUMN staff_dashboard_message_id INTEGER;

        CREATE TABLE IF NOT EXISTS role_vacations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            membership_id INTEGER NOT NULL REFERENCES memberships(id) ON DELETE CASCADE,
            role_id INTEGER NOT NULL,
            starts_on TEXT NOT NULL,
            ends_on TEXT,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            source TEXT NOT NULL CHECK (source IN ('role_event', 'startup_sync', 'periodic_sync', 'manual_sync')),
            created_at TEXT NOT NULL,
            CHECK (ends_on IS NULL OR starts_on <= ends_on)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_open_role_vacation_per_membership
        ON role_vacations(membership_id)
        WHERE ends_on IS NULL;

        CREATE INDEX IF NOT EXISTS idx_role_vacations_membership_period
        ON role_vacations(membership_id, starts_on, ends_on);
        ''',
    )
)

# v1.2: exact vacation-role intervals, explicit activity classification, evidence archive,
# and a dedicated confidential channel for per-activity reports.
MIGRATIONS.append(
    (
        3,
        r'''
        ALTER TABLE guild_settings ADD COLUMN activity_report_channel_id INTEGER;
        ALTER TABLE guild_settings ADD COLUMN vacation_role_cutover_at TEXT;

        ALTER TABLE role_vacations ADD COLUMN starts_at TEXT;
        ALTER TABLE role_vacations ADD COLUMN ends_at TEXT;
        ALTER TABLE role_vacations ADD COLUMN sync_uncertain INTEGER NOT NULL DEFAULT 0 CHECK (sync_uncertain IN (0, 1));

        ALTER TABLE activities ADD COLUMN classification_mode TEXT NOT NULL DEFAULT 'auto'
            CHECK (classification_mode IN ('auto', 'planned', 'spontaneous'));
        ALTER TABLE activities ADD COLUMN report_channel_id INTEGER;
        ALTER TABLE activities ADD COLUMN report_message_id INTEGER;
        ALTER TABLE activities ADD COLUMN report_posted_at TEXT;

        CREATE TABLE IF NOT EXISTS activity_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
            url TEXT NOT NULL,
            filename TEXT,
            content_type TEXT,
            added_by_user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_activity_evidence_activity
        ON activity_evidence(activity_id, created_at);

        CREATE INDEX IF NOT EXISTS idx_role_vacations_membership_exact
        ON role_vacations(membership_id, starts_at, ends_at);
        ''',
    )
)

# v1.3: Academy/Main role groups with historical intervals, group-aware activity
# audiences and cleaner per-activity report layout metadata.
MIGRATIONS.append(
    (
        4,
        r'''
        ALTER TABLE guild_settings ADD COLUMN academy_role_id INTEGER;
        ALTER TABLE guild_settings ADD COLUMN main_role_id INTEGER;
        ALTER TABLE guild_settings ADD COLUMN group_role_cutover_at TEXT;

        ALTER TABLE activities ADD COLUMN audience_group TEXT
            CHECK (audience_group IS NULL OR audience_group IN ('academy', 'main'));
        ALTER TABLE activities ADD COLUMN report_data_message_id INTEGER;

        ALTER TABLE activity_evidence ADD COLUMN mirrored_url TEXT;
        ALTER TABLE activity_evidence ADD COLUMN mirror_channel_id INTEGER;
        ALTER TABLE activity_evidence ADD COLUMN mirror_message_id INTEGER;

        CREATE TABLE IF NOT EXISTS membership_group_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            membership_id INTEGER NOT NULL REFERENCES memberships(id) ON DELETE CASCADE,
            group_name TEXT NOT NULL CHECK (group_name IN ('academy', 'main', 'unclassified', 'conflict')),
            starts_at TEXT NOT NULL,
            ends_at TEXT,
            source TEXT NOT NULL CHECK (source IN ('role_event', 'startup_sync', 'periodic_sync', 'manual_sync', 'setup_sync')),
            sync_uncertain INTEGER NOT NULL DEFAULT 0 CHECK (sync_uncertain IN (0, 1)),
            created_at TEXT NOT NULL,
            CHECK (ends_at IS NULL OR starts_at <= ends_at)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_open_membership_group_period
        ON membership_group_periods(membership_id)
        WHERE ends_at IS NULL;

        CREATE INDEX IF NOT EXISTS idx_membership_group_periods_lookup
        ON membership_group_periods(membership_id, starts_at, ends_at);
        ''',
    )
)
