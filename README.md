# BITinvest

(изначально — BIT-Tinkoff-Invest-Bot)

Copy-trading мост между двумя брокерскими счетами T-Bank Invest (бывш.
Тинькофф Инвестиции): состав портфеля одного или нескольких "master"-счетов
пропорционально переносится на "slave"-счёт.

Осознанно развёрнут на **двух физически разных серверах** (parser-хост с
токенами master-счетов и executor-хост с токеном slave-счёта, обмен —
через `step.csv` по HTTP) — это не архитектурная случайность, а способ не
попадать под встроенную в Т-Банк платную систему автоследования.

## Структура

```
src/bitinvest/
├── config.py             # чтение токенов/весов из secrets/, валидация
├── portfolio.py          # parse() — снимок портфеля по токенам в проценты по figi
├── rebalance.py          # расчёт целевых лотов и дельт между master и slave
├── trading.py            # buy()/sell() — рыночные ордера на один account_id
├── parser_service.py     # entrypoint для parser-хоста
└── executor_service.py   # entrypoint для executor-хоста

scripts/
├── autorun_parser.sh     # цикл на parser-хосте: parse -> step.csv -> publish
└── autorun_executor.sh   # цикл на executor-хосте: fetch step.csv -> rebalance

secrets/                  # токены/веса, в .gitignore, создаётся автоматически
data/                     # step.csv, в .gitignore
tests/                    # pytest, чистая логика ребаланса без сети
```

## Установка

Библиотека `tinkoff-investments` в карантине на PyPI и не ставится.
Используется официальный преемник `t-tech-investments` (T-Bank), с отдельного
индекса, путь импорта — `t_tech.invest` (было `tinkoff.invest`).

```bash
conda env create -f environment.yml
conda activate bitinvest
```

или через pip напрямую:

```bash
pip install -r requirements.txt
```

## Настройка токенов

Токены создаются в личном кабинете T-Invest. **Обязательно** делать их
**account-scoped** (привязанными к одному конкретному брокерскому счёту), а
не full-profile — иначе при нескольких счетах под одним токеном ордер уйдёт
не туда, куда ожидается (`trading.py` явно предупреждает в лог и использует
только первый счёт, если токен всё же вернул несколько). Токен живёт 3
месяца с момента последнего использования — потребуется периодическая
переактивация/перевыпуск.

При первом запуске `scripts/autorun_parser.sh` / `autorun_executor.sh`
интерактивно спросят токены и веса и сохранят их в `secrets/` (`chmod 600`,
не в аргументах командной строки — не светятся в `ps aux`).

## Запуск

На parser-хосте:

```bash
BITINVEST_PUBLISH_DIR=/data/www ./scripts/autorun_parser.sh
```

На executor-хосте:

```bash
BITINVEST_STEP_CSV_URL=http://<parser-host>:8082/step.csv ./scripts/autorun_executor.sh
```

## Известные ограничения

- Рассчитан на **один slave-токен**. Если их несколько, `executor_service.py`
  применит один и тот же набор ордеров к каждому из них поверх уже
  объединённого portfolio — трейды задвоятся. Не задавать больше одного
  slave-токена.
- Дедбенда по проценту отклонения нет: ребаланс триггерится при отклонении
  от целевого объёма на ≥1 лот. Более мягкий процентный порог можно добавить
  в `rebalance.check_deltas()`, если появится нужда.
- Если у master-счёта нет позиции в рублях (`RUB000UTSTOM`) в портфеле,
  остаток от ребаланса некуда положить — унаследовано из исходной версии.

## Тесты

```bash
pytest tests/
```

Живых ордеров тесты не делают — только чистая логика ребаланса на
синтетических данных. Полный прогон с реальными токенами — вручную, на
песочнице/малых суммах.
