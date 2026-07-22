"""Реальные порты reconciliation sweeper (go-live).

- `has_completed_review` = «нет DONE-строки в сторе → reconcile» И (опционально)
  подтверждение DONE-строки артефактом на самом GitHub. Без `verify` — store-only
  (ловит пропущенные webhook'и / необработанные / застрявшие PR). С `verify` —
  DONE в сторе перепроверяется против GitHub: если артефакта ревью нет, это
  «проглоченный» сбой (pr-agent вернулся штатно, но ничего не опубликовал) →
  reconcile. Сам предикат `verify` инъектируется, чтобы точную эвристику артефакта
  (какой коммент/ревью считать доказательством) можно было донастроить на смоуке
  без правки логики свипера.
- `list_open_prs` — открытые PR по настроенным репозиториям (GitHub API).

Парсинг и композиция тестируемы; реальные HTTP-вызовы — pragma.
"""
from __future__ import annotations

from typing import Callable, Optional

from reliability.sweeper import OpenPR, business_key
from reliability.state import StateStore

# (repo, number, head_sha, command) -> артефакт ревью присутствует на GitHub
VerifyReview = Callable[[str, int, str, str], bool]


def parse_open_prs(pulls_json: list, repo: str) -> list:
    out = []
    for pr in pulls_json:
        number = pr.get("number")
        head_sha = (pr.get("head") or {}).get("sha")
        if number is not None and head_sha:
            out.append(OpenPR(repo=repo, number=int(number), head_sha=head_sha))
    return out


def make_has_completed_review(
    store: StateStore, verify: Optional[VerifyReview] = None
) -> Callable[[str, int, str, str], bool]:
    def has_completed_review(repo: str, number: int, head_sha: str, command: str) -> bool:
        if not store.already_done(business_key(repo, number, head_sha, command)):
            return False              # нет DONE-строки → точно не сделано → reconcile
        if verify is None:
            return True               # store-only: доверяем DONE-строке
        # DONE в сторе есть — подтверждаем реальным артефактом (ловим проглоченный сбой)
        return verify(repo, number, head_sha, command)
    return has_completed_review


def make_github_review_verifier(client) -> VerifyReview:  # pragma: no cover - реальный GitHub
    """Опорный `verify`: доказательством ревью считаем наличие активности бота на PR.
    Точную эвристику (formal review vs. коммент, привязка к head_sha) донастроить на
    смоуке — здесь только композиция, чтобы порт можно было заменить не трогая свипер."""
    def verify(repo: str, number: int, head_sha: str, command: str) -> bool:
        return client.has_bot_activity(repo, number)
    return verify


def make_list_open_prs(client, repos):  # pragma: no cover - реальные вызовы GitHub
    def list_open_prs():
        prs = []
        for repo in repos:
            prs.extend(parse_open_prs(client.list_open_pulls(repo), repo))
        return prs
    return list_open_prs


def parse_repo_specs(repos):
    """Делит записи RELIABILITY_REPOS на точные репо и маски.

    Каждая запись — одно из:
      * `owner/repo` — конкретный репозиторий (как раньше);
      * `owner/*`    — маска: все репозитории установки App на этот owner (орг/аккаунт);
      * `owner`      — голый owner без `/repo`: тоже маска `owner/*` (в контексте свипера
                       у записи без `/` нет иного валидного смысла — как `owner/repo` она
                       всегда даёт 401 на `/repos/{owner}/installation`);
      * `*`          — все репозитории всех установок App (то же, что пустой RELIABILITY_REPOS).

    Возвращает `(concrete, mask_owners)`: `concrete` — список `owner/repo`;
    `mask_owners` — список owner'ов для раскрытия (для `*` кладём маркер `"*"`).
    Пустые записи игнорируются. Чистая функция → тестируется без сети."""
    concrete, mask_owners = [], []
    for spec in repos:
        spec = spec.strip()
        if not spec:
            continue
        if spec == "*":
            mask_owners.append("*")
        elif spec.endswith("/*"):
            mask_owners.append(spec[: -len("/*")])
        elif "/" not in spec:
            mask_owners.append(spec)  # голый owner → маска owner/* (не валиден как owner/repo)
        else:
            concrete.append(spec)
    return concrete, mask_owners


def resolve_masked_repos(mask_owners, provider, client):
    """Раскрывает маски `owner/*` в реальные `owner/repo` через установки App.

    `owner/*` → репозитории установки App на этот owner; `*` → репо всех установок.
    Owner сопоставляется по `installation.account.login` (регистронезависимо).
    Неизвестный owner (App не установлен на орг) пропускается — свипер не должен
    падать/молчать из-за одной несуществующей маски; живые ревью и так идут по
    webhook. Вызывается на КАЖДОМ проходе свипера → новые репо орг подхватываются
    автоматически, список руками вести не нужно.

    `provider`/`client` инъектируются → раскрытие тестируется без сети."""
    if not mask_owners:
        return []
    want_all = "*" in mask_owners
    wanted = {o.lower() for o in mask_owners if o != "*"}
    out = []
    for inst in provider.list_installations():
        login = ((inst.get("account") or {}).get("login") or "")
        if want_all or login.lower() in wanted:
            token = provider.token_for(inst["id"])
            out.extend(client.list_installation_repos(token))
    return out


def make_list_open_prs_masked(client, provider, repos):
    """`list_open_prs` с поддержкой масок `owner/*` (и `*`) вперемешку с точными
    `owner/repo`. Маски раскрываются на каждом проходе (новые репо орг — сами);
    точные и раскрытые репо объединяются и дедуплицируются (порядок сохраняется).
    Пустой охват (маска на неустановленный owner, точных нет) → пустой список."""
    concrete, mask_owners = parse_repo_specs(repos)

    def list_open_prs():
        repos_now = list(dict.fromkeys(
            concrete + resolve_masked_repos(mask_owners, provider, client)))
        prs = []
        for repo in repos_now:
            prs.extend(parse_open_prs(client.list_open_pulls(repo), repo))
        return prs
    return list_open_prs


def make_list_open_prs_all(client, provider):
    """org-wide бэкстоп: обходит ВСЕ установки App и их репозитории (вкл. новые и
    несколько орг/аккаунтов сразу). Используется, когда RELIABILITY_REPOS пуст —
    список репо не задаётся руками, App сам определяет охват через свои установки.
    `client`/`provider` инъектируются → оркестрация тестируема без сети."""
    def list_open_prs():
        prs = []
        for inst in provider.list_installations():
            token = provider.token_for(inst["id"])
            for repo in client.list_installation_repos(token):
                prs.extend(parse_open_prs(client.list_open_pulls(repo), repo))
        return prs
    return list_open_prs
