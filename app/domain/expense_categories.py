EXPENSE_CATEGORY_PRESETS: tuple[str, ...] = (
    "Common",
    "Watchman Salary",
    "Common Electricity",
    "Others",
)

ADD_CATEGORY_VALUE = "__add__"

LEGACY_CATEGORY_MAP: dict[str, str] = {
    "general": "Common",
    "misc": "Common",
    "salary": "Watchman Salary",
}


def resolve_expense_category(category: str, category_other: str = "") -> str:
    if category == ADD_CATEGORY_VALUE:
        return category_other.strip() or "Others"
    return category.strip() or "Common"


def category_options(custom: list[str]) -> list[str]:
    """Presets first, then user-added categories (deduplicated)."""
    return list(EXPENSE_CATEGORY_PRESETS) + [
        c for c in custom if c not in EXPENSE_CATEGORY_PRESETS
    ]
