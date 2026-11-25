"""Areas, access levels and role templates.

The MD (super admin) decides each user's access per area. Role templates only pre-fill
the matrix when a user is created; the MD can switch any area on or off afterwards.
"""

LEVELS = ["none", "own", "view", "edit"]
LEVEL_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}
LEVEL_LABELS = {
    "none": "No access",
    "own": "Own records",
    "view": "View all",
    "edit": "Edit all",
}

AREAS = {
    "dashboard": "Dashboard & performance",
    "customers": "Customers",
    "orders": "Orders & sourcing",
    "finance": "Invoices & payments",
    "expenses": "Expenses",
    "reports": "Plans, targets & reports",
    "tasks": "Tasks & assignments",
    "hr": "Staff & payroll",
    "tax": "Tax & VAT",
}

AREA_HELP = {
    "dashboard": "Own = personal performance only. View = company wide performance.",
    "customers": "Own = customers assigned to the user.",
    "orders": "Own = orders where the user is the sales or logistics officer.",
    "finance": "Own = invoices of the user's own customers.",
    "expenses": "Own = expenses the user submitted.",
    "reports": "Own = submit own daily reports and see own targets.",
    "tasks": "Own = personal to do and schedule. Edit = can also assign tasks to others.",
    "hr": "Own = own payslips only.",
    "tax": "View or edit the tax and VAT summaries.",
}

ROLES = {
    "shareholder": "Shareholder / Director",
    "office_admin": "Office Admin",
    "sales": "Sales Executive / Officer",
    "logistics": "Logistics Officer",
    "hr": "Human Resource Officer",
}

ROLE_TEMPLATES = {
    "shareholder": {"dashboard": "view", "customers": "view", "orders": "view", "finance": "view",
                    "expenses": "view", "reports": "view", "tasks": "own", "hr": "view", "tax": "view"},
    "office_admin": {"dashboard": "own", "customers": "view", "orders": "edit", "finance": "edit",
                     "expenses": "edit", "reports": "own", "tasks": "own", "hr": "own", "tax": "edit"},
    "sales": {"dashboard": "own", "customers": "own", "orders": "own", "finance": "own",
              "expenses": "own", "reports": "own", "tasks": "own", "hr": "own", "tax": "none"},
    "logistics": {"dashboard": "own", "customers": "none", "orders": "own", "finance": "none",
                  "expenses": "own", "reports": "own", "tasks": "own", "hr": "own", "tax": "none"},
    "hr": {"dashboard": "own", "customers": "none", "orders": "none", "finance": "none",
           "expenses": "own", "reports": "own", "tasks": "own", "hr": "edit", "tax": "none"},
}


def template_for_roles(roles: list[str]) -> dict[str, str]:
    """A user with several roles gets the highest level each role allows."""
    result = {area: "none" for area in AREAS}
    for role in roles:
        for area, lvl in ROLE_TEMPLATES.get(role, {}).items():
            if LEVEL_RANK[lvl] > LEVEL_RANK[result[area]]:
                result[area] = lvl
    result["tasks"] = result["tasks"] if result["tasks"] != "none" else "own"
    return result


def level_of(user, area: str) -> str:
    if user.is_superadmin:
        return "edit"
    for p in user.permissions:
        if p.area == area:
            return p.level if p.level in LEVEL_RANK else "none"
    return "none"


def at_least(user, area: str, minimum: str) -> bool:
    return LEVEL_RANK[level_of(user, area)] >= LEVEL_RANK[minimum]
