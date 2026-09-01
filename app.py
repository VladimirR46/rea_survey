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

# Свободный ввод («другая специальность») хранится отдельной строкой
# survey_answer с кодом «<вопрос>_other». Так основной ответ остаётся
# категориальным и пригодным для подсчётов, а текст лежит рядом.
OTHER_SUFFIX = "_other"
OTHER_MAX_LEN = 100

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


def other_option(question):
    """Вариант этого вопроса, требующий текстового ответа, или None."""
    for opt in question["options"]:
        if opt.get("other"):
            return opt
    return None


def clean_other_text(raw):
    """Приводит свободный ввод к виду, пригодному для анализа:
    схлопывает пробелы и обрезает по длине."""
    text = " ".join((raw or "").split())
    return text[:OTHER_MAX_LEN]


def prune_hidden_answers(rid):
    """Удаляет ответы на вопросы, ставшие ненужными.

    Человек мог вернуться назад и сменить ветвящий ответ: был
    «практикующий врач» — стал «руководитель», и вопрос про формат помощи
    больше не задаётся. Без очистки в данных остаётся формат помощи
    у руководителя, которого о нём не спрашивали.

    Заодно убирает текст свободного ввода, если вопрос скрылся или
    человек передумал и выбрал обычный вариант.

    Цикл — на случай цепочки условий. Удаление может только открывать
    вопросы, но не скрывать, поэтому он всегда сходится."""
    db = get_db()
    while True:
        answers = get_answers(rid)
        drop = set()

        for q in QUESTIONS:
            code = q["code"]
            if code in answers and is_skipped(q, answers):
                drop.add(code)

            # текст имеет смысл, только пока выбран сам вариант «другое»
            other_code = code + OTHER_SUFFIX
            if other_code in answers:
                opt = other_option(q)
                if code in drop or opt is None or answers.get(code) != opt["value"]:
                    drop.add(other_code)

        if not drop:
            return answers

        db.executemany(
            "DELETE FROM survey_answer WHERE respondent_id = ? AND question_code = ?",
            [(rid, code) for code in sorted(drop)],
        )
        db.commit()


def finish_tasks(rid):
    """Завершение опроса: этап done и отметка времени."""
    db = get_db()
    total = db.execute(
        "SELECT COUNT(*) c FROM task WHERE respondent_id = ?", (rid,)
    ).fetchone()["c"]
    db.execute("UPDATE respondent SET finished_at = ? WHERE id = ? "
               "AND finished_at IS NULL", (now(), rid))
    db.commit()
    _set_step(rid, total, stage="done")
    return redirect(url_for("thanks"))


def redirect_to_stage(r):
    """Единственное место, которое решает, где респонденту сейчас место.

    Этап определяет доступный раздел: survey — только вопросы, tasks —
    только задания, done и withdrawn — только финальная страница. Любой
    маршрут не своего этапа возвращает человека сюда, а не обрабатывает
    запрос: иначе шаг одного раздела трактуется как шаг другого."""
    stage = r["stage"]
    if stage == "survey":
        return redirect(url_for("survey", idx=r["step"]))
    if stage == "tasks":
        return redirect(url_for("task", idx=r["step"]))
    return redirect(url_for("thanks"))


def finish_survey(rid):
    """Переход от анкеты к заданиям. Один и тот же переход нужен и после
    последнего ответа, и когда шаг почему-то оказался за пределами анкеты."""
    materialize_tasks(rid)
    # step обнуляется: теперь это номер задания, а не вопроса
    _set_step(rid, 0, stage="tasks")
    return redirect(url_for("task", idx=0))


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


@app.context_processor
def template_globals():
    """Ограничение длины свободного ввода задаётся в одном месте
    и попадает в атрибут maxlength."""
    return {"other_max": OTHER_MAX_LEN}


# ─────────────────────────────  маршруты  ─────────────────────────────

@app.get("/")
def index():
    """Начатое прохождение продолжается с того места, где человек его бросил."""
    r = current_respondent()
    if r is not None:
        return redirect_to_stage(r)
    return render_template("welcome.html")


@app.post("/start")
def start():
    """Согласие получено: создаём респондента и фиксируем факт согласия."""
    # повторная отправка формы не должна создавать второго респондента
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


@app.get("/survey/<int:idx>")
def survey(idx):
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))

    if r["stage"] != "survey":
        return redirect_to_stage(r)

    # Промотать вперёд по URL нельзя: сверяем с сохранённым шагом
    if idx != r["step"]:
        return redirect(url_for("survey", idx=r["step"]))

    answers = get_answers(r["id"])

    # Вопрос мог стать пропускаемым — перескакиваем на следующий видимый.
    # None значит, что вопросы кончились. Сравнивать idx с len(QUESTIONS)
    # здесь нельзя: редирект на тот же самый idx уходил в цикл.
    target = next_visible(idx, answers)
    if target is None:
        return finish_survey(r["id"])
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
        other_value=answers.get(QUESTIONS[idx]["code"] + OTHER_SUFFIX),
        has_prev=prev_visible(idx, answers) is not None,
    )


@app.post("/survey/<int:idx>")
def survey_submit(idx):
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))

    if r["stage"] != "survey":
        return redirect_to_stage(r)

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
    other_text = clean_other_text(request.form.get("other_text"))

    def invalid(message, keep_selected=None):
        """Показать вопрос заново с сообщением, не потеряв введённое."""
        answers = get_answers(r["id"])
        pos, total = progress(idx, answers)
        return render_template(
            "question.html", question=question, idx=idx, pos=pos, total=total,
            selected=keep_selected, other_value=other_text,
            has_prev=prev_visible(idx, answers) is not None,
            error=message,
        ), 400

    # Проверка на сервере обязательна: атрибут required обходится
    # отключением JS или прямым POST-запросом
    valid = {o["value"] for o in question["options"]}
    if value not in valid:
        return invalid("Выберите один из вариантов")

    chosen = next(o for o in question["options"] if o["value"] == value)
    wants_text = bool(chosen.get("other"))
    if wants_text and not other_text:
        return invalid("Укажите вашу специальность", keep_selected=value)

    db.execute(
        """INSERT OR REPLACE INTO survey_answer
               (respondent_id, question_code, value, answered_at)
           VALUES (?, ?, ?, ?)""",
        (r["id"], question["code"], value, now()),
    )
    other_code = question["code"] + OTHER_SUFFIX
    if wants_text:
        db.execute(
            """INSERT OR REPLACE INTO survey_answer
                   (respondent_id, question_code, value, answered_at)
               VALUES (?, ?, ?, ?)""",
            (r["id"], other_code, other_text, now()),
        )
    else:
        # человек мог сначала выбрать «другое», а потом передумать
        db.execute(
            "DELETE FROM survey_answer WHERE respondent_id = ? AND question_code = ?",
            (r["id"], other_code),
        )
    db.commit()

    # Ответ мог включить пропуск — убираем осиротевшие ответы
    # и пересчитываем на свежих данных
    answers = prune_hidden_answers(r["id"])
    target = next_visible(idx + 1, answers)
    if target is None:
        return finish_survey(r["id"])

    _set_step(r["id"], target)
    return redirect(url_for("survey", idx=target))


@app.get("/tasks")
def tasks_entry():
    """Вход в блок заданий — перенаправляет на текущее."""
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))
    return redirect_to_stage(r)


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
    row = db.execute(
        "SELECT * FROM task WHERE respondent_id = ? AND idx = ?", (r["id"], idx)
    ).fetchone()

    # Заданий больше нет: доводим состояние до конца, а не показываем
    # благодарность человеку, который так и остался на этапе tasks
    if row is None:
        return finish_tasks(r["id"])

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

    if r["stage"] != "tasks":
        return redirect_to_stage(r)

    if idx != r["step"]:
        return redirect(url_for("task", idx=r["step"]))

    db = get_db()
    row = db.execute(
        "SELECT * FROM task WHERE respondent_id = ? AND idx = ?", (r["id"], idx)
    ).fetchone()
    if row is None:
        return finish_tasks(r["id"])

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
        return finish_tasks(r["id"])

    _set_step(r["id"], idx + 1)
    return redirect(url_for("task", idx=idx + 1))


@app.get("/thanks")
def thanks():
    """Финальная страница. Сессия очищается только здесь и только у того,
    кто действительно закончил: иначе случайный заход на этот адрес
    посреди опроса стирал куку и обнулял всё прохождение."""
    r = current_respondent()
    if r is None:
        return redirect(url_for("index"))
    if r["stage"] not in ("done", "withdrawn"):
        return redirect_to_stage(r)

    withdrawn = r["stage"] == "withdrawn"
    session.clear()
    return render_template("thanks.html", withdrawn=withdrawn)
