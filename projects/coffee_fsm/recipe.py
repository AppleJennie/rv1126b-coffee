#!/usr/bin/env python3
# recipe.py —— 配方引擎（TASK 4）
# 读取 config/recipes.yaml，按 menu.json 的 drink id 查配方。
# 业务层只问引擎要 Recipe，不允许在流程代码里按饮品名写分支。

import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_PATH = os.path.join(ROOT, "config", "recipes.yaml")

_REQUIRED = ("name", "dose_g", "grind_sec", "brew_sec", "arm_sequence")


class RecipeError(Exception):
    pass


class Recipe:
    """一份饮品配方（不可变）。"""

    def __init__(self, slug, data):
        for k in _REQUIRED:
            if k not in data:
                raise RecipeError(f"配方 {slug} 缺少字段 {k}")
        self.slug = slug
        self.name = data["name"]
        self.match_ids = list(data.get("match_ids") or [])
        self.dose_g = float(data["dose_g"])
        self.grind_sec = float(data["grind_sec"])
        self.water_ml = float(data.get("water_ml", 0))
        self.temp_c = float(data.get("temp_c", 0))
        self.brew_sec = float(data["brew_sec"])
        self.arm_sequence = list(data["arm_sequence"])
        if self.dose_g > 0 and self.grind_sec <= 0:
            raise RecipeError(f"配方 {slug}: dose_g>0 但 grind_sec<=0")
        if not self.arm_sequence:
            raise RecipeError(f"配方 {slug}: arm_sequence 为空")

    @property
    def needs_grinder(self):
        return self.dose_g > 0

    def summary(self):
        return (f"{self.name}({self.slug}): 粉 {self.dose_g}g 磨 {self.grind_sec}s "
                f"水 {self.water_ml}ml {self.temp_c}°C 冲 {self.brew_sec}s "
                f"臂动作 {len(self.arm_sequence)} 步")

    def __repr__(self):
        return f"<Recipe {self.slug}>"


class RecipeEngine:
    """配方查询：drink_id -> Recipe（无匹配回 default）。"""

    def __init__(self, path=DEFAULT_PATH):
        with open(path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        if not doc or "recipes" not in doc:
            raise RecipeError(f"{path}: 缺少 recipes 段")
        self.default_slug = doc.get("default")
        self.recipes = {slug: Recipe(slug, d) for slug, d in doc["recipes"].items()}
        if self.default_slug not in self.recipes:
            raise RecipeError(f"default 配方 {self.default_slug!r} 不存在")
        self._by_id = {}
        for r in self.recipes.values():
            for did in r.match_ids:
                if did in self._by_id:
                    raise RecipeError(f"drink id {did} 被 {self._by_id[did].slug} "
                                      f"和 {r.slug} 重复匹配")
                self._by_id[did] = r

    def for_drink(self, drink_id):
        """按 menu.json 的 drink id 查配方，无匹配回 default 配方。"""
        return self._by_id.get(drink_id) or self.recipes[self.default_slug]

    def __iter__(self):
        return iter(self.recipes.values())


if __name__ == "__main__":
    # 冒烟：加载 + 打印全部配方 + 查询示例
    eng = RecipeEngine()
    for r in eng:
        print(r.summary())
    for did in (1, 999):
        print(f"drink_id={did} -> {eng.for_drink(did).slug}")
