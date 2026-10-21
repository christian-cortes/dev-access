#!/usr/bin/env python3
"""Genera el portal estático desde backend/catalog.yml.

Uso:
    python scripts/build-portal.py \
      --catalog backend/catalog.yml \
      --templates portal/templates \
      --out portal/dist
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.catalog import load_catalog, public_catalog  # noqa: E402


def sha8(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


def copy_hashed(src, out):
    target = out / f"{src.stem}.{sha8(src)}{src.suffix}"
    shutil.copy2(src, target)
    return target.name


def initials(name):
    words = [word for word in str(name).split() if len(word) > 2]
    if not words:
        words = str(name).split()
    if len(words) == 1:
        return words[0].replace("-", "")[:2].upper()
    return "".join(word[0] for word in words[:2]).upper()


def display_url(url):
    return str(url).replace("https://", "").replace("http://", "").rstrip("/") or str(url)


def head_icon(service, portal_dir):
    icon = service.get("icon")
    if icon:
        name = Path(str(icon)).name
        if name == str(icon) and (portal_dir / "favicons" / name).is_file():
            return f"/favicons/{name}"
    for tech in service.get("tech") or []:
        name = str(tech).strip()
        if name and Path(name).name == name and (portal_dir / "icons" / f"{name}.svg").is_file():
            return f"/icons/{name}.svg"
    return None


def excluded(service, tokens):
    tech = service.get("tech") or []
    for token in tokens:
        if service["slug"] == token:
            return True
        if service["slug"].startswith(token + "-"):
            return True
        if token in tech:
            return True
    return False


def page_path(category):
    url = category.get("url") or ""
    if url.endswith(".html"):
        return url.lstrip("/")
    return f"{category['slug']}.html"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--templates", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    catalog = public_catalog(load_catalog(args.catalog))
    out = Path(args.out)
    portal_dir = Path(args.templates).parent
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    services = catalog.get("services", [])
    for service in services:
        tech = service.get("tech") or []
        if isinstance(tech, str):
            tech = [tech]
        service["tech"] = [str(item) for item in tech]
        service["initials"] = initials(service["name"])
        service["head_icon"] = head_icon(service, portal_dir)
        service["display_url"] = display_url(service["url"])
        service["badge"] = bool(service.get("badge", True))
        service["note"] = Markup(service.get("note") or "")

    categories = catalog.get("categories", [])
    for category in categories:
        members = [service for service in services if service["category"] == category["slug"]]
        category["services"] = members
        category["count"] = len(members)
        category["count_label"] = category.get("count_label", "servicios")
        category["note_header"] = category.get("note_header", "Nota")
        category["card_tech"] = category.get("card_tech") or []
        category["show_card"] = bool(category.get("show_card", True))
        category["show_table"] = bool(category.get("show_table", True))
        category["show_nav"] = bool(category.get("show_nav", True))
        category["description"] = Markup(category.get("description") or "")
        category["subtitle"] = Markup(category.get("subtitle") or "")
        category["intro_html"] = Markup(category.get("intro_html") or "")
        category["note_html"] = Markup(category.get("note_html") or "")

    home = catalog.get("home") or {}
    nav_categories = sorted(
        [category for category in categories if category["show_nav"]],
        key=lambda item: int(item.get("nav_order", 100)),
    )

    tokens = home.get("shortcut_exclude") or []
    shortcut_services = []
    seen = set()
    for slug in home.get("shortcut_categories") or []:
        for service in services:
            if service["category"] != slug or service["slug"] in seen:
                continue
            if excluded(service, tokens):
                continue
            seen.add(service["slug"])
            shortcut_services.append(service)

    def asset(src_name):
        return copy_hashed(portal_dir / src_name, out)

    style_asset = asset("style.css")
    app_asset = asset("app.js")
    render_asset = asset("render.js")
    theme_asset = asset("theme.js")
    asset_version = style_asset.rsplit(".", 2)[1]

    for name in ("login.html", "login.css", "login.js", "push-client.js", "firebase-messaging-sw.js"):
        shutil.copy2(portal_dir / name, out / name)
    for directory in ("vendor", "favicons", "icons"):
        shutil.copytree(portal_dir / directory, out / directory)

    env = Environment(
        loader=FileSystemLoader(args.templates),
        autoescape=select_autoescape(["html"]),
    )
    common = {
        "asset_version": asset_version,
        "style_asset": style_asset,
        "app_asset": app_asset,
        "render_asset": render_asset,
        "theme_asset": theme_asset,
        "nav_categories": nav_categories,
    }

    index_html = env.get_template("pages/index.html").render(
        **common,
        page_title="CortexDev · Índice interno",
        brand_title="CortexDev · Acceso interno",
        brand_subtitle="Infraestructura, aplicaciones y utilidades en <code>*.cortexdev.win</code>",
        active_nav="index",
        is_index=True,
        footer_note="ubuntu-services · access_nginx",
        categories=categories,
        home=home,
        shortcut_services=shortcut_services,
    )
    (out / "index.html").write_text(index_html, encoding="utf-8")

    for category in categories:
        if not category["show_table"]:
            continue
        html = env.get_template("pages/category.html").render(
            **common,
            page_title=f"{category['title']} · cortexdev.win",
            brand_title=category["title"],
            brand_subtitle=category.get("subtitle") or "",
            active_nav=category["slug"],
            is_index=False,
            footer_note=category["title"],
            category=category,
            services=category["services"],
        )
        target = out / page_path(category)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")

    notif = env.get_template("pages/notificaciones.html").render(
        **common,
        page_title="CortexDev · Notificaciones",
        brand_title="Notificaciones",
        brand_subtitle="Avisos push para <code>index.cortexdev.win</code>",
        active_nav="notificaciones",
        is_index=False,
        footer_note="Notificaciones",
    )
    (out / "notificaciones.html").write_text(notif, encoding="utf-8")

    print(f"portal generado en {out}: {len(services)} servicios, {len(categories)} categorías")


if __name__ == "__main__":
    main()
