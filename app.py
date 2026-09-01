"""Опрос предпочтений врачей в отношении медицинских ИИ-систем.
РЭУ им. Г.В. Плеханова.

Поток: согласие -> анкета -> задания выбора -> благодарность.
Состояние респондента хранится в БД (stage и step), в куке только UUID.
"""

import hashlib
import json
import os
import random
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from flask import Flask, redirect, render_template, request, session, url_for

from db import DB_PATH, get_db, init_db
from design.generator import generate

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)


# ─────────────────────────────  конфигурация  ─────────────────────────────

# Ключ подписи кук сессий, задаётся в docker-compose.yml
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-not-for-production")

# Кука должна пережить закрытие вкладки: телефон выгружает вкладки из памяти,
# и без этого респондент теряет прогресс и начинает опрос заново.
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=int(os.environ.get("SESSION_DAYS", "14"))),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SEND_FILE_MAX_AGE_DEFAULT=0,
    TEMPLATES_AUTO_RELOAD=True,
)

# Правите текст согласия — увеличивайте версию, чтобы знать,
# какую редакцию читал каждый респондент.
CONSENT_VERSION = "v1"

with open(os.path.join(BASE_DIR, "design/questions.json"), encoding="utf-8") as f:
    QUESTIONS = json.load(f)["questions"]

with open(os.path.join(BASE_DIR, "design/attributes.json"), encoding="utf-8") as f:
    ATTRIBUTES = json.load(f)["attributes"]

# 'shared' — один набор заданий на всех, 'individual' — свой каждому
DESIGN_MODE = os.environ.get("DESIGN_MODE", "individual")
N_TASKS = int(os.environ.get("N_TASKS", "12"))
DESIGN_SEED = int(os.environ.get("DESIGN_SEED", "20260901"))


# ─────────────────────────────  утилиты  ─────────────────────────────

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_mobile(user_agent):
    """Полный user-agent не сохраняем — он слишком уникален
    и по сути является отпечатком устройства."""
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


# ─────────────────────────────  дизайн заданий  ─────────────────────────────

def ensure_design():
    """Находит или создаёт строку design под текущие настройки.

    Вызывается при импорте, до появления контекста Flask, поэтому работает
    с отдельным соединением. Воркеров может быть несколько и стартуют они
    одновременно, поэтому вставка идёт через ON CONFLICT: победит первый,
    остальные прочитают уже существующую строку."""
    raw = json.dumps(ATTRIBUTES, ensure_ascii=False, sort_keys=True)
    attrs_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]

    # Снимок характеристик кладём внутрь дизайна: коды уровней в таблице task
    # останутся расшифровываемыми даже после правок attributes.json
    spec = {"attributes": ATTRIBUTES}
    if DESIGN_MODE == "shared":
        spec["tasks"] = generate(ATTRIBUTES, DESIGN_SEED, N_TASKS)

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
    """Личный seed, выведенный из UUID: зная ID, набор всегда можно
    воспроизвести."""
    return int(hashlib.sha256(rid.encode()).hexdigest()[:8], 16)


def materialize_tasks(rid):
    """Записывает все задания респондента в таблицу task.

    Генерация происходит здесь один раз, а не при показе каждой страницы.
    Иначе обновление страницы меняло бы пару, и мы бы не знали,
    что человек видел в момент выбора."""
    db = get_db()

    exists = db.execute(
        "SELECT 1 FROM task WHERE respondent_id = ? LIMIT 1", (rid,)
    ).fetchone()
    if exists:
        return

    seed = seed_for(rid)
    rng = random.Random(seed)

    if DESIGN_MODE == "shared":
        pairs = list(DESIGN_SPEC["tasks"])
    else:
        pairs = generate(DESIGN_SPEC["attributes"], seed, N_TASKS)

    # Порядок заданий индивидуален даже в общем режиме: иначе эффект
    # усталости ляжет на одни и те же пары у всех респондентов
    rng.shuffle(pairs)

    for i, pair in enumerate(pairs):
        db.execute(
            """INSERT INTO task
                   (respondent_id, idx, profile_a_json, profile_b_json, swapped)
               VALUES (?, ?, ?, ?, ?)""",
            (rid, i,
             json.dumps(pair["A"], ensure_ascii=False),
             json.dumps(pair["B"], ensure_ascii=False),
             rng.randint(0, 1)),   # какой профиль окажется слева
        )

    db.execute("UPDATE respondent SET design_id = ?, seed = ? WHERE id = ?",
               (DESIGN_ID, seed, rid))
    db.commit()


def build_rows(profile_left, profile_right):
    """Строки таблицы сравнения: название характеристики и два её значения.

    Расшифровка берётся из снимка дизайна, а не из текущего attributes.json,
    иначе старые задания перестанут читаться после правки файла."""
    rows = []
    for attr in DESIGN_SPEC["attributes"]:
        code = attr["code"]
        rows.append({
            "name": attr["name"],
            "left": LEVELS.get((code, profile_left[code]), {"label": profile_left[code]}),
            "right": LEVELS.get((code, profile_right[code]), {"label": profile_right[code]}),
        })
    return rows


# ─────────────────────────────  логика анкеты  ─────────────────────────────

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
    return answers.get(cond["question"]) == cond["value"]


def next_visible(idx, answers):
    """Индекс ближайшего показываемого вопроса начиная с idx.
    None — вопросы кончились."""
    while idx < len(QUESTIONS):
        if not is_skipped(QUESTIONS[idx], answers):
            return idx
        idx += 1
    return None


def prev_visible(idx, answers):
    """То же назад. None — это был первый вопрос."""
    idx -= 1
    while idx >= 0:
        if not is_skipped(QUESTIONS[idx], answers):
            return idx
        idx -= 1
    return None


def progress(idx, answers):
    """Позиция и общее число вопросов с учётом пропусков. Число может
    измениться после ответа на первый вопрос — это нормально.

    Считаем через сравнение, а не через index(): вопрос мог стать
    пропускаемым, и тогда его нет в списке видимых."""
    visible = [i for i, q in enumerate(QUESTIONS) if not is_skipped(q, answers)]
    pos = sum(1 for i in visible if i <= idx) or 1
    return pos, len(visible)


# ─────────────────────────────  инициализация  ─────────────────────────────

init_db(app)
DESIGN_ID, DESIGN_SPEC = ensure_design()

# Плоский справочник уровней: (код характеристики, значение) -> описание уровня.
# Собирается один раз, чтобы не перебирать список на каждой ячейке таблицы.
LEVELS = {
    (attr["code"], lvl["value"]): lvl
    for attr in DESIGN_SPEC["attributes"]
    for lvl in attr["levels"]
}


# ─────────────────────────────  маршруты  ─────────────────────────────

@app.get("/")
def index():
    return render_template("welcome.html")


@app.post("/start")
def start():
    """Согласие получено: создаём респондента и фиксируем факт согласия."""
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


@app.get("/survey/<int:idx>")
def survey(idx):
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))

    # Промотать вперёд по URL нельзя: сверяем с сохранённым шагом
    if idx != r["step"] or idx >= len(QUESTIONS):
        return redirect(url_for("survey", idx=r["step"]))

    answers = get_answers(r["id"])

    # Вопрос мог стать пропускаемым — перескакиваем на следующий видимый
    target = next_visible(idx, answers)
    if target is None:
        return redirect(url_for("tasks_entry"))
    if target != idx:
        _set_step(r["id"], target)
        return redirect(url_for("survey", idx=target))

    pos, total = progress(idx, answers)
    return render_template(
        "question.html",
        question=QUESTIONS[idx],
        idx=idx,
        pos=pos,
        total=total,
        selected=answers.get(QUESTIONS[idx]["code"]),
        has_prev=prev_visible(idx, answers) is not None,
    )


@app.post("/survey/<int:idx>")
def survey_submit(idx):
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))

    # Форма из устаревшей вкладки или прямой запрос мимо текущего шага.
    # Ответ не принимаем и возвращаем человека туда, где он на самом деле.
    if idx != r["step"] or idx >= len(QUESTIONS):
        return redirect(url_for("survey", idx=r["step"]))

    question = QUESTIONS[idx]
    db = get_db()

    # Кнопка «назад» — просто перемещаемся, ничего не записывая
    if request.form.get("action") == "back":
        target = prev_visible(idx, get_answers(r["id"]))
        if target is not None:
            _set_step(r["id"], target)
            return redirect(url_for("survey", idx=target))
        return redirect(url_for("survey", idx=idx))

    value = request.form.get("value")
    # Проверка на сервере обязательна: атрибут required обходится
    # отключением JS или прямым POST-запросом
    valid = {o["value"] for o in question["options"]}
    if value not in valid:
        answers = get_answers(r["id"])
        pos, total = progress(idx, answers)
        return render_template(
            "question.html", question=question, idx=idx, pos=pos, total=total,
            selected=None, has_prev=prev_visible(idx, answers) is not None,
            error="Выберите один из вариантов",
        ), 400

    db.execute(
        """INSERT OR REPLACE INTO survey_answer
               (respondent_id, question_code, value, answered_at)
           VALUES (?, ?, ?, ?)""",
        (r["id"], question["code"], value, now()),
    )
    db.commit()

    # Ответ мог включить пропуск — пересчитываем на свежих данных
    target = next_visible(idx + 1, get_answers(r["id"]))
    if target is None:
        materialize_tasks(r["id"])
        # step обнуляется: теперь это номер задания, а не вопроса
        _set_step(r["id"], 0, stage="tasks")
        return redirect(url_for("tasks_entry"))

    _set_step(r["id"], target)
    return redirect(url_for("survey", idx=target))


@app.get("/tasks")
def tasks_entry():
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))
    return redirect(url_for("task", idx=r["step"]))


@app.get("/task/<int:idx>")
def task(idx):
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))

    if r["stage"] == "done":
        return redirect(url_for("thanks"))

    if idx != r["step"]:
        return redirect(url_for("task", idx=r["step"]))

    db = get_db()
    row = db.execute(
        "SELECT * FROM task WHERE respondent_id = ? AND idx = ?", (r["id"], idx)
    ).fetchone()

    if row is None:
        return redirect(url_for("thanks"))

    # Время только первого показа: иначе обновление страницы обнулило бы отсчёт
    if row["shown_at"] is None:
        db.execute("UPDATE task SET shown_at = ? WHERE id = ?", (now(), row["id"]))
        db.commit()

    profile_a = json.loads(row["profile_a_json"])
    profile_b = json.loads(row["profile_b_json"])
    left, right = ((profile_b, profile_a) if row["swapped"]
                   else (profile_a, profile_b))

    total = db.execute(
        "SELECT COUNT(*) c FROM task WHERE respondent_id = ?", (r["id"],)
    ).fetchone()["c"]

    return render_template(
        "task.html",
        rows=build_rows(left, right),
        idx=idx, pos=idx + 1, total=total, selected=None,
    )


@app.post("/task/<int:idx>")
def task_submit(idx):
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))

    if idx != r["step"]:
        return redirect(url_for("task", idx=r["step"]))

    db = get_db()
    row = db.execute(
        "SELECT * FROM task WHERE respondent_id = ? AND idx = ?", (r["id"], idx)
    ).fetchone()
    if row is None:
        return redirect(url_for("thanks"))

    total = db.execute(
        "SELECT COUNT(*) c FROM task WHERE respondent_id = ?", (r["id"],)
    ).fetchone()["c"]

    side = request.form.get("choice")   # 'left' | 'right' | 'none'
    if side not in ("left", "right", "none"):
        profile_a = json.loads(row["profile_a_json"])
        profile_b = json.loads(row["profile_b_json"])
        left, right = ((profile_b, profile_a) if row["swapped"]
                       else (profile_a, profile_b))
        return render_template(
            "task.html", rows=build_rows(left, right),
            idx=idx, pos=idx + 1, total=total, selected=None,
            error="Выберите один из вариантов",
        ), 400

    # Форма присылает сторону экрана, в БД пишем логический профиль:
    # при swapped=1 левая колонка — это профиль B
    if side == "none":
        choice = "none"
    elif row["swapped"]:
        choice = "B" if side == "left" else "A"
    else:
        choice = "A" if side == "left" else "B"

    # Время из браузера считает только активные секунды: если вкладка
    # ушла в фон, отсчёт останавливается
    try:
        dwell = int(request.form.get("dwell_ms", "0"))
        dwell = dwell if 0 < dwell < 3_600_000 else None
    except ValueError:
        dwell = None

    db.execute(
        "UPDATE task SET choice = ?, answered_at = ?, dwell_ms = ? WHERE id = ?",
        (choice, now(), dwell, row["id"]),
    )
    db.commit()

    if idx + 1 >= total:
        _set_step(r["id"], total, stage="done")
        db.execute("UPDATE respondent SET finished_at = ? WHERE id = ?",
                   (now(), r["id"]))
        db.commit()
        return redirect(url_for("thanks"))

    _set_step(r["id"], idx + 1)
    return redirect(url_for("task", idx=idx + 1))


@app.get("/thanks")
def thanks():
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))
    session.clear()
    return render_template("thanks.html")
