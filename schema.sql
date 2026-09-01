-- Версия набора заданий. Меняете attributes.json — появляется новая строка,
-- старые респонденты остаются привязаны к прежней версии.
CREATE TABLE IF NOT EXISTS design (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    mode             TEXT NOT NULL,          -- 'shared' | 'individual'
    seed             INTEGER NOT NULL,
    attributes_hash  TEXT NOT NULL,
    spec_json        TEXT,
    created_at       TEXT NOT NULL
);

-- Без этого индекса несколько воркеров, стартующих одновременно на пустой
-- базе, создадут дубли дизайна, и респонденты разъедутся по разным design_id.
CREATE UNIQUE INDEX IF NOT EXISTS idx_design_key
    ON design(mode, seed, attributes_hash);

CREATE TABLE IF NOT EXISTS respondent (
    id               TEXT PRIMARY KEY,       -- UUID, он же в куке
    design_id        INTEGER REFERENCES design(id),
    seed             INTEGER,                -- личный seed для individual
    stage            TEXT NOT NULL,          -- welcome|survey|tasks|done|withdrawn
    step             INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    consent_at       TEXT,
    consent_version  TEXT,                   -- какую редакцию текста читал
    finished_at      TEXT,
    is_mobile        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS survey_answer (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    respondent_id  TEXT NOT NULL REFERENCES respondent(id),
    question_code  TEXT NOT NULL,
    value          TEXT NOT NULL,
    answered_at    TEXT NOT NULL,
    UNIQUE (respondent_id, question_code)    -- повторный ответ перезапишет старый
);

CREATE TABLE IF NOT EXISTS task (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    respondent_id   TEXT NOT NULL REFERENCES respondent(id),
    idx             INTEGER NOT NULL,        -- порядковый номер, с нуля
    profile_a_json  TEXT NOT NULL,           -- снимок характеристик, не ссылка
    profile_b_json  TEXT NOT NULL,
    swapped         INTEGER NOT NULL DEFAULT 0,
    choice          TEXT,                    -- 'A' | 'B' | 'none'
    shown_at        TEXT,
    answered_at     TEXT,
    dwell_ms        INTEGER,
    UNIQUE (respondent_id, idx)
);

CREATE INDEX IF NOT EXISTS idx_answer_resp ON survey_answer(respondent_id);
CREATE INDEX IF NOT EXISTS idx_task_resp   ON task(respondent_id, idx);
