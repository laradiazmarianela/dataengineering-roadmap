#!/usr/bin/env python3
"""CLI para autenticar y descargar empleados desde la API de Rankmi."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Sequence

import requests

AUTH_URL = "https://rankmi-api.rankmi.com/v1/auth"
EMPLOYEES_URL = "https://rankmi-api.rankmi.com/v1/payroll/employees"
DEFAULT_TIMEOUT = 30
DEFAULT_PAGE_SIZE = 200


class RankmiAuthError(RuntimeError):
    """Error personalizado para fallos de autenticacion."""


@dataclass
class PageResult:
    page: int
    employees: List[Dict[str, Any]]
    raw_payload: Dict[str, Any]


class RankmiClient:
    """Cliente liviano para la API de Rankmi."""

    def __init__(self, uid: str, secret_key: str, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.uid = uid
        self.secret_key = secret_key
        self.timeout = timeout
        self._session = requests.Session()
        self._token: Optional[str] = None

    def authenticate(self, force: bool = False) -> str:
        """Obtiene el token JWT con las credenciales entregadas."""
        if self._token and not force:
            return self._token

        params = {"uid": self.uid, "secretKey": self.secret_key}
        response = self._session.post(AUTH_URL, params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        token = payload.get("token") or payload.get("accessToken") or payload.get("jwt")
        if not token:
            raise RankmiAuthError(
                "No se encontro el token en la respuesta de autenticacion. "
                f"Respuesta: {json.dumps(payload, ensure_ascii=False)}"
            )
        self._token = token
        return token

    def fetch_employee_page(
        self,
        page: int,
        page_size: int,
        country: Optional[str],
    ) -> Dict[str, Any]:
        """Descarga una pagina de empleados."""
        token = self.authenticate()
        headers = {"Authorization": f"Bearer {token}"}
        params: Dict[str, Any] = {"page": page, "pageSize": page_size}
        if country:
            params["country"] = country

        response = self._session.get(
            EMPLOYEES_URL,
            headers=headers,
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def iter_employee_pages(
        self,
        country: Optional[str],
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: Optional[int] = None,
        delay_seconds: float = 0,
    ) -> Iterator[PageResult]:
        """Itera sobre paginas de empleados hasta agotar los datos o alcanzar max_pages."""
        page = 1
        pages_yielded = 0

        while True:
            if max_pages is not None and pages_yielded >= max_pages:
                break

            payload = self.fetch_employee_page(page=page, page_size=page_size, country=country)
            employees = extract_employees(payload)
            yield PageResult(page=page, employees=employees, raw_payload=payload)

            pages_yielded += 1
            if not should_continue(payload, employees, page, page_size):
                break

            page += 1
            if delay_seconds > 0:
                time.sleep(delay_seconds)


def extract_employees(payload: Any) -> List[Dict[str, Any]]:
    """Intenta inferir la lista de empleados dentro de la respuesta."""
    candidate_lists: Sequence[str] = (
        "employees",
        "data",
        "results",
        "items",
        "payload",
    )
    if isinstance(payload, list):
        return [ensure_dict(item) for item in payload]

    if isinstance(payload, dict):
        for key in candidate_lists:
            value = payload.get(key)
            if isinstance(value, list):
                return [ensure_dict(item) for item in value]

    return []


def ensure_dict(item: Any) -> Dict[str, Any]:
    """Convierte cada entrada en dict si es posible."""
    if isinstance(item, dict):
        return item
    return {"value": item}


def should_continue(
    payload: Any,
    employees: Sequence[Dict[str, Any]],
    current_page: int,
    page_size: int,
) -> bool:
    """Determina si probablemente existan mas paginas."""
    pagination = payload.get("pagination") if isinstance(payload, dict) else None
    if isinstance(pagination, dict):
        total_pages = pagination.get("totalPages") or pagination.get("pages")
        if total_pages:
            return current_page < int(total_pages)
        has_more = pagination.get("hasMore") or pagination.get("has_next")
        if isinstance(has_more, bool):
            return has_more
        next_page = pagination.get("nextPage") or pagination.get("next")
        if next_page:
            return True

    return len(employees) == page_size and len(employees) > 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Autentica y descarga empleados desde la API payroll de Rankmi.",
    )
    parser.add_argument(
        "--uid",
        default=os.getenv("RANKMI_UID"),
        help="UID provisto por Rankmi (o variable de entorno RANKMI_UID).",
    )
    parser.add_argument(
        "--secret-key",
        default=os.getenv("RANKMI_SECRET_KEY"),
        help="Secret Key provista por Rankmi (o variable de entorno RANKMI_SECRET_KEY).",
    )
    parser.add_argument(
        "--country",
        help="Filtro de pais (por ejemplo, CL, MX, PE).",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=f"Tamano de pagina a solicitar (default: {DEFAULT_PAGE_SIZE}).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        help="Limite de paginas a descargar; si no se indica, descarga todas las disponibles.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0,
        help="Segundos a esperar entre llamadas sucesivas para evitar rate limiting.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Timeout HTTP en segundos (default: {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="Archivo destino en formato JSON Lines; usa '-' para stdout.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Imprime JSON indentado (solo recomendable para depurar).",
    )

    args = parser.parse_args(argv)
    if not args.uid or not args.secret_key:
        parser.error(
            "Debes proporcionar --uid y --secret-key o definir RANKMI_UID y RANKMI_SECRET_KEY."
        )
    return args


def open_output(path: str):
    if path == "-" or path.lower() == "stdout":
        return sys.stdout
    return open(path, "w", encoding="utf-8")


def dump_employees(
    client: RankmiClient,
    *,
    country: Optional[str],
    page_size: int,
    max_pages: Optional[int],
    delay: float,
    output_path: str,
    pretty: bool,
) -> int:
    """Descarga los empleados y los escribe como JSON Lines."""
    total = 0
    indent = 2 if pretty else None
    with open_output(output_path) as handle:
        for result in client.iter_employee_pages(
            country=country,
            page_size=page_size,
            max_pages=max_pages,
            delay_seconds=delay,
        ):
            for employee in result.employees:
                json.dump(employee, handle, ensure_ascii=False, indent=indent)
                handle.write("\n")
                total += 1
            print(
                f"[Rankmi] Pagina {result.page} -> {len(result.employees)} registros",
                file=sys.stderr,
            )
    return total


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    client = RankmiClient(uid=args.uid, secret_key=args.secret_key, timeout=args.timeout)
    try:
        total = dump_employees(
            client,
            country=args.country,
            page_size=args.page_size,
            max_pages=args.max_pages,
            delay=args.delay,
            output_path=args.output,
            pretty=args.pretty,
        )
    except requests.HTTPError as exc:
        print(f"Error HTTP al llamar a Rankmi: {exc}", file=sys.stderr)
        if exc.response is not None:
            print(exc.response.text, file=sys.stderr)
        return 1
    except RankmiAuthError as exc:
        print(f"Error de autenticacion: {exc}", file=sys.stderr)
        return 2
    except requests.RequestException as exc:
        print(f"Fallo de red hacia Rankmi: {exc}", file=sys.stderr)
        return 3

    print(f"[Rankmi] Total de empleados descargados: {total}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
