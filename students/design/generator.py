import random


def _column(levels, n_tasks, n_alts, rng):
    """Уровни одной характеристики для всех заданий: n_tasks наборов
    по n_alts разных уровней.

    Баланс: каждый уровень показывается почти одинаковое число раз
    (при 10 заданиях, 3 сервисах и 5 брендах — ровно по 6 раз).
    Набор собирается жадно из уровней с наибольшим остатком: так
    в одном задании уровни никогда не повторяются."""
    values = [l["value"] for l in levels]
    slots = n_tasks * n_alts
    base, extra = divmod(slots, len(values))
    left = {v: base for v in values}
    for v in rng.sample(values, extra):
        left[v] += 1

    sets = []
    for _ in range(n_tasks):
        order = sorted(values, key=lambda v: (-left[v], rng.random()))
        chosen = order[:n_alts]
        for v in chosen:
            left[v] -= 1
        rng.shuffle(chosen)
        sets.append(chosen)

    rng.shuffle(sets)
    return sets


def generate(attributes, seed, n_tasks, n_alts=3):
    """Список заданий: каждое — список из n_alts профилей {код: уровень}.

    Каждая характеристика раскладывается независимо, поэтому сочетания
    брендов внутри профиля случайны, а в одной строке таблицы бренды
    всегда разные. Чистая функция: одни аргументы — один результат."""
    rng = random.Random(seed)

    for _ in range(100):
        columns = {a["code"]: _column(a["levels"], n_tasks, n_alts, rng)
                   for a in attributes}
        tasks = [[{code: columns[code][t][j] for code in columns}
                  for j in range(n_alts)]
                 for t in range(n_tasks)]

        # одинаковые задания практически невозможны, но проверим
        keys = {frozenset(tuple(sorted(p.items())) for p in t) for t in tasks}
        if len(keys) == len(tasks):
            return tasks
    return tasks
