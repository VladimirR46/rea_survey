"""Опрос студентов: драйверы и барьеры использования российских ЛЛМ.
РЭУ им. Г.В. Плеханова.

Поток: согласие -> анкета -> инструкция -> пример -> задания выбора
(три сервиса) -> благодарность. Состояние респондента хранится в БД
(stage и step), в куке только UUID.
"""

import csv
import hashlib
import hmac
import io
import json
import os
import random
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone

from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for)

import admin

from db import DB_PATH, get_db, init_db
from design.generator import generate

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)


# ─────────────────────────────  конфигурация  ─────────────────────────────

app.secret_key = os.environ.get("SECRET_KEY", "dev-only-not-for-production")

# На том же домене живёт опрос врачей со своей кукой «session».
# Без отдельного имени и пути куки двух опросов затирали бы друг друга.
SCRIPT_NAME = os.environ.get("SCRIPT_NAME", "").rstrip("/")
app.config.update(
    SESSION_COOKIE_NAME="students_session",
    SESSION_COOKIE_PATH=SCRIPT_NAME or "/",
    PERMANENT_SESSION_LIFETIME=timedelta(days=int(os.environ.get("SESSION_DAYS", "14"))),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SEND_FILE_MAX_AGE_DEFAULT=0,
    TEMPLATES_AUTO_RELOAD=True,
)

# Правите текст согласия — увеличивайте версию
CONSENT_VERSION = "v1"

with open(os.path.join(BASE_DIR, "design/questions.json"), encoding="utf-8") as f:
    QUESTIONS = json.load(f)["questions"]

with open(os.path.join(BASE_DIR, "design/attributes.json"), encoding="utf-8") as f:
    ATTRIBUTES = json.load(f)["attributes"]

OTHER_SUFFIX = "_other"
OTHER_MAX_LEN = 100

DESIGN_MODE = os.environ.get("DESIGN_MODE", "individual")
N_TASKS = int(os.environ.get("N_TASKS", "10"))
N_REPEATS = int(os.environ.get("N_REPEATS", "2"))
DESIGN_SEED = int(os.environ.get("DESIGN_SEED", "20261109"))

# Сервисов в одном задании. Буквы — внутренние коды профилей,
# подписи — то, что видит респондент в колонках слева направо.
N_ALTS = 3
LETTERS = "ABC"
COLUMN_LABELS = ("Сервис А", "Сервис Б", "Сервис В")

# Сценарий выбора: один для всех заданий
SCENARIO = ("Представьте, что вы выбираете ЛЛМ для подготовки к экзамену: "
            "вам нужно разобраться в учебном материале и решить "
            "тренировочные задания.")
SCENARIO_SHORT = ("Подготовка к экзамену: нужно разобраться в учебном материале "
                  "и решить тренировочные задания")

RESULTS_KEY = os.environ.get("RESULTS_KEY", "")
ADMIN_HOURS = 12

# Пример перед заданиями — ровно таблица из описания исследования.
# Здесь, а не в attributes.json: правка того файла дала бы новый design_id.
EXAMPLE_SET = (
    {"accuracy": "chatgpt", "explanations": "claude", "time_saving": "alice",
     "cost": "deepseek", "access": "gigachat", "privacy": "claude"},
    {"accuracy": "deepseek", "explanations": "chatgpt", "time_saving": "claude",
     "cost": "gigachat", "access": "alice", "privacy": "gigachat"},
    {"accuracy": "gigachat", "explanations": "alice", "time_saving": "deepseek",
     "cost": "chatgpt", "access": "claude", "privacy": "deepseek"},
)


# ─────────────────────────────  утилиты  ─────────────────────────────

def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def is_mobile(user_agent):
    """Полный user-agent не сохраняем — это отпечаток устройства."""
    ua = (user_agent or "").lower()
    return int(any(k in ua for k in ("mobile", "android", "iphone", "ipad")))


def current_respondent():
    rid = session.get("rid")
    if not rid:
        return None
    return get_db().execute(
        "SELECT * FROM respondent WHERE id = ?", (rid,)
    ).fetchone()


def _set_step(rid, step, stage=None):
    db = get_db()
    if stage:
        db.execute("UPDATE respondent SET step = ?, stage = ? WHERE id = ?",
                   (step, stage, rid))
    else:
        db.execute("UPDATE respondent SET step = ? WHERE id = ?", (step, rid))
    db.commit()


def plural_ru(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


# ─────────────────────────────  дизайн заданий  ─────────────────────────────

def ensure_design():
    """Находит или создаёт строку design под текущие настройки."""
    raw = json.dumps(ATTRIBUTES, ensure_ascii=False, sort_keys=True)
    attrs_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]

    spec = {"attributes": ATTRIBUTES}
    if DESIGN_MODE == "shared":
        spec["tasks"] = generate(ATTRIBUTES, DESIGN_SEED, N_TASKS, N_ALTS)

    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """INSERT INTO design (mode, seed, attributes_hash, spec_json, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (mode, seed, attributes_hash) DO NOTHING""",
            (DESIGN_MODE, DESIGN_SEED, attrs_hash,
             json.dumps(spec, ensure_ascii=False), now()),
        )
        conn.commit()
        row = conn.execute(
            """SELECT id, spec_json FROM design
               WHERE mode = ? AND seed = ? AND attributes_hash = ?""",
            (DESIGN_MODE, DESIGN_SEED, attrs_hash),
        ).fetchone()
        return row["id"], json.loads(row["spec_json"])
    finally:
        conn.close()


def seed_for(rid):
    return int(hashlib.sha256(rid.encode()).hexdigest()[:8], 16)


def materialize_tasks(rid):
    """Записывает все задания респондента в таблицу task один раз,
    чтобы обновление страницы не меняло показанный набор."""
    db = get_db()
    if db.execute("SELECT 1 FROM task WHERE respondent_id = ? LIMIT 1",
                  (rid,)).fetchone():
        return

    seed = seed_for(rid)
    rng = random.Random(seed)

    if DESIGN_MODE == "shared":
        sets = [list(t) for t in DESIGN_SPEC["tasks"]]
    else:
        sets = generate(DESIGN_SPEC["attributes"], seed, N_TASKS, N_ALTS)
    rng.shuffle(sets)

    def insert(i, profiles, order, repeat_of=None):
        db.execute(
            """INSERT INTO task (respondent_id, idx, profiles_json,
                                 display_order, repeat_of)
               VALUES (?, ?, ?, ?, ?)""",
            (rid, i, json.dumps(profiles, ensure_ascii=False), order, repeat_of),
        )

    # в каком порядке профили A, B, C стоят на экране
    orders = ["".join(rng.sample(LETTERS, N_ALTS)) for _ in sets]
    for i, profiles in enumerate(sets):
        insert(i, profiles, orders[i])

    # Повторы: тот же набор, но каждый сервис переезжает в другую колонку
    # (циклический сдвиг). Последнее задание не берём, чтобы повтор
    # не шёл сразу за оригиналом.
    n = len(sets)
    pool = list(range(n - 1))
    for j, orig in enumerate(rng.sample(pool, min(N_REPEATS, len(pool)))):
        shift = rng.randint(1, N_ALTS - 1)
        order = orders[orig][shift:] + orders[orig][:shift]
        insert(n + j, sets[orig], order, repeat_of=orig)

    db.execute("UPDATE respondent SET design_id = ?, seed = ? WHERE id = ?",
               (DESIGN_ID, seed, rid))
    db.commit()


def level_view(code, value):
    lvl = LEVELS.get((code, value), {"label": value})
    label, em = lvl["label"], lvl.get("em")
    # «Как у <b>ChatGPT</b>»: бренд выделяется, остальное — обычным текстом
    if em and label.endswith(em):
        return {"pre": label[: -len(em)], "em": em}
    return {"pre": "", "em": label}


def build_table(columns):
    """columns — профили в порядке колонок на экране.
    Расшифровка — из снимка дизайна, а не из текущего attributes.json."""
    rows = []
    for attr in DESIGN_SPEC["attributes"]:
        rows.append({
            "name": attr["name"],
            "icon": attr.get("icon", ""),
            "description": attr.get("description", ""),
            "cells": [level_view(attr["code"], p.get(attr["code"])) for p in columns],
        })
    return rows


def task_columns(row):
    profiles = dict(zip(LETTERS, json.loads(row["profiles_json"])))
    return [profiles[letter] for letter in row["display_order"]]


# ─────────────────────────────  логика анкеты  ─────────────────────────────

def is_grid(q):
    return q.get("type") == "grid"


def grid_code(q, row):
    return f'{q["code"]}_{row["value"]}'


def get_answers(rid):
    rows = get_db().execute(
        "SELECT question_code, value FROM survey_answer WHERE respondent_id = ?",
        (rid,),
    ).fetchall()
    return {r["question_code"]: r["value"] for r in rows}


def is_skipped(question, answers):
    cond = question.get("skip_if")
    if not cond:
        return False
    value = answers.get(cond["question"])
    if isinstance(cond["value"], list):
        return value in cond["value"]
    return value == cond["value"]


def next_visible(idx, answers):
    while idx < len(QUESTIONS):
        if not is_skipped(QUESTIONS[idx], answers):
            return idx
        idx += 1
    return None


def prev_visible(idx, answers):
    idx -= 1
    while idx >= 0:
        if not is_skipped(QUESTIONS[idx], answers):
            return idx
        idx -= 1
    return None


def other_option(question):
    for opt in question.get("options", []):
        if opt.get("other"):
            return opt
    return None


def clean_other_text(raw):
    return " ".join((raw or "").split())[:OTHER_MAX_LEN]


def answer_codes(q):
    """Все коды, под которыми в БД лежат ответы этого вопроса."""
    if is_grid(q):
        return [grid_code(q, r) for r in q["rows"]]
    codes = [q["code"]]
    if other_option(q):
        codes.append(q["code"] + OTHER_SUFFIX)
    return codes


def prune_hidden_answers(rid):
    """Удаляет ответы на вопросы, ставшие ненужными после смены
    ветвящего ответа, и текст «другого», если выбран обычный вариант."""
    db = get_db()
    while True:
        answers = get_answers(rid)
        drop = set()
        for q in QUESTIONS:
            if is_skipped(q, answers):
                drop.update(c for c in answer_codes(q) if c in answers)
                continue
            other_code = q["code"] + OTHER_SUFFIX
            if other_code in answers:
                opt = other_option(q)
                if opt is None or answers.get(q["code"]) != opt["value"]:
                    drop.add(other_code)
        if not drop:
            return answers
        db.executemany(
            "DELETE FROM survey_answer WHERE respondent_id = ? AND question_code = ?",
            [(rid, code) for code in sorted(drop)],
        )
        db.commit()


def progress(idx, answers):
    visible = [i for i, q in enumerate(QUESTIONS) if not is_skipped(q, answers)]
    pos = sum(1 for i in visible if i <= idx) or 1
    return pos, len(visible)


def journey(phase, pos=1, total=1):
    """Общий прогресс для полоски в шапке: анкета — первая четверть,
    инструкция — до 35%, задания — остальное."""
    if phase == "survey":
        pct = 25 * (pos - 1) / max(total, 1)
    elif phase == "intro":
        pct = 25 + 5 * (pos - 1)
    elif phase == "tasks":
        pct = 35 + 65 * (pos - 1) / max(total, 1)
    else:
        pct = 100
    return {"phase": phase, "pct": round(pct, 1)}


def finish_all(rid):
    db = get_db()
    db.execute("UPDATE respondent SET finished_at = ? WHERE id = ? "
               "AND finished_at IS NULL", (now(), rid))
    db.commit()
    _set_step(rid, 0, stage="done")
    return redirect(url_for("thanks"))


def redirect_to_stage(r):
    """Единственное место, которое решает, где респонденту сейчас место."""
    stage = r["stage"]
    if stage == "survey":
        return redirect(url_for("survey", idx=r["step"]))
    if stage == "intro":
        if r["step"] == 1:
            return redirect(url_for("tasks_example"))
        return redirect(url_for("tasks_intro"))
    if stage == "tasks":
        return redirect(url_for("task", idx=r["step"]))
    return redirect(url_for("thanks"))


def finish_survey(rid):
    materialize_tasks(rid)
    _set_step(rid, 0, stage="intro")
    return redirect(url_for("tasks_intro"))


def task_total(rid):
    return get_db().execute(
        "SELECT COUNT(*) c FROM task WHERE respondent_id = ?", (rid,)
    ).fetchone()["c"]


# ─────────────────────────────  выгрузка  ─────────────────────────────

def survey_columns():
    cols = []
    for q in QUESTIONS:
        cols += answer_codes(q)
    return cols


def export_csv():
    """Строка на задание. Ответы анкеты повторяются в каждой строке
    респондента; кто не дошёл до заданий — одной строкой."""
    db = get_db()
    q_cols = survey_columns()

    answers = {}
    for a in db.execute("SELECT respondent_id, question_code, value FROM survey_answer"):
        answers.setdefault(a["respondent_id"], {})[a["question_code"]] = a["value"]

    rows = db.execute(
        """SELECT r.id, r.stage, r.created_at, r.consent_at, r.consent_version,
                  r.finished_at, r.is_mobile, r.design_id,
                  t.idx, t.profiles_json, t.display_order, t.repeat_of,
                  t.choice, t.shown_at, t.answered_at, t.dwell_ms
           FROM respondent r LEFT JOIN task t ON t.respondent_id = r.id
           ORDER BY r.created_at, r.id, t.idx"""
    ).fetchall()

    attr_codes = [a["code"] for a in DESIGN_SPEC["attributes"]]
    parsed = []
    for r in rows:
        profs = json.loads(r["profiles_json"]) if r["profiles_json"] else []
        for p in profs:
            attr_codes += [c for c in p if c not in attr_codes]
        parsed.append((r, profs))

    choice_of = {(r["id"], r["idx"]): r["choice"] for r in rows if r["idx"] is not None}

    out = io.StringIO()
    w = csv.writer(out)
    alt_cols = [f"{L}_{c}" for L in LETTERS for c in attr_codes]
    w.writerow(
        ["respondent_id", "stage", "created_at", "consent_at", "consent_version",
         "finished_at", "is_mobile", "design_id"]
        + q_cols
        + ["task", "repeat_of_task", "repeat_consistent"]
        + alt_cols
        + ["order", "choice", "choice_position", "shown_at", "answered_at", "dwell_ms"]
    )

    for r, profs in parsed:
        ans = answers.get(r["id"], {})
        base = [r["id"], r["stage"], r["created_at"], r["consent_at"],
                r["consent_version"], r["finished_at"], r["is_mobile"],
                r["design_id"]] + [ans.get(c, "") for c in q_cols]

        if r["idx"] is None:
            w.writerow(base)
            continue

        by_letter = dict(zip(LETTERS, profs))
        choice = r["choice"] or ""
        # 1 / 2 / 3 — номер колонки на экране, как видел респондент
        position = r["display_order"].index(choice) + 1 if choice else ""

        consistent = ""
        if r["repeat_of"] is not None:
            orig = choice_of.get((r["id"], r["repeat_of"]))
            if choice and orig:
                consistent = int(choice == orig)

        w.writerow(
            base
            + [r["idx"] + 1,
               "" if r["repeat_of"] is None else r["repeat_of"] + 1,
               consistent]
            + [by_letter.get(L, {}).get(c, "") for L in LETTERS for c in attr_codes]
            + [r["display_order"], choice, position, r["shown_at"] or "",
               r["answered_at"] or "",
               r["dwell_ms"] if r["dwell_ms"] is not None else ""]
        )

    return "﻿" + out.getvalue()


# ─────────────────────────────  инициализация  ─────────────────────────────

init_db(app)
DESIGN_ID, DESIGN_SPEC = ensure_design()

LEVELS = {
    (attr["code"], lvl["value"]): lvl
    for attr in DESIGN_SPEC["attributes"]
    for lvl in attr["levels"]
}


@app.context_processor
def template_globals():
    return {"other_max": OTHER_MAX_LEN, "plural_ru": plural_ru,
            "column_labels": COLUMN_LABELS}


# ─────────────────────────────  маршруты  ─────────────────────────────

@app.get("/")
def index():
    r = current_respondent()
    if r is not None:
        return redirect_to_stage(r)
    return render_template("welcome.html", n_tasks=N_TASKS + N_REPEATS)


@app.post("/start")
def start():
    r = current_respondent()
    if r is not None:
        return redirect_to_stage(r)

    rid = str(uuid.uuid4())
    db = get_db()
    db.execute(
        """INSERT INTO respondent
               (id, stage, step, created_at, consent_at, consent_version, is_mobile)
           VALUES (?, 'survey', 0, ?, ?, ?, ?)""",
        (rid, now(), now(), CONSENT_VERSION,
         is_mobile(request.headers.get("User-Agent"))),
    )
    db.commit()

    session["rid"] = rid
    session.permanent = True
    return redirect(url_for("survey", idx=0))


def render_question(r, idx, answers, error=None, form=None, status=200):
    """Показ вопроса анкеты. form — то, что человек отправил, чтобы
    при ошибке не потерять уже отмеченное."""
    q = QUESTIONS[idx]
    pos, total = progress(idx, answers)
    src = form if form is not None else answers
    ctx = dict(
        question=q, idx=idx, pos=pos, total=total, error=error,
        has_prev=prev_visible(idx, answers) is not None,
        journey=journey("survey", pos, total),
    )
    if is_grid(q):
        ctx["selected"] = {row["value"]: src.get(grid_code(q, row)) for row in q["rows"]}
        return render_template("grid.html", **ctx), status
    ctx["selected"] = src.get(q["code"])
    ctx["other_value"] = src.get(q["code"] + OTHER_SUFFIX)
    return render_template("question.html", **ctx), status


@app.get("/survey/<int:idx>")
def survey(idx):
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))
    if r["stage"] != "survey":
        return redirect_to_stage(r)
    if idx != r["step"]:
        return redirect(url_for("survey", idx=r["step"]))

    answers = get_answers(r["id"])
    target = next_visible(idx, answers)
    if target is None:
        return finish_survey(r["id"])
    if target != idx:
        _set_step(r["id"], target)
        return redirect(url_for("survey", idx=target))

    return render_question(r, idx, answers)


def save_answer(db, rid, code, value):
    db.execute(
        """INSERT OR REPLACE INTO survey_answer
               (respondent_id, question_code, value, answered_at)
           VALUES (?, ?, ?, ?)""",
        (rid, code, value, now()),
    )


@app.post("/survey/<int:idx>")
def survey_submit(idx):
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))
    if r["stage"] != "survey":
        return redirect_to_stage(r)
    if idx != r["step"] or idx >= len(QUESTIONS):
        return redirect(url_for("survey", idx=r["step"]))

    q = QUESTIONS[idx]
    db = get_db()

    if request.form.get("action") == "back":
        target = prev_visible(idx, get_answers(r["id"]))
        if target is not None:
            _set_step(r["id"], target)
            return redirect(url_for("survey", idx=target))
        return redirect(url_for("survey", idx=idx))

    valid = {o["value"] for o in q["options"]}

    if is_grid(q):
        values = {grid_code(q, row): request.form.get(grid_code(q, row))
                  for row in q["rows"]}
        if any(v not in valid for v in values.values()):
            form = {k: v for k, v in values.items() if v in valid}
            return render_question(r, idx, get_answers(r["id"]),
                                   error="Отметьте вариант в каждой строке",
                                   form=form, status=400)
        for code, value in values.items():
            save_answer(db, r["id"], code, value)
    else:
        value = request.form.get("value")
        other_text = clean_other_text(request.form.get("other_text"))
        form = {q["code"]: value, q["code"] + OTHER_SUFFIX: other_text}
        if value not in valid:
            return render_question(r, idx, get_answers(r["id"]),
                                   error="Выберите один из вариантов",
                                   form=form, status=400)
        chosen = next(o for o in q["options"] if o["value"] == value)
        if chosen.get("other") and not other_text:
            return render_question(r, idx, get_answers(r["id"]),
                                   error="Уточните ваш вариант в поле ввода",
                                   form=form, status=400)
        save_answer(db, r["id"], q["code"], value)
        other_code = q["code"] + OTHER_SUFFIX
        if chosen.get("other"):
            save_answer(db, r["id"], other_code, other_text)
        else:
            db.execute("DELETE FROM survey_answer WHERE respondent_id = ? "
                       "AND question_code = ?", (r["id"], other_code))
    db.commit()

    answers = prune_hidden_answers(r["id"])
    target = next_visible(idx + 1, answers)
    if target is None:
        return finish_survey(r["id"])
    _set_step(r["id"], target)
    return redirect(url_for("survey", idx=target))


@app.route("/tasks/intro", methods=["GET", "POST"])
def tasks_intro():
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))
    if r["stage"] != "intro" or r["step"] != 0:
        return redirect_to_stage(r)

    if request.method == "POST":
        if request.form.get("action") == "back":
            target = prev_visible(len(QUESTIONS), get_answers(r["id"]))
            if target is not None:
                _set_step(r["id"], target, stage="survey")
                return redirect(url_for("survey", idx=target))
            return redirect(url_for("tasks_intro"))
        _set_step(r["id"], 1)
        return redirect(url_for("tasks_example"))

    return render_template("task_intro.html", total=task_total(r["id"]),
                           attributes=DESIGN_SPEC["attributes"],
                           scenario=SCENARIO, journey=journey("intro", 1))


def example_table():
    """Если кода нет в текущих характеристиках, берётся первый уровень,
    чтобы пример не ломался после правок json."""
    cols = []
    for src in EXAMPLE_SET:
        prof = {}
        for attr in DESIGN_SPEC["attributes"]:
            codes = [l["value"] for l in attr["levels"]]
            v = src.get(attr["code"])
            prof[attr["code"]] = v if v in codes else codes[0]
        cols.append(prof)
    return build_table(cols)


@app.route("/tasks/example", methods=["GET", "POST"])
def tasks_example():
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))
    if r["stage"] != "intro" or r["step"] != 1:
        return redirect_to_stage(r)

    if request.method == "POST":
        if request.form.get("action") == "back":
            _set_step(r["id"], 0)
            return redirect(url_for("tasks_intro"))
        _set_step(r["id"], 0, stage="tasks")
        return redirect(url_for("task", idx=0))

    return render_template("task_example.html", rows=example_table(),
                           scenario=SCENARIO_SHORT, journey=journey("intro", 2))


@app.get("/tasks")
def tasks_entry():
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))
    return redirect_to_stage(r)


def render_task(row, idx, total, error=None, status=200):
    return render_template(
        "task.html", rows=build_table(task_columns(row)),
        idx=idx, pos=idx + 1, total=total, error=error,
        scenario=SCENARIO_SHORT, journey=journey("tasks", idx + 1, total),
    ), status


@app.get("/task/<int:idx>")
def task(idx):
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))
    if r["stage"] != "tasks":
        return redirect_to_stage(r)
    if idx != r["step"]:
        return redirect(url_for("task", idx=r["step"]))

    db = get_db()
    row = db.execute("SELECT * FROM task WHERE respondent_id = ? AND idx = ?",
                     (r["id"], idx)).fetchone()
    if row is None:
        return finish_all(r["id"])

    if row["shown_at"] is None:
        db.execute("UPDATE task SET shown_at = ? WHERE id = ?", (now(), row["id"]))
        db.commit()

    return render_task(row, idx, task_total(r["id"]))


@app.post("/task/<int:idx>")
def task_submit(idx):
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))
    if r["stage"] != "tasks":
        return redirect_to_stage(r)
    if idx != r["step"]:
        return redirect(url_for("task", idx=r["step"]))

    db = get_db()
    row = db.execute("SELECT * FROM task WHERE respondent_id = ? AND idx = ?",
                     (r["id"], idx)).fetchone()
    if row is None:
        return finish_all(r["id"])
    total = task_total(r["id"])

    # форма присылает номер колонки на экране (1, 2, 3),
    # в БД пишем букву профиля, стоявшего в этой колонке
    try:
        col = int(request.form.get("choice", ""))
    except ValueError:
        col = 0
    if not 1 <= col <= N_ALTS:
        return render_task(row, idx, total, error="Выберите один из сервисов",
                           status=400)
    choice = row["display_order"][col - 1]

    # активное время из браузера: пока вкладка в фоне, отсчёт стоит
    try:
        dwell = int(request.form.get("dwell_ms", "0"))
        dwell = dwell if 0 < dwell < 3_600_000 else None
    except ValueError:
        dwell = None

    db.execute("UPDATE task SET choice = ?, answered_at = ?, dwell_ms = ? WHERE id = ?",
               (choice, now(), dwell, row["id"]))
    db.commit()

    if idx + 1 >= total:
        return finish_all(r["id"])
    _set_step(r["id"], idx + 1)
    return redirect(url_for("task", idx=idx + 1))


@app.get("/thanks")
def thanks():
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))
    if r["stage"] not in ("done", "withdrawn"):
        return redirect_to_stage(r)
    withdrawn = r["stage"] == "withdrawn"
    session.pop("rid", None)
    return render_template("thanks.html", withdrawn=withdrawn,
                           journey=journey("done"))


# ─────────────────────────────  панель исследователя  ─────────────────────────────

def is_admin():
    return session.get("admin_until", 0) > time.time()


def admin_only():
    if not RESULTS_KEY:
        abort(404)
    if not is_admin():
        return redirect(url_for("results"))
    return None


@app.get("/results")
def results():
    if not RESULTS_KEY:
        abort(404)
    if not is_admin():
        return render_template("admin_login.html")
    stats = admin.compute(get_db(), QUESTIONS, DESIGN_SPEC["attributes"],
                          LETTERS, COLUMN_LABELS)
    return render_template("admin.html", s=stats)


@app.post("/results/login")
def results_login():
    if not RESULTS_KEY:
        abort(404)
    key = request.form.get("key", "")
    if not hmac.compare_digest(key.encode(), RESULTS_KEY.encode()):
        time.sleep(1)
        return render_template("admin_login.html", error="Неверный ключ"), 403
    session["admin_until"] = time.time() + ADMIN_HOURS * 3600
    session.permanent = True
    return redirect(url_for("results"))


@app.post("/results/logout")
def results_logout():
    session.pop("admin_until", None)
    return redirect(url_for("results"))


@app.get("/results/csv")
def results_csv():
    denied = admin_only()
    if denied:
        return denied
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    return Response(
        export_csv(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="students_results_{stamp}.csv"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/results/delete")
def results_delete():
    denied = admin_only()
    if denied:
        return denied
    rid = (request.form.get("rid") or "").strip()
    if admin.delete_respondent(get_db(), rid):
        flash(f"Респондент {rid[:8]} удалён", "ok")
    else:
        flash("Такого респондента нет", "error")
    return redirect(url_for("results") + "#respondents")


@app.post("/results/clear")
def results_clear():
    denied = admin_only()
    if denied:
        return denied
    if (request.form.get("confirm") or "").strip().upper() != "УДАЛИТЬ":
        flash("База не очищена: нужно ввести слово УДАЛИТЬ", "error")
        return redirect(url_for("results") + "#danger")
    name = admin.clear_all(get_db(), DB_PATH)
    flash(f"База очищена. Резервная копия: data/backups/{name}", "ok")
    return redirect(url_for("results"))
