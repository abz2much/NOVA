"""Every SQLite table Nova owns, in one place.

A component is the set of tables one module owns, with the additive column
migrations that have shipped for them. A store is one physical database
file and the components that share it. The DDL below is the exact schema
each owning module used to carry itself; fresh databases come out
identical, and existing ones only ever gain columns, tables and indexes.

Column migrations are listed oldest first. `version` counts the shipped
schema steps for a component (1 = its original tables); it is what the
setup upgrade records in the store's ledger once the real schema has been
verified. Add a new step by appending a Column and bumping the version —
never by editing or removing an earlier one.

A DataStep rewrites existing rows once, when a database's ledger records an
older version of the component than the step's. It runs in the same
transaction as the column migrations, after them, and is written so that
running it again changes nothing.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    table: str
    name: str
    ddl: str


@dataclass(frozen=True)
class DataStep:
    version: int
    statements: tuple[str, ...]


@dataclass(frozen=True)
class Component:
    name: str
    version: int
    tables: tuple[str, ...]
    statements: tuple[str, ...]
    columns: tuple[Column, ...] = ()
    # Run after the column migrations, e.g. an index on an added column.
    post: tuple[str, ...] = ()
    virtual: bool = False
    data: tuple[DataStep, ...] = ()


@dataclass(frozen=True)
class Store:
    key: str
    relpath: str               # relative to Home Assistant's config directory
    components: tuple[str, ...]
    wal: bool
    # Upgraded once at setup when the file already exists. Stores whose
    # tables depend on an optional SQLite feature (FTS5) or on which memory
    # backend is in use stay lazy: their owners create them on first use.
    upgrade_at_setup: bool = True


LEDGER_TABLE = "nova_schema_components"
LEDGER_DDL = (
    "CREATE TABLE IF NOT EXISTS nova_schema_components ("
    "component TEXT PRIMARY KEY, version INTEGER NOT NULL, "
    "applied_at TEXT NOT NULL)"
)


# Mirrors cognitive.routines.ROUTINE_DOMAINS (persistence may not import it;
# test_routine_alerts.py checks they agree). None: any real state.
_PERSON_ROUTINE_STATES: dict[str, tuple[str, ...] | None] = {
    "light": ("on", "off"),
    "switch": ("on", "off"),
    "fan": ("on", "off"),
    "cover": ("open", "closed"),
    "lock": ("locked", "unlocked"),
    "climate": None,
    "media_player": ("playing",),
}


def _person_routines_v2() -> tuple[str, ...]:
    """person_patterns v2: key every stored routine as
    cognitive.routines.routine_key does, drop time routines that are not
    something a person does (trackers, sensors, helpers), then keep only the
    most recent row of each routine."""
    entity = ("COALESCE(json_extract(data, '$.entity_id'), "
              "substr(description, 1, instr(description, ' ') - 1))")
    dom = "substr(routine_key, 1, instr(routine_key, '.') - 1)"
    tail = "substr(routine_key, instr(routine_key, '|') + 1)"
    state = f"lower(substr({tail}, 1, instr({tail}, '|') - 1))"
    allowed = []
    for domain, states in _PERSON_ROUTINE_STATES.items():
        if states is None:
            allowed.append(f"({dom} = '{domain}' AND {state} NOT IN "
                           "('', 'unknown', 'unavailable', 'none'))")
        else:
            listed = ", ".join(f"'{st}'" for st in states)
            allowed.append(f"({dom} = '{domain}' AND {state} IN ({listed}))")
    return (
        f"""UPDATE person_patterns
SET routine_key = {entity} || '|' || json_extract(data, '$.state') || '|'
                  || CAST(json_extract(data, '$.hour') AS INTEGER)
WHERE (routine_key IS NULL OR routine_key = '')
  AND pattern_type = 'time_routine' AND json_valid(data)
  AND json_extract(data, '$.state') IS NOT NULL
  AND json_extract(data, '$.hour') IS NOT NULL
  AND instr({entity}, '.') > 1""",
        """UPDATE person_patterns
SET routine_key = 'cmd|' || json_extract(data, '$.command') || '|'
                  || CAST(json_extract(data, '$.hour') AS INTEGER)
WHERE (routine_key IS NULL OR routine_key = '')
  AND pattern_type = 'repeated_command' AND json_valid(data)
  AND json_extract(data, '$.command') IS NOT NULL
  AND json_extract(data, '$.hour') IS NOT NULL""",
        "DELETE FROM person_patterns WHERE pattern_type = 'time_routine' AND ("
        "routine_key IS NULL OR routine_key = '' OR NOT ("
        + " OR ".join(allowed) + "))",
        """DELETE FROM person_patterns
WHERE routine_key IS NOT NULL AND routine_key != '' AND EXISTS (
  SELECT 1 FROM person_patterns AS newer
  WHERE newer.person = person_patterns.person
    AND newer.pattern_type = person_patterns.pattern_type
    AND newer.routine_key = person_patterns.routine_key
    AND (COALESCE(newer.last_seen, '') > COALESCE(person_patterns.last_seen, '')
         OR (COALESCE(newer.last_seen, '') = COALESCE(person_patterns.last_seen, '')
             AND newer.id > person_patterns.id)))""",
    )


_PERSON_ROUTINES_V2 = _person_routines_v2()


_COMPONENTS = (
    # ── nova/conversations.db ────────────────────────────────────────────
    Component(
        name="conversations",
        version=2,
        tables=("conversations", "sentinel_events", "activity_log"),
        statements=(
            """CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    device_id   TEXT    NOT NULL DEFAULT 'unknown',
    role        TEXT    NOT NULL CHECK(role IN ('user','assistant')),
    content     TEXT    NOT NULL
)""",
            "CREATE INDEX IF NOT EXISTS idx_timestamp ON conversations(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_device    ON conversations(device_id)",
            """CREATE TABLE IF NOT EXISTS sentinel_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    entity_id   TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,
    detail      TEXT
)""",
            """CREATE TABLE IF NOT EXISTS activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    entity_id   TEXT    NOT NULL DEFAULT '',
    category    TEXT    NOT NULL DEFAULT 'other',
    urgency     TEXT    NOT NULL DEFAULT 'low',
    message     TEXT    NOT NULL DEFAULT '',
    was_spoken  INTEGER NOT NULL DEFAULT 0,
    source      TEXT    NOT NULL DEFAULT 'observer'
)""",
            "CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_log(timestamp)",
        ),
        # Person-scoped episodic continuity. No backfill: old rows read NULL.
        columns=(Column("conversations", "subject", "TEXT"),),
    ),
    Component(
        name="action_log",
        version=1,
        tables=("action_log",),
        statements=(
            """CREATE TABLE IF NOT EXISTS action_log (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id            TEXT    NOT NULL,
    ts_created            REAL    NOT NULL,
    ts_updated            REAL    NOT NULL,
    action                TEXT    NOT NULL,
    source                TEXT    NOT NULL,
    requested_by_user_id  TEXT,
    requested_by_name     TEXT,
    request_device_id     TEXT,
    domain                TEXT,
    service               TEXT,
    entity_id             TEXT,
    requested_state       TEXT,
    approval_required     INTEGER NOT NULL DEFAULT 0,
    approval_result       TEXT    NOT NULL DEFAULT 'not_required',
    execution_result      TEXT    NOT NULL DEFAULT 'pending',
    reason_code           TEXT,
    reason_text           TEXT
)""",
            "CREATE INDEX IF NOT EXISTS idx_action_log_keyset  ON action_log(ts_created, id)",
            "CREATE INDEX IF NOT EXISTS idx_action_log_request ON action_log(request_id)",
        ),
    ),
    Component(
        name="spoken_history",
        version=2,
        tables=("spoken_history",),
        statements=(
            """CREATE TABLE IF NOT EXISTS spoken_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      REAL    NOT NULL,
    text           TEXT    NOT NULL,
    source         TEXT    NOT NULL,
    speakers       TEXT    NOT NULL,
    delivery_state TEXT    NOT NULL DEFAULT 'sent',
    repeat_of_id   INTEGER
)""",
            "CREATE INDEX IF NOT EXISTS idx_spoken_history_ts ON spoken_history(timestamp)",
        ),
        # Links a spoken row to the action request it narrates; NULL on old rows.
        columns=(Column("spoken_history", "action_request_id", "TEXT"),),
    ),
    # ── nova/knowledge.db ────────────────────────────────────────────────
    Component(
        name="facts",
        version=2,
        tables=("facts",),
        statements=(
            """CREATE TABLE IF NOT EXISTS facts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL DEFAULT 'fact',
    subject         TEXT NOT NULL DEFAULT 'household',
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'stated',
    confidence      REAL NOT NULL DEFAULT 1.0,
    salience        REAL NOT NULL DEFAULT 1.0,
    status          TEXT NOT NULL DEFAULT 'confirmed',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    last_referenced REAL,
    expires_at      REAL,
    UNIQUE(subject, key)
)""",
            "CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject)",
        ),
        # v7.88.0: every fact stored before pending facts existed was trusted,
        # so it reads back as 'confirmed'. Rows that already carry a status
        # keep it exactly.
        columns=(Column("facts", "status", "TEXT NOT NULL DEFAULT 'confirmed'"),),
    ),
    Component(
        name="relations",
        version=1,
        tables=("relations",),
        statements=(
            # Links between things ("sam owns car.jeep"). Every row starts
            # 'pending' and is only used once a person confirms it. A removed
            # row is kept with deleted_at set, so a later non stated write
            # cannot bring back an edge the user took out. The table is new
            # in 8.7.13: an existing knowledge.db only gains it, and the
            # facts tables are untouched.
            """CREATE TABLE IF NOT EXISTS relations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subject     TEXT NOT NULL,
    predicate   TEXT NOT NULL,
    object      TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'stated'
                CHECK (source IN ('stated', 'observed', 'inferred')),
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'confirmed')),
    confidence  REAL NOT NULL DEFAULT 1.0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    deleted_at  REAL,
    UNIQUE(subject, predicate, object)
)""",
            "CREATE INDEX IF NOT EXISTS idx_relations_subject ON relations(subject)",
            "CREATE INDEX IF NOT EXISTS idx_relations_object ON relations(object)",
        ),
    ),
    Component(
        name="fact_vectors",
        version=1,
        tables=("fact_vectors",),
        statements=(
            "CREATE TABLE IF NOT EXISTS fact_vectors ("
            "fact_id INTEGER PRIMARY KEY, model TEXT NOT NULL, "
            "dim INTEGER NOT NULL, vec BLOB NOT NULL, "
            "text_hash TEXT NOT NULL, updated_at REAL NOT NULL)",
            # A fact that is forgotten or expires takes its vector with it.
            "CREATE TRIGGER IF NOT EXISTS trg_fact_vectors_cleanup "
            "AFTER DELETE ON facts BEGIN "
            "DELETE FROM fact_vectors WHERE fact_id = OLD.id; END",
        ),
    ),
    # ── nova/patterns.db ─────────────────────────────────────────────────
    Component(
        name="pattern_log",
        version=6,
        tables=("state_changes", "commands", "suggestions"),
        statements=(
            """CREATE TABLE IF NOT EXISTS state_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    old_state TEXT,
    new_state TEXT NOT NULL,
    area_id TEXT,
    hour INTEGER,
    day_of_week INTEGER,
    triggered_by TEXT DEFAULT 'system',
    source_entity_id TEXT DEFAULT '',
    source_confidence REAL DEFAULT 0.0,
    person TEXT DEFAULT 'unknown',
    person_confidence REAL DEFAULT 0.0
)""",
            "CREATE INDEX IF NOT EXISTS idx_sc_entity ON state_changes(entity_id)",
            "CREATE INDEX IF NOT EXISTS idx_sc_ts ON state_changes(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_sc_hour_dow ON state_changes(hour, day_of_week)",
            """CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    text TEXT NOT NULL,
    handled_by TEXT DEFAULT 'agent',
    entity_ids TEXT DEFAULT '[]',
    person TEXT DEFAULT 'unknown',
    hour INTEGER,
    day_of_week INTEGER
)""",
            "CREATE INDEX IF NOT EXISTS idx_cmd_ts ON commands(timestamp)",
            """CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL,
    description TEXT NOT NULL,
    automation_yaml TEXT,
    status TEXT DEFAULT 'pending',
    confidence REAL DEFAULT 0.0,
    pattern_count INTEGER DEFAULT 0,
    approved_at TEXT,
    dismissed_at TEXT,
    pattern_type TEXT DEFAULT '',
    entity_ids TEXT DEFAULT '',
    details TEXT DEFAULT '{}'
)""",
        ),
        columns=(
            # v6.41.0 who; v6.77.0 how sure of who.
            Column("state_changes", "person", "TEXT DEFAULT 'unknown'"),
            Column("state_changes", "person_confidence", "REAL DEFAULT 0.0"),
            # v7.109.0: the source's own detection confidence; NULL = none given.
            Column("state_changes", "detection_confidence", "REAL DEFAULT NULL"),
            # Automation awareness: how a transition happened. Old rows stay generic.
            Column("state_changes", "source_entity_id", "TEXT DEFAULT ''"),
            Column("state_changes", "source_confidence", "REAL DEFAULT 0.0"),
            # v6.80.0: the evidence behind a suggestion.
            Column("suggestions", "pattern_type", "TEXT DEFAULT ''"),
            Column("suggestions", "entity_ids", "TEXT DEFAULT ''"),
            Column("suggestions", "details", "TEXT DEFAULT '{}'"),
        ),
        post=(
            "CREATE INDEX IF NOT EXISTS idx_sc_person ON state_changes(person)",
            "CREATE INDEX IF NOT EXISTS idx_sc_source "
            "ON state_changes(triggered_by, source_entity_id)",
        ),
    ),
    Component(
        name="person_patterns",
        version=2,
        tables=("person_patterns",),
        statements=(
            """CREATE TABLE IF NOT EXISTS person_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT NOT NULL,
    pattern_type TEXT NOT NULL,
    description TEXT NOT NULL,
    data TEXT DEFAULT '{}',
    confidence REAL DEFAULT 0.0,
    last_seen TEXT,
    occurrences INTEGER DEFAULT 1
)""",
            "CREATE INDEX IF NOT EXISTS idx_pp_person ON person_patterns(person)",
        ),
        # v2: routines are identified by what they are, not by their wording.
        columns=(Column("person_patterns", "routine_key", "TEXT"),),
        post=("CREATE INDEX IF NOT EXISTS idx_pp_key "
              "ON person_patterns(person, pattern_type, routine_key)",),
        data=(DataStep(2, _PERSON_ROUTINES_V2),),
    ),
    Component(
        name="cognition",
        version=1,
        tables=("cognition_model", "cognition_alerted"),
        statements=(
            "CREATE TABLE IF NOT EXISTS cognition_model ("
            "entity_id TEXT PRIMARY KEY, data TEXT, updated REAL)",
            "CREATE TABLE IF NOT EXISTS cognition_alerted ("
            "key TEXT PRIMARY KEY, day INTEGER)",
        ),
    ),
    Component(
        name="automation_trials",
        version=1,
        tables=("automation_trials",),
        statements=(
            """CREATE TABLE IF NOT EXISTS automation_trials (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    suggestion_id        INTEGER NOT NULL,
    automation_id        TEXT NOT NULL,
    automation_entity_id TEXT,
    installed_at         REAL NOT NULL,
    run_count            INTEGER NOT NULL DEFAULT 0,
    last_run             REAL,
    manual_outcome       TEXT,
    manual_outcome_ts    REAL
)""",
            "CREATE INDEX IF NOT EXISTS idx_at_automation_id ON automation_trials (automation_id)",
            "CREATE INDEX IF NOT EXISTS idx_at_entity_id     ON automation_trials (automation_entity_id)",
            "CREATE INDEX IF NOT EXISTS idx_at_suggestion_id ON automation_trials (suggestion_id)",
        ),
    ),
    Component(
        name="followups",
        version=1,
        tables=("followups",),
        statements=(
            """CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts TEXT NOT NULL,
    due_ts TEXT NOT NULL,
    instruction TEXT NOT NULL,
    context TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT DEFAULT ''
)""",
            "CREATE INDEX IF NOT EXISTS idx_fu_due ON followups(status, due_ts)",
        ),
    ),
    Component(
        name="goals",
        version=1,
        tables=("goals",),
        statements=(
            """CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts TEXT NOT NULL,
    updated_ts TEXT NOT NULL,
    title TEXT NOT NULL,
    outcome TEXT NOT NULL,
    steps TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    progress TEXT NOT NULL DEFAULT '[]',
    next_check_ts TEXT NOT NULL,
    check_interval_min REAL NOT NULL DEFAULT 30,
    deadline_ts TEXT,
    runs INTEGER NOT NULL DEFAULT 0,
    last_result TEXT DEFAULT ''
)""",
            "CREATE INDEX IF NOT EXISTS idx_goal_due ON goals(status, next_check_ts)",
        ),
    ),
    # ── nova/decisions.db ────────────────────────────────────────────────
    Component(
        name="decision_records",
        version=2,
        tables=("decision_records",),
        statements=(
            """CREATE TABLE IF NOT EXISTS decision_records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL NOT NULL,
    kind           TEXT NOT NULL,
    observation    TEXT NOT NULL DEFAULT '{}',
    interpretation TEXT NOT NULL DEFAULT '{}',
    evidence       TEXT NOT NULL DEFAULT '{}',
    decision       TEXT NOT NULL DEFAULT '',
    reason         TEXT NOT NULL DEFAULT '',
    model          TEXT,
    tokens         INTEGER,
    latency_ms     INTEGER,
    confidence     REAL,
    outcome        TEXT,
    outcome_ts     REAL,
    outcome_source TEXT,
    ref            TEXT
)""",
            "CREATE INDEX IF NOT EXISTS idx_dr_ts      ON decision_records (ts)",
            "CREATE INDEX IF NOT EXISTS idx_dr_kind    ON decision_records (kind)",
            "CREATE INDEX IF NOT EXISTS idx_dr_outcome ON decision_records (outcome)",
        ),
        columns=(Column("decision_records", "ref", "TEXT"),),
        # After the column: a database from before `ref` existed must gain
        # the column before its index can be built.
        post=("CREATE INDEX IF NOT EXISTS idx_dr_ref     ON decision_records (ref)",),
    ),
    # ── nova/provider_activity.db ────────────────────────────────────────
    Component(
        name="provider_activity",
        version=1,
        tables=("provider_activity_daily",),
        statements=(
            """CREATE TABLE IF NOT EXISTS provider_activity_daily (
    day            TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    role           TEXT NOT NULL,
    location       TEXT NOT NULL,
    data_category  TEXT NOT NULL,
    success_count  INTEGER NOT NULL DEFAULT 0,
    failure_count  INTEGER NOT NULL DEFAULT 0,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    latency_ms_sum INTEGER NOT NULL DEFAULT 0,
    call_count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider, model, role, location, data_category)
)""",
        ),
    ),
    # ── nova/reminders.db ────────────────────────────────────────────────
    Component(
        name="reminders",
        version=1,
        tables=("reminders",),
        statements=(
            """CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created     TEXT NOT NULL,
    label       TEXT NOT NULL,
    trigger_at  TEXT NOT NULL,  -- ISO timestamp
    repeat      TEXT,            -- 'daily', 'weekly:MON', etc. (optional)
    require_home   INTEGER NOT NULL DEFAULT 1,  -- 1 = only fire when someone home
    respect_quiet  INTEGER NOT NULL DEFAULT 1,  -- 1 = skip during quiet hours
    acknowledged   INTEGER NOT NULL DEFAULT 0,
    last_fired     TEXT
)""",
            "CREATE INDEX IF NOT EXISTS idx_trigger_at ON reminders(trigger_at)",
        ),
    ),
    # ── nova/scene_memory.db ─────────────────────────────────────────────
    Component(
        name="scene_memory",
        version=1,
        tables=("sightings",),
        statements=(
            "CREATE TABLE IF NOT EXISTS sightings ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "camera TEXT NOT NULL, area TEXT, ts REAL NOT NULL, "
            "description TEXT NOT NULL, objects TEXT NOT NULL DEFAULT '')",
            "CREATE INDEX IF NOT EXISTS idx_sightings_camera ON sightings(camera, ts)",
            "CREATE INDEX IF NOT EXISTS idx_sightings_ts ON sightings(ts)",
        ),
    ),
    # ── nova.db (keyword memory and document search) ─────────────────────
    Component(
        name="memory_fts",
        version=1,
        tables=("memory_fts",),
        statements=(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(content, metadata, timestamp)",
        ),
        virtual=True,
    ),
    Component(
        name="document_fts",
        version=1,
        tables=("document_fts",),
        statements=(
            "CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(content, source, chunk_id, ingested)",
        ),
        virtual=True,
    ),
    Component(
        name="document_watch_seen",
        version=1,
        tables=("document_watch_seen",),
        statements=(
            "CREATE TABLE IF NOT EXISTS document_watch_seen ("
            "path TEXT PRIMARY KEY, mtime REAL, ingested TEXT)",
        ),
    ),
    Component(
        name="doc_vectors",
        version=1,
        tables=("doc_vectors",),
        statements=(
            "CREATE TABLE IF NOT EXISTS doc_vectors ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  source TEXT NOT NULL,"
            "  chunk INTEGER NOT NULL,"
            "  content TEXT NOT NULL,"
            "  dim INTEGER NOT NULL,"
            "  vec BLOB NOT NULL,"
            "  model TEXT NOT NULL,"
            "  ingested TEXT NOT NULL"
            ")",
            "CREATE INDEX IF NOT EXISTS idx_doc_vectors_source ON doc_vectors(source)",
        ),
    ),
)

COMPONENTS: dict[str, Component] = {c.name: c for c in _COMPONENTS}


STORES: tuple[Store, ...] = (
    Store("conversations", "nova/conversations.db",
          ("conversations", "action_log", "spoken_history"), wal=True),
    Store("knowledge", "nova/knowledge.db", ("facts", "fact_vectors", "relations"), wal=True),
    Store("patterns", "nova/patterns.db",
          ("pattern_log", "person_patterns", "cognition", "automation_trials",
           "followups", "goals"), wal=True),
    Store("decisions", "nova/decisions.db", ("decision_records",), wal=False),
    Store("provider_activity", "nova/provider_activity.db",
          ("provider_activity",), wal=True),
    Store("reminders", "nova/reminders.db", ("reminders",), wal=False),
    Store("scene_memory", "nova/scene_memory.db", ("scene_memory",), wal=True),
    Store("nova", "nova.db",
          ("memory_fts", "document_fts", "document_watch_seen", "doc_vectors"),
          wal=False, upgrade_at_setup=False),
)
