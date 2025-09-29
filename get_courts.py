#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import csv
import random
import sqlite3
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

import httpx
from bs4 import BeautifulSoup
from tenacity import (
    RetryCallState,
    retry,
    stop_after_attempt,
    wait_random,
    wait_random_exponential,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0",
    "Accept-Encoding": "gzip, deflate, br, zstd",
}

REGION_LIST_URL = (
    "https://sudrf.ru/index.php?id=300&act=go_search&searchtype=fs"
    "&court_name=&court_subj=0&court_type=0&court_okrug=0&vcourt_okrug=0"
)
SUBJ_LINK = (
    "https://sudrf.ru/index.php?id=300&act=go_search&searchtype=fs"
    "&court_name=&court_subj={code}&court_type=0&court_okrug=0&vcourt_okrug=0"
).format

PROXY_FILE = Path("proxy")
COURTS_CSV = Path("courts.csv")
DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "monitor.sqlite"

PROXIES: Sequence[str] = ()


def load_proxies() -> Sequence[str]:
    if not PROXY_FILE.exists():
        return ()
    proxies: List[str] = []
    for raw in PROXY_FILE.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if not value:
            continue
        if not value.startswith("http://") and not value.startswith("https://"):
            value = f"http://{value}"
        proxies.append(value)
    return proxies


def pick_proxy() -> str:
    if not PROXIES:
        raise RuntimeError("Proxy list is empty")
    return random.choice(PROXIES)


def log_retry(retry_state: RetryCallState) -> None:
    try:
        region_code = retry_state.args[0] if retry_state.args else "<?>"
        url = SUBJ_LINK(str(region_code))
    except Exception:
        url = "<не удалось собрать URL>"

    attempt = getattr(retry_state, "attempt_number", None)
    sleep = getattr(getattr(retry_state, "next_action", None), "sleep", None)
    exc = None
    try:
        exc = retry_state.outcome.exception()
    except Exception:
        pass

    message = f"Ретрай попытка {attempt}/10 для {url}"
    if isinstance(sleep, (int, float)):
        message += f" (пауза {sleep:.1f} сек)"
    if exc:
        message += f": {exc!r}"
    print(message)


def write_csv(rows: Iterable[Tuple[str, str]]) -> None:
    with COURTS_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        for code, url in rows:
            writer.writerow([code, url])
    print(f"CSV обновлён: {COURTS_CSV.resolve()}")


def sync_db(rows: Sequence[Tuple[str, str]]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS courts (
                code TEXT PRIMARY KEY,
                url TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO courts(code, url) VALUES(?, ?)\n"
            "ON CONFLICT(code) DO UPDATE SET url=excluded.url",
            rows,
        )
        if rows:
            codes = [code for code, _ in rows]
            placeholders = ",".join("?" for _ in codes)
            conn.execute(
                f"DELETE FROM courts WHERE code NOT IN ({placeholders})",
                codes,
            )
        else:
            conn.execute("DELETE FROM courts")
        conn.commit()
    print(f"SQLite обновлён: {DB_PATH.resolve()}")


async def fetch_region_codes() -> List[str]:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(25.0, read=35.0),
        verify=False,
        follow_redirects=True,
        headers=HEADERS,
        proxy=pick_proxy(),
    ) as client:
        resp = await client.get(REGION_LIST_URL)
        resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    select = soup.find(id="court_subj")
    if not select:
        raise RuntimeError("Не найден список регионов court_subj")
    codes = [opt["value"] for opt in select.find_all("option") if opt.get("value")]
    filtered = [code for code in codes if code not in {"0", "97"}]
    print(f"Получено кодов регионов: {len(filtered)}")
    return filtered


@retry(
    wait=wait_random_exponential(multiplier=5, max=30) + wait_random(1, 3),
    stop=stop_after_attempt(10),
    reraise=True,
    before_sleep=log_retry,
)
async def get_courts_for_region(code: str) -> List[Tuple[str, str]]:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(25.0, read=35.0),
        verify=False,
        follow_redirects=True,
        headers=HEADERS,
        proxy=pick_proxy(),
    ) as client:
        resp = await client.get(SUBJ_LINK(code=code))
        resp.raise_for_status()

    print(f"[{code}] HTTP {resp.status_code}")
    soup = BeautifulSoup(resp.text, "lxml")
    ul = soup.find("ul", class_="search-results")
    if not ul:
        print(f"[{code}] предупреждение: блок результатов не найден")
        return []

    items = ul.find_all("li")
    result: List[Tuple[str, str]] = []
    for li in items:
        b_tag = li.find("b", string="Классификационный код:")
        link_tag = li.find("a", target="_blank")
        class_code = b_tag.next_sibling.strip() if b_tag and b_tag.next_sibling else ""
        href = link_tag["href"].strip() if link_tag and link_tag.get("href") else ""
        if class_code and href:
            result.append((class_code, href))
    return result


async def fetch_all_courts(codes_list, concurrency):
    sem = asyncio.Semaphore(concurrency)

    async def _wrapped(code):
        async with sem:
            pairs = await get_courts_for_region(code)
            return [(cls, href) for (cls, href) in pairs]

    tasks = [_wrapped(code) for code in codes_list]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    ok = []
    failed = []
    for code, r in zip(codes_list, results):
        if isinstance(r, Exception):
            failed.append(code)
        else:
            ok.extend(r)
    return ok, failed


async def fetch_all_regions_rounds(codes_list, max_rounds, concurrency):
    remaining = list(codes_list)
    total_ok = []

    for round_idx in range(1, max_rounds + 1):
        if not remaining:
            print(f"[ROUND {round_idx}] Всё уже собрано — новых регионов нет.")
            break

        print(f"[ROUND {round_idx}] Старт. Осталось регионов: {len(remaining)}")
        ok, failed = await fetch_all_courts(
            remaining,
            concurrency=concurrency,
        )
        total_ok.extend(ok)

        print(f"[ROUND {round_idx}] Успехов: {len(ok)}, не собрались: {len(failed)}")

        if not failed:
            print(f"[ROUND {round_idx}] Все регионы собраны.")
            remaining = []
            break

        remaining = failed  # на следующий раунд пойдут только провалившиеся

        # Экспоненциальная пауза перед повтором (с джиттером)
        cooldown = 5 * (2 ** (round_idx - 1))
        cooldown += random.uniform(0.5, 1.5)
        print(f"[ROUND {round_idx}] Пауза перед следующим раундом: ~{cooldown:.1f} сек")
        await asyncio.sleep(cooldown)

    if remaining:
        print(f"[FINAL] Не удалось собрать регионы: {remaining}")

    if remaining:
        print(f"[FINAL] Не удалось собрать регионы: {remaining}")

    return total_ok, remaining


def deduplicate(rows: Iterable[Tuple[str, str]]) -> List[Tuple[str, str]]:
    unique: dict[str, str] = {}
    for code, url in rows:
        code = code.strip()
        url = url.strip()
        if not code or not url:
            continue
        unique[code] = url
    return sorted(unique.items())


async def main() -> None:
    global PROXIES
    PROXIES = load_proxies()
    if not PROXIES:
        raise SystemExit("Файл proxy пуст или отсутствует")

    codes = await fetch_region_codes()
    all_rows, failed = await fetch_all_regions_rounds(
        codes, max_rounds=3, concurrency=6
    )
    rows = deduplicate(all_rows)
    print(f"Собрано записей: {len(rows)}")
    if failed:
        print(f"Не собраны регионы: {failed}")

    write_csv(rows)
    sync_db(rows)


if __name__ == "__main__":
    asyncio.run(main())
