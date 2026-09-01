"""Сборка пар профилей.

Заглушка: случайный выбор уровней с отсевом вырожденных пар.
Когда появится настоящий план эксперимента, меняется только тело
generate() — остальной код про неё ничего не знает.
"""

import random


def _ranks(profile, attributes):
    """Ранги профиля по упорядоченным характеристикам."""
    out = {}
    for attr in attributes:
        for lvl in attr["levels"]:
            if lvl["value"] == profile[attr["code"]] and "rank" in lvl:
                out[attr["code"]] = lvl["rank"]
    return out


def _dominates(a, b, attributes):
    """A доминирует B: не хуже по всем упорядоченным характеристикам
    и лучше хотя бы по одной, при совпадении неупорядоченных.
    Такие пары бессмысленны — выбор в них предопределён."""
    unordered = [attr["code"] for attr in attributes
                 if not any("rank" in l for l in attr["levels"])]
    if any(a[c] != b[c] for c in unordered):
        return False

    ra, rb = _ranks(a, attributes), _ranks(b, attributes)
    return all(ra[c] >= rb[c] for c in ra) and any(ra[c] > rb[c] for c in ra)


def generate(attributes, seed, n_tasks):
    """Список заданий вида {"A": {код: уровень}, "B": {...}}.

    Чистая функция: одни и те же аргументы дают один и тот же результат."""
    rng = random.Random(seed)
    tasks = []
    attempts = 0

    while len(tasks) < n_tasks and attempts < n_tasks * 200:
        attempts += 1

        a = {attr["code"]: rng.choice(attr["levels"])["value"] for attr in attributes}
        b = {attr["code"]: rng.choice(attr["levels"])["value"] for attr in attributes}

        if a == b:
            continue
        if _dominates(a, b, attributes) or _dominates(b, a, attributes):
            continue

        tasks.append({"A": a, "B": b})

    return tasks