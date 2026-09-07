"""Deterministic commentary rendered from insight evidence and templates."""
from __future__ import annotations

from typing import Any

# key: (base_type, lang, level)
TEMPLATES: dict[tuple[str, str, str], str] = {
    ("TRAFFIC_RISK", "en", "pro"):
        "{a} is losing ~{pace_s:.1f}s/lap behind {b}, gap {gap:.1f}s. "
        "Track position pressure builds — an early stop becomes viable.",
    ("TRAFFIC_RISK", "en", "beginner"):
        "{a} is faster but stuck behind {b}. Overtaking here is hard, so "
        "the team may pit {a} to get clear air.",
    ("TRAFFIC_RISK", "ru", "pro"):
        "{a} теряет ~{pace_s:.1f}с/круг за {b}, отставание {gap:.1f}с. "
        "Ранний пит-стоп становится оправданным.",
    ("TRAFFIC_RISK", "ru", "beginner"):
        "{a} едет быстрее, но заперт за {b}. Обгонять тут почти негде, "
        "поэтому команда может позвать {a} в боксы ради чистого воздуха.",

    ("DRS_TRAIN", "en", "pro"):
        "DRS train of {cars} cars behind {head}; intervals inside DRS range, "
        "overtaking probability low until the train breaks.",
    ("DRS_TRAIN", "en", "beginner"):
        "{cars} cars are queued up behind {head}. Everyone has DRS, so "
        "nobody gains an advantage — expect a stalemate.",
    ("DRS_TRAIN", "ru", "pro"):
        "DRS-паровоз из {cars} машин за {head}; интервалы в зоне DRS, "
        "вероятность обгонов низкая, пока цепь не разорвётся.",
    ("DRS_TRAIN", "ru", "beginner"):
        "За {head} выстроилась очередь из {cars} машин. DRS есть у всех, "
        "поэтому преимущества ни у кого нет — позиции заморожены.",

    ("PIT_WINDOW", "en", "pro"):
        "{a} has a free stop: {margin:.1f}s of margin over the pit loss to "
        "the next car behind, tyres {age} laps old.",
    ("PIT_WINDOW", "en", "beginner"):
        "{a} can pit without losing a position — the gap behind is big "
        "enough to cover the stop.",
    ("PIT_WINDOW", "ru", "pro"):
        "У {a} «бесплатный» пит-стоп: запас {margin:.1f}с сверх потери на "
        "пит-лейне, резине {age} кругов.",
    ("PIT_WINDOW", "ru", "beginner"):
        "{a} может заехать в боксы и не потерять позицию — разрыв до машины "
        "сзади перекрывает потерю времени.",

    ("UNDERCUT_RISK", "en", "pro"):
        "Undercut threat on {b}: {a} is {gap:.1f}s back on {age}-lap tyres "
        "with matching pace. Defending stop window is open.",
    ("UNDERCUT_RISK", "en", "beginner"):
        "{a} is close enough to try an undercut: pit first, get fresh "
        "tyres, and jump {b} during the stops.",
    ("UNDERCUT_RISK", "ru", "pro"):
        "Угроза ундерката для {b}: {a} в {gap:.1f}с на резине возрастом "
        "{age} кругов при равном темпе. Окно защитного пит-стопа открыто.",
    ("UNDERCUT_RISK", "ru", "beginner"):
        "{a} достаточно близко для ундерката: заехать в боксы первым, взять "
        "свежую резину и опередить {b} за счёт пит-стопов.",

    ("DEGRADATION_TREND", "en", "pro"):
        "{a}'s pace has dropped {drift_s:.1f}s over 3 laps on {age}-lap tyres "
        "— degradation accelerating, a stop is overdue.",
    ("DEGRADATION_TREND", "en", "beginner"):
        "{a}'s tyres are going off — each lap is slower than the last. "
        "The team will likely pit soon.",
    ("DEGRADATION_TREND", "ru", "pro"):
        "Темп {a} упал на {drift_s:.1f}с за 3 круга на резине {age} кругов "
        "— деградация ускоряется, пит-стоп давно назрел.",
    ("DEGRADATION_TREND", "ru", "beginner"):
        "Резина {a} сдаёт — каждый круг медленнее предыдущего. "
        "Команда скорее всего скоро вызовет в боксы.",

    ("CLEAN_AIR_PACE_LEADER", "en", "pro"):
        "{a} is the fastest car in clear air, averaging {avg_s:.3f}s/lap "
        "({in_clean} drivers free). {strategy_note}",
    ("CLEAN_AIR_PACE_LEADER", "en", "beginner"):
        "{a} has no traffic and is showing the best pace in clear air "
        "({avg_s:.3f}s average). {strategy_note}",
    ("CLEAN_AIR_PACE_LEADER", "ru", "pro"):
        "{a} — самая быстрая машина в чистом воздухе: средний круг {avg_s:.3f}с "
        "({in_clean} пилотов без трафика). {strategy_note}",
    ("CLEAN_AIR_PACE_LEADER", "ru", "beginner"):
        "{a} едет в свободном воздухе и показывает лучший темп "
        "({avg_s:.3f}с в среднем). {strategy_note}",

    ("BATTLE_DETECTED", "en", "pro"):
        "P{p1} {a} vs P{p2} {b}: gap {gap:.2f}s, laps within pace window — "
        "DRS attack possible next straight.",
    ("BATTLE_DETECTED", "en", "beginner"):
        "{a} is right behind {b} — just {gap:.2f}s apart and evenly matched. "
        "An overtake could happen any lap.",
    ("BATTLE_DETECTED", "ru", "pro"):
        "P{p1} {a} против P{p2} {b}: отставание {gap:.2f}с, темп сопоставим — "
        "атака с DRS возможна на следующей прямой.",
    ("BATTLE_DETECTED", "ru", "beginner"):
        "{a} прямо за {b} — всего {gap:.2f}с при равном темпе. "
        "Обгон может случиться в любой момент.",

    ("SC_PIT_WINDOW", "en", "pro"):
        "{a} should pit now: Safety Car halves pit-stop time loss. "
        "Tyres are {age} laps old — ideal window to change rubber.",
    ("SC_PIT_WINDOW", "en", "beginner"):
        "With the Safety Car out, pitting costs {a} much less time than usual. "
        "Great moment to change tyres ({age} laps old).",
    ("SC_PIT_WINDOW", "ru", "pro"):
        "{a} стоит заехать в боксы: машина безопасности вдвое снижает потерю времени. "
        "Резине {age} кругов — идеальный момент для смены.",
    ("SC_PIT_WINDOW", "ru", "beginner"):
        "Под машиной безопасности пит-стоп для {a} обходится дешевле обычного. "
        "Самый выгодный момент сменить резину ({age} кругов).",
}

_BASE_TYPES = (
    "TRAFFIC_RISK", "DRS_TRAIN", "PIT_WINDOW", "SC_PIT_WINDOW", "UNDERCUT_RISK",
    "DEGRADATION_TREND", "CLEAN_AIR_PACE_LEADER", "BATTLE_DETECTED",
)

_MODEL_TYPES = {
    "TRAFFIC_RISK",
    "PIT_WINDOW",
    "SC_PIT_WINDOW",
    "UNDERCUT_RISK",
    "DEGRADATION_TREND",
    "CLEAN_AIR_PACE_LEADER",
}


def _params(base: str, ins: dict[str, Any]) -> dict[str, Any]:
    ev = ins["evidence"]
    ids = ins["driver_ids"]
    if base == "TRAFFIC_RISK":
        return {"a": ids[0], "b": ids[1],
                "pace_s": ev["pace_delta_ms"] / 1000, "gap": ev["interval_s"]}
    if base == "DRS_TRAIN":
        return {"cars": ev["cars"], "head": ev["head"]}
    if base == "PIT_WINDOW":
        return {"a": ids[0], "margin": ev["margin_s"], "age": ev["tyre_age_laps"]}
    if base == "UNDERCUT_RISK":
        return {"a": ids[0], "b": ids[1],
                "gap": ev["interval_s"], "age": ev["attacker_tyre_age_laps"]}
    if base == "DEGRADATION_TREND":
        return {"a": ids[0], "drift_s": ev["drift_ms"] / 1000, "age": ev["tyre_age_laps"]}
    if base == "CLEAN_AIR_PACE_LEADER":
        avg_s = ev["avg_lap_ms"] / 1000
        vs = ev.get("vs_race_leader_ms")
        if vs is not None and vs < 0:
            strategy_note = f"faster than the leader's pace by {abs(vs/1000):.3f}s — strategy signal."
        else:
            strategy_note = "monitoring for strategic opportunity."
        return {"a": ids[0], "avg_s": avg_s, "in_clean": ev["drivers_in_clean_air"],
                "strategy_note": strategy_note}
    if base == "BATTLE_DETECTED":
        positions = ev.get("positions", [0, 0])
        return {"a": ids[0], "b": ids[1], "gap": ev["interval_s"],
                "p1": positions[0], "p2": positions[1]}
    if base == "SC_PIT_WINDOW":
        return {"a": ids[0], "age": ev["tyre_age_laps"]}
    return {}


def render(insight: dict[str, Any], lang: str = "en", level: str = "pro") -> str:
    base = next((b for b in _BASE_TYPES if insight["type"].startswith(b)), None)
    if base is None:
        return insight["type"]  # unknown types degrade gracefully
    template = TEMPLATES.get((base, lang, level)) or TEMPLATES[(base, "en", "pro")]
    text = template.format(**_params(base, insight))
    return f"MODEL · {text}" if base in _MODEL_TYPES else text


def render_all(insights: list[dict[str, Any]], lang: str = "en", level: str = "pro") -> list[dict]:
    return [
        {
            "insight_id": i["insight_id"],
            "severity": i["severity"],
            "lap": i["lap"],
            "text": render(i, lang, level),
        }
        for i in insights
    ]
