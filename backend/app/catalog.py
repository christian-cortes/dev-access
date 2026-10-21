"""Carga, validación y proyección pública del catálogo."""
from __future__ import annotations

import copy
from urllib.parse import urlparse

import yaml

from .sanitize import sanitize_html

HTML_FIELDS = ("description", "intro_html", "note_html", "note")
CATEGORY_HTML_FIELDS = ("description", "subtitle", "intro_html", "note_html")


def load_catalog(path):
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    validate_catalog(data)
    return data


def _url_ok(value):
    if not value:
        return False
    parsed = urlparse(str(value))
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def validate_catalog(catalog):
    if not isinstance(catalog, dict):
        raise ValueError("el catálogo debe ser un objeto YAML")
    categories = catalog.get("categories") or []
    services = catalog.get("services") or []
    if not isinstance(categories, list) or not isinstance(services, list):
        raise ValueError("categories y services deben ser listas")

    category_slugs = set()
    for category in categories:
        slug = category.get("slug")
        if not slug:
            raise ValueError("categoría sin slug")
        if slug in category_slugs:
            raise ValueError(f"categoría duplicada: {slug}")
        category_slugs.add(slug)
        if not category.get("title"):
            raise ValueError(f"categoría sin title: {slug}")

    service_slugs = set()
    for service in services:
        slug = service.get("slug")
        if not slug:
            raise ValueError("servicio sin slug")
        if slug in service_slugs:
            raise ValueError(f"slug duplicado: {slug}")
        service_slugs.add(slug)
        if not service.get("name"):
            raise ValueError(f"servicio sin name: {slug}")
        if service.get("category") not in category_slugs:
            raise ValueError(f"categoría inexistente en {slug}: {service.get('category')}")
        if not _url_ok(service.get("url")):
            raise ValueError(f"URL inválida en {slug}")
        if service.get("probe_url") and not _url_ok(service["probe_url"]):
            raise ValueError(f"probe_url inválida en {slug}")
        method = service.get("method")
        if method is not None and str(method).upper() not in ("GET", "HEAD"):
            raise ValueError(f"method inválida en {slug}: {method}")
        accept = service.get("accept")
        if accept is not None:
            try:
                if isinstance(accept, (list, tuple)):
                    [int(code) for code in accept]
                else:
                    [int(part) for part in str(accept).split(",") if part.strip()]
            except (TypeError, ValueError):
                raise ValueError(f"accept inválido en {slug}") from None
        try:
            int(service.get("sort", 100))
        except (TypeError, ValueError):
            raise ValueError(f"sort inválido en {slug}") from None
    return catalog


def public_catalog(catalog):
    data = copy.deepcopy(catalog)
    for category in data.get("categories", []):
        for field in CATEGORY_HTML_FIELDS:
            if field in category:
                category[field] = sanitize_html(category[field])
    for service in data.get("services", []):
        for field in HTML_FIELDS:
            if field in service:
                service[field] = sanitize_html(service[field])
    home = data.get("home") or {}
    for field in ("notice_html",):
        if field in home:
            home[field] = sanitize_html(home[field])
    data["home"] = home
    return data
