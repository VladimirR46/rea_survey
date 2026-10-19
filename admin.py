"""Панель исследователя (/results): сводная статистика и операции с базой.

Здесь только чтение и подсчёты; маршруты и проверка ключа — в app.py.
"""

import json
import os
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

# Москва без перехода на летнее время — фиксированного смещения достаточно
MSK = timezone(timedelta(hours=3))

STAGE_LABELS = {
    "survey": "Анкета",
    "intro": "Инструкция",
    "tasks": "Задания",
    "done": "Завершил",
    "withdrawn": "Отказался",
}

MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн",
          "июл", "авг", "сен", "окт", "ноя", "дек"]

# Пороги для пометок качества
FAST_MEDIAN_MS = 2000      # медиана времени на ситуацию меньше 2 с
MIN_FOR_FLAGS = 5          # пометки ставим, только если ответов не меньше


def _ts(s):
    return datetime.fromisoformat(s) if s else None


def _msk(s):
    dt = _ts(s)
    return dt.astimezone(MSK) if dt else None


def fmt_dt(s):
    dt = _msk(s)
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt:%H:%M}" if dt else ""


def fmt_duration(seconds):
    if seconds is None:
        return "—"
    s = int(round(seconds))
    if s < 3600:
        return f"{s // 60}:{s % 60:02d}"
    if s < 48 * 3600:
        return f"{s // 3600} ч {s % 3600 // 60} мин"
    return f"{s // 86400} дн"


def _pct(n, total):
    return round(100 * n / total) if total else 0


def _bars(items):
    """items: [(label, n)] -> строки столбчатой диаграммы с долями
    от суммы и шириной относительно максимума."""
    total = sum(n for _, n in items)
    top = max([n for _, n in items] + [1])
    return [{"label": label, "n": n, "pct": _pct(n, total),
             "w": round(100 * n / top, 1)} for label, n in items], total


def compute(db, questions, attributes):
    respondents = db.execute(
        "SELECT * FROM respondent ORDER BY created_at DESC").fetchall()

    answers = {}
    for a in db.execute("SELECT respondent_id, question_code, value FROM survey_answer"):
        answers.setdefault(a["respondent_id"], {})[a["question_code"]] = a["value"]

    tasks = {}
    for t in db.execute("SELECT * FROM task ORDER BY respondent_id, idx"):
        tasks.setdefault(t["respondent_id"], []).append(t)

    labels = {(a["code"], l["value"]): l.get("label", l["value"])
              for a in attributes for l in a["levels"]}
    role_q = next((q for q in questions if q["code"] == "role"), None)
    role_labels = {o["value"]: o["label"] for o in role_q["options"]} if role_q else {}

    # ── по респондентам ──
    rows, durations = [], []
    stage_n = {k: 0 for k in STAGE_LABELS}
    mobile = 0
    pos_counts = {"1": 0, "2": 0, "none": 0}
    rep_ok = rep_total = 0
    level_shown, level_chosen = {}, {}

    for r in respondents:
        rid = r["id"]
        stage_n[r["stage"]] = stage_n.get(r["stage"], 0) + 1
        mobile += r["is_mobile"] or 0
        ts = tasks.get(rid, [])
        answered = [t for t in ts if t["choice"]]
        by_idx = {t["idx"]: t for t in ts}

        positions, dwells = [], []
        r_rep_ok = r_rep_total = 0
        for t in answered:
            left = "B" if t["swapped"] else "A"
            pos = "none" if t["choice"] == "none" else ("1" if t["choice"] == left else "2")
            positions.append(pos)
            pos_counts[pos] += 1
            if t["dwell_ms"]:
                dwells.append(t["dwell_ms"])

            pa, pb = json.loads(t["profile_a_json"]), json.loads(t["profile_b_json"])
            for side, prof in (("A", pa), ("B", pb)):
                for code, val in prof.items():
                    key = (code, val)
                    level_shown[key] = level_shown.get(key, 0) + 1
                    if t["choice"] == side:
                        level_chosen[key] = level_chosen.get(key, 0) + 1

            if t["repeat_of"] is not None:
                orig = by_idx.get(t["repeat_of"])
                if orig is not None and orig["choice"]:
                    r_rep_total += 1
                    r_rep_ok += int(orig["choice"] == t["choice"])
        rep_ok += r_rep_ok
        rep_total += r_rep_total

        duration = None
        if r["finished_at"]:
            duration = (_ts(r["finished_at"]) - _ts(r["created_at"])).total_seconds()
            durations.append(duration)

        median_dwell = statistics.median(dwells) if dwells else None
        flags = []
        if len(answered) >= MIN_FOR_FLAGS and median_dwell and median_dwell < FAST_MEDIAN_MS:
            flags.append(("Быстро", f"медиана {median_dwell / 1000:.1f} с на ситуацию"))
        if len(answered) >= MIN_FOR_FLAGS and len(set(positions)) == 1:
            what = {"1": "всегда левое", "2": "всегда правое", "none": "всегда «Ни одно»"}
            flags.append(("Однообразно", what[positions[0]]))
        if r_rep_total and r_rep_ok < r_rep_total:
            flags.append(("Повтор", f"совпало {r_rep_ok} из {r_rep_total} повторов"))

        ans = answers.get(rid, {})
        rows.append({
            "id": rid,
            "short": rid[:8],
            "created": fmt_dt(r["created_at"]),
            "stage": r["stage"],
            "stage_label": STAGE_LABELS.get(r["stage"], r["stage"]),
            "role": role_labels.get(ans.get("role"), "—"),
            "answered": len(answered),
            "total": len(ts),
            "duration": fmt_duration(duration) if duration is not None else "—",
            "dwell": f"{median_dwell / 1000:.1f} с" if median_dwell else "—",
            "mobile": bool(r["is_mobile"]),
            "flags": flags,
        })

    total = len(respondents)
    done = stage_n.get("done", 0)

    kpi = {
        "total": total,
        "done": done,
        "in_progress": sum(stage_n.get(s, 0) for s in ("survey", "intro", "tasks")),
        "completion": _pct(done, total),
        "median_duration": fmt_duration(statistics.median(durations)) if durations else "—",
        "mobile": _pct(mobile, total),
        "flagged": sum(1 for row in rows if row["flags"]),
    }

    # ── воронка ──
    after_survey = sum(stage_n.get(s, 0) for s in ("intro", "tasks", "done"))
    in_tasks = sum(stage_n.get(s, 0) for s in ("tasks", "done"))
    funnel = [
        {"label": "Дали согласие", "n": total},
        {"label": "Ответили на анкету", "n": after_survey},
        {"label": "Перешли к заданиям", "n": in_tasks},
        {"label": "Завершили опрос", "n": done},
    ]
    for f in funnel:
        f["pct"] = _pct(f["n"], total)

    # ── по дням ──
    started, finished = {}, {}
    for r in respondents:
        d = _msk(r["created_at"]).date()
        started[d] = started.get(d, 0) + 1
        if r["finished_at"]:
            fd = _msk(r["finished_at"]).date()
            finished[fd] = finished.get(fd, 0) + 1
    days = []
    if started:
        today = datetime.now(MSK).date()
        first = max(min(started), today - timedelta(days=59))
        top = max(started.values())
        d = first
        while d <= today:
            n = started.get(d, 0)
            days.append({
                "label": f"{d.day} {MONTHS[d.month - 1]}",
                "n": n, "done": finished.get(d, 0),
                "h": round(100 * n / top, 1),
            })
            d += timedelta(days=1)

    # ── задания ──
    choices_total = sum(pos_counts.values())
    choice_split = [
        {"key": "c1", "label": "ИИ-решение 1 (слева)", "n": pos_counts["1"]},
        {"key": "c2", "label": "ИИ-решение 2 (справа)", "n": pos_counts["2"]},
        {"key": "c3", "label": "Ни одно из решений", "n": pos_counts["none"]},
    ]
    for c in choice_split:
        c["pct"] = _pct(c["n"], choices_total)

    levels = []
    for a in attributes:
        items = []
        for l in a["levels"]:
            key = (a["code"], l["value"])
            shown = level_shown.get(key, 0)
            chosen = level_chosen.get(key, 0)
            items.append({"label": labels[key], "shown": shown, "chosen": chosen,
                          "share": _pct(chosen, shown)})
        levels.append({"name": a["name"], "lv": items})

    # ── анкета ──
    survey = []
    for q in questions:
        items = [(o["label"], sum(1 for a in answers.values() if a.get(q["code"]) == o["value"]))
                 for o in q["options"]]
        bars, n = _bars(items)
        others = sorted({a[q["code"] + "_other"] for a in answers.values()
                         if a.get(q["code"] + "_other")})
        survey.append({"text": q["text"], "bars": bars, "n": n, "others": others})

    return {
        "kpi": kpi,
        "funnel": funnel,
        "days": days,
        "choices_total": choices_total,
        "choice_split": choice_split,
        "repeat": {"ok": rep_ok, "total": rep_total, "pct": _pct(rep_ok, rep_total)},
        "levels": levels,
        "survey": survey,
        "rows": rows,
        "updated": datetime.now(MSK).strftime("%d.%m.%Y %H:%M"),
    }


def delete_respondent(db, rid):
    """Удаляет все записи одного респондента. True, если он был."""
    if db.execute("SELECT 1 FROM respondent WHERE id = ?", (rid,)).fetchone() is None:
        return False
    # сначала дочерние строки: внешние ключи включены
    db.execute("DELETE FROM task WHERE respondent_id = ?", (rid,))
    db.execute("DELETE FROM survey_answer WHERE respondent_id = ?", (rid,))
    db.execute("DELETE FROM respondent WHERE id = ?", (rid,))
    db.commit()
    return True


def clear_all(db, db_path):
    """Сначала делает резервную копию рядом с базой, затем удаляет всех
    респондентов, их ответы и задания. Таблица design не трогается.
    Возвращает имя файла копии."""
    folder = os.path.join(os.path.dirname(db_path), "backups")
    os.makedirs(folder, exist_ok=True)
    name = f"before_clear_{datetime.now(MSK):%Y%m%d_%H%M%S}.sqlite3"
    dst = sqlite3.connect(os.path.join(folder, name))
    try:
        db.backup(dst)
    finally:
        dst.close()

    db.execute("DELETE FROM task")
    db.execute("DELETE FROM survey_answer")
    db.execute("DELETE FROM respondent")
    db.commit()
    return name
