# Кейсы доработок — docs/solutions/

Здесь лежат кейсы: что ломалось или чего не хватало, почему и как решили. Читатель — человек
или агент, который этой работы не видел и завтра будет делать похожую. Соглашение —
https://github.com/po-helper-org/.github/blob/main/SOLUTIONS.md

| Дата | Кейс | Вид | Область | PR |
|---|---|---|---|---|
| 2026-09-08 | [Дорогая модель работала умолчанием, а не настройкой](2026-09-08-reviewer-default-model-cost.md) | ops | config | #19 |
| 2026-09-02 | [Свипер ревьюил черновики](2026-09-02-sweeper-skips-drafts.md) | bug | reliability | #17 |
| 2026-08-19 | [Большой дифф рвал LLM по таймауту — map-reduce за флагом](2026-08-19-adaptive-pr-processing-mapreduce.md) | feature | reliability | #7 |
| 2026-08-19 | [Стоп-метки и severity-разметка](2026-08-19-stop-labels-and-severity.md) | feature | protocol | #14 |
| 2026-08-17 | [Задача контура застревала в фазе pr-open](2026-08-17-report-review-to-issue-cycle.md) | feature | integration | #15 |
| 2026-07-23 | [Лестница таймаутов плодила дубли ревью](2026-07-23-llm-timeout-ladder-duplicate-reviews.md) | bug | reliability | #10 |
| 2026-07-22 | [Sentry делает тихие сбои адресуемыми](2026-07-22-sentry-makes-silent-failures-visible.md) | ops | observability | #8 |
| 2026-07-18 | [Self-hosted ревью-бот падал «в тишине»](2026-07-18-pr-agent-silent-failure-core.md) | feature | reliability | #4 |
