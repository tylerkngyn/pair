import json
from pathlib import Path

from app.practice.exercises import EXERCISES

RAW = json.loads(Path(__file__).with_name("catalog.json").read_text())
PROBLEMS = {}
for item in RAW["problems"]:
    exercise = EXERCISES.get(item["number"])
    if exercise:
        PROBLEMS[item["id"]] = {**item, **exercise}


def summaries():
    return [{k: p[k] for k in ("id", "number", "title", "category", "difficulty", "url")} | {"test_count": len(p["cases"])} for p in PROBLEMS.values()]
