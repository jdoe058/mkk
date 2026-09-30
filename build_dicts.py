"""
Разбор dump.json → vacancies_dict.json

Цепочка связей:
  vacancy.vacancy_category  →  vacancy_list[id]          (номенклатура)
  vacancy_list[id].category →  categories[id]            (категория)
  vacancy.organization      →  organizations[id]         (учреждение)

Выход — единый файл:
  {
    "categories":    {id: {..., vlist_count, vlist_ids, vacancy_count, vacancy_ids}},
    "vacancy_list":  {id: {..., category: {…развёрнутая категория…},
                                vacancy_count, vacancy_ids}},
    "organizations": {id: {..., vacancy_count, vacancy_ids}},
    "vacancies":     {id: {..., vacancy_category: {…развёрнутая номенклатура…},
                                organization:       {…развёрнутая организация…}}}
  }
"""

import json
import re
import sys

IN_FILE  = "dump.json"
OUT_FILE = "vacancies_dict.json"


# ---------- утилиты ----------
def load_dump(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"ERROR: {path} не найден", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: {path} не валидный JSON: {e}", file=sys.stderr)
        sys.exit(1)


def status_parts(status) -> tuple[str | None, str | None]:
    if isinstance(status, dict):
        return status.get("value"), status.get("label")
    if isinstance(status, str):
        return status, None
    return None, None


def field_value(field, default=None):
    if isinstance(field, dict):
        return field.get("value", default)
    if isinstance(field, str):
        return field
    return default


def id_from_url(url: str) -> int | None:
    m = re.search(r"[?&]id=(\d+)", url or "")
    return int(m.group(1)) if m is not None else None


def to_int(v) -> int | None:
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.isdigit():
        return int(v)
    return None


def write_json(obj, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)


def strip_counters(d: dict) -> dict:
    """Копия без служебных счётчиков — для вкладывания в вакансии и vacancy_list."""
    return {k: v for k, v in d.items() if k not in ("vacancy_count", "vacancy_ids",
                                                    "vlist_count", "vlist_ids")}


# ---------- 1. категории ----------
def build_categories(data: dict) -> dict[str, dict]:
    items = (data.get("categories") or {}).get("items") or []
    result: dict[str, dict] = {}

    for row in items:
        cat_id = row.get("id")
        if not isinstance(cat_id, int):
            continue
        detail = row.get("detail") or {}
        if "error" in detail:
            continue

        form = (detail.get("form") or {}).get("VacancyCategory") or {}
        status_value, status_label = status_parts(form.get("status"))

        result[str(cat_id)] = {
            "name":          form.get("name"),
            "status":        status_value,
            "status_label":  status_label,
            "vlist_count":   0,
            "vlist_ids":     [],
            "vacancy_count": 0,
            "vacancy_ids":   [],
        }

    return result


# ---------- 2. номенклатура должностей ----------
def build_vacancy_list(
    data: dict,
    cats: dict[str, dict],
) -> tuple[dict[str, dict], list[tuple[int, int]]]:
    """
    Возвращает (vlist, orphan_cats_from_vlist).
    orphan_cats_from_vlist = [(vlist_id, category_id), ...]
    """
    items = (data.get("vacancy_list") or {}).get("items") or []
    result: dict[str, dict] = {}
    orphan_cats: list[tuple[int, int]] = []

    for row in items:
        item_id = row.get("id")
        if not isinstance(item_id, int):
            continue
        detail = row.get("detail") or {}
        if "error" in detail:
            continue

        form = (detail.get("form") or {}).get("VacancyList") or {}
        meta = detail.get("meta") or {}

        # status (radio → dict или строка)
        status_value, status_label = status_parts(form.get("status"))

        # category (select → {"value": "1", "label": "Врач"})
        cat_id = to_int(field_value(form.get("category")))
        cat_info: dict | None = None
        if cat_id is not None:
            cat_info = cats.get(str(cat_id))
            if cat_info is not None:
                cat_info["vlist_count"] = (cat_info.get("vlist_count") or 0) + 1
                cat_info["vlist_ids"].append(item_id)
            else:
                orphan_cats.append((item_id, cat_id))

        result[str(item_id)] = {
            "id":           item_id,
            "name":         form.get("name") or row.get("name"),
            "category": (
                {"id": cat_id, **strip_counters(cat_info)}
                if cat_info is not None
                else ({"id": cat_id} if cat_id is not None else None)
            ),
            "status":       status_value,
            "status_label": status_label,
            "sort":         form.get("sort"),
            "created_at":   meta.get("created_at") or row.get("created_at"),
            "updated_at":   meta.get("updated_at"),
            "creator":      meta.get("creator"),
            "vacancy_count": 0,
            "vacancy_ids":   [],
        }

    return result, orphan_cats


# ---------- 3. организации ----------
def build_organizations(data: dict) -> dict[str, dict]:
    items = (data.get("organizations") or {}).get("items") or []
    result: dict[str, dict] = {}

    for row in items:
        detail = row.get("detail") or {}
        if "error" in detail:
            continue

        org_id = id_from_url(detail.get("url") or "")
        if org_id is None:
            org_id = row.get("id") if isinstance(row.get("id"), int) else None
        if org_id is None:
            continue

        form = (detail.get("form") or {}).get("Organization") or {}
        status_value, status_label = status_parts(form.get("status"))

        result[str(org_id)] = {
            "name":                  form.get("name"),
            "short_name":            form.get("short_name"),
            "status":                status_value,
            "status_label":          status_label,
            "address":               form.get("address"),
            "website":               form.get("website"),
            "personnel_dep_address": form.get("personnel_dep_address"),
            "personnel_dep_contact": form.get("personnel_dep_contact"),
            "personnel_dep_phone":   form.get("personnel_dep_phone"),
            "personnel_dep_email":   form.get("personnel_dep_email"),
            "vacancy_count":         0,
            "vacancy_ids":           [],
        }

    return result


# ---------- 4. вакансии ----------
def build_vacancies(
    data: dict,
    cats: dict[str, dict],
    vlist: dict[str, dict],
    orgs: dict[str, dict],
) -> tuple[dict[str, dict], dict]:
    items = (data.get("vacancies") or {}).get("items") or []
    result: dict[str, dict] = {}

    used_cat: set[int] = set()
    used_vlist: set[int] = set()
    used_org: set[int] = set()

    orphan_vlist: list[tuple[int, int]] = []      # vacancy → vacancy_list
    orphan_org:   list[tuple[int, int]] = []      # vacancy → organization

    for row in items:
        vac_id = row.get("id")
        if not isinstance(vac_id, int):
            continue
        detail = row.get("detail") or {}
        if "error" in detail:
            continue

        form = (detail.get("form") or {}).get("Vacancy") or {}

        # --- vacancy_category → vacancy_list ---
        vl_id = to_int(form.get("vacancy_category"))
        vl_info: dict | None = None
        if vl_id is not None:
            vl_info = vlist.get(str(vl_id))
            if vl_info is not None:
                used_vlist.add(vl_id)
                vl_info["vacancy_count"] = (vl_info.get("vacancy_count") or 0) + 1
                vl_info["vacancy_ids"].append(vac_id)

                # категория этой номенклатуры → vacancy_count у categories
                cat_ref = vl_info.get("category")
                if isinstance(cat_ref, dict):
                    cat_id = cat_ref.get("id")
                    if isinstance(cat_id, int):
                        cat_info = cats.get(str(cat_id))
                        if cat_info is not None:
                            used_cat.add(cat_id)
                            cat_info["vacancy_count"] = (cat_info.get("vacancy_count") or 0) + 1
                            cat_info["vacancy_ids"].append(vac_id)
            else:
                orphan_vlist.append((vac_id, vl_id))

        # --- organization ---
        org_raw = form.get("organization")
        if isinstance(org_raw, dict):
            org_id = to_int(org_raw.get("value"))
        else:
            org_id = to_int(org_raw)

        org_info: dict | None = None
        if org_id is not None:
            org_info = orgs.get(str(org_id))
            if org_info is not None:
                used_org.add(org_id)
                org_info["vacancy_count"] = (org_info.get("vacancy_count") or 0) + 1
                org_info["vacancy_ids"].append(vac_id)
            else:
                orphan_org.append((vac_id, org_id))

        result[str(vac_id)] = {
            "id":   vac_id,
            "name": form.get("name"),

            "vacancy_category": (
                {"id": vl_id, **strip_counters(vl_info)}
                if vl_info is not None
                else ({"id": vl_id} if vl_id is not None else None)
            ),

            "work_mode":            form.get("work_mode"),
            "mentors":              field_value(form.get("mentors")),
            "subdivision":          form.get("subdivision"),
            "payment_main":         form.get("payment_main"),
            "housing":              field_value(form.get("housing")),
            "social_land":          field_value(form.get("social_land")),
            "social_communal":      field_value(form.get("social_communal")),
            "social_rent":          field_value(form.get("social_rent")),
            "social_mortgage":      field_value(form.get("social_mortgage")),
            "social_deposit":       field_value(form.get("social_deposit")),
            "zemskii":              field_value(form.get("zemskii")),
            "zemskii_sum":          form.get("zemskii_sum"),
            "status":               field_value(form.get("status")),

            "organization": (
                {"id": org_id, **strip_counters(org_info)}
                if org_info is not None
                else ({"id": org_id} if org_id is not None else None)
            ),

            "diplom":               form.get("diplom"),
            "certificate":          form.get("certificate"),
            "additional_training":  form.get("additional_training"),
            "payment_stimulating":  form.get("payment_stimulating"),
            "payment_other":        form.get("payment_other"),
        }

    stats = {
        "used_categories":      used_cat,
        "used_vacancy_list":    used_vlist,
        "used_organizations":   used_org,
        "orphan_vlist":         orphan_vlist,
        "orphan_organizations": orphan_org,
    }
    return result, stats


# ---------- консольный отчёт ----------
def print_unused(
    cats: dict[str, dict],
    vlist: dict[str, dict],
    orgs: dict[str, dict],
    stats: dict,
) -> None:
    all_cat_ids   = {int(k) for k in cats}
    all_vlist_ids = {int(k) for k in vlist}
    all_org_ids   = {int(k) for k in orgs}

    unused_cat   = sorted(all_cat_ids - stats["used_categories"])
    unused_vlist = sorted(all_vlist_ids - stats["used_vacancy_list"])
    unused_org   = sorted(all_org_ids - stats["used_organizations"])

    print()
    print(f"Незадействованные категории ({len(unused_cat)}):")
    for cid in unused_cat:
        info = cats.get(str(cid)) or {}
        print(f"  [{cid}] {info.get('name')}  (status={info.get('status')})")

    print()
    print(f"Номенклатуры без вакансий ({len(unused_vlist)}):")
    for vid in unused_vlist:
        info = vlist.get(str(vid)) or {}
        cat = info.get("category") or {}
        print(f"  [{vid}] {info.get('name')}  (категория: {cat.get('name')})")

    print()
    print(f"Незадействованные организации ({len(unused_org)}):")
    for oid in unused_org:
        info = orgs.get(str(oid)) or {}
        print(f"  [{oid}] {info.get('short_name') or info.get('name')}")

    oc = stats["orphan_vlist"]
    oo = stats["orphan_organizations"]
    if oc or oo:
        print()
        print("Сиротские ссылки (в вакансиях есть id, которого нет в справочнике):")
        if oc:
            print(f"  vacancy → vacancy_list: {len(oc)}")
            for vac_id, vid in oc[:10]:
                print(f"    vacancy={vac_id} → vacancy_list_id={vid}")
        if oo:
            print(f"  vacancy → organization: {len(oo)}")
            for vac_id, oid in oo[:10]:
                print(f"    vacancy={vac_id} → organization_id={oid}")


# ---------- main ----------
def main() -> int:
    data = load_dump(IN_FILE)

    cats = build_categories(data)
    vlist, orphan_cats_from_vlist = build_vacancy_list(data, cats)
    orgs = build_organizations(data)
    vacs, stats = build_vacancies(data, cats, vlist, orgs)

    output = {
        "categories":    cats,
        "vacancy_list":  vlist,
        "organizations": orgs,
        "vacancies":     vacs,
    }
    write_json(output, OUT_FILE)

    print(f"OK: {OUT_FILE}")
    print(f"    категорий:      {len(cats)}")
    print(f"    номенклатур:    {len(vlist)}")
    print(f"    организаций:    {len(orgs)}")
    print(f"    вакансий:       {len(vacs)}")

    if orphan_cats_from_vlist:
        print(f"    vacancy_list → categories: битых ссылок {len(orphan_cats_from_vlist)}",
              file=sys.stderr)
        for vl_id, cid in orphan_cats_from_vlist[:10]:
            print(f"      vlist={vl_id} → category_id={cid}", file=sys.stderr)

    print_unused(cats, vlist, orgs, stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())