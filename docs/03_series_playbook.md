# Как залить одну серию в прод

Пошаговый регламент: от «серии нет нигде» до «серия на сайте с фото и
историей цен». Написан по итогам первой полной проводки — пилотной
серии «2000-ліття Різдва Христового», 9 сентября 2026.

Читать вместе с `02_series_artifacts.md` (что каждый шаг оставляет на
диске) и `01_findings.md` (почему шаги устроены именно так).

---

## 1. Что настроить один раз

### Локальное окружение

```
pip install -e ".[photos,dev]"
```

Extra `photos` — это opencv, Pillow и numpy, то есть весь фотопайплайн
(`fetch-photos`, `process-photos`, а значит и `--step all`). Без него
поставятся только `httpx`, `selectolax` и `psycopg`, которых хватает
шагам сбора и записи в БД. Разделено ради сервера: ночному
`update-prices` незачем собирать стек обработки изображений, чтобы
разобрать одну HTML-таблицу (раздел 8).

### Доступ к серверу

Сервер — Hetzner, пользователь `deploy`, SSH на нестандартном порту.
Чтобы не таскать `-p` и IP в каждой команде, заведите алиас в
`~/.ssh/config`:

```
Host coinkeeper
    HostName <ip сервера>
    Port <ssh-порт>
    User deploy
```

Дальше во всех командах ниже — просто `coinkeeper`. Проверка:

```
ssh coinkeeper 'hostname; docker compose -f ~/coinkeeper/docker-compose.yml ps --format "{{.Service}} {{.State}}"'
```

Должны быть `api`, `postgres`, `minio`, `redis` в состоянии `running`.

### Канал до postgres

Порт postgres **не опубликован на хосте** — ни наружу, ни на loopback
сервера. Обычный `ssh -L 5432:localhost:5432` не сработает: слушать
там нечего. Туннель ведётся **на IP контейнера в docker-сети**:

```
PGIP=$(ssh coinkeeper 'cd ~/coinkeeper && docker inspect -f "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}" $(docker compose ps -q postgres)')
ssh -f -N -L 15432:$PGIP:5432 coinkeeper
```

IP выдаётся docker'ом и меняется, если сеть пересоздавали
(`docker compose down` и обратно), поэтому его каждый раз берут
командой, а не помнят наизусть.

### Переменные окружения

```
export DATABASE_URL="postgresql:///coinkeeper"
export PGHOST=127.0.0.1 PGPORT=15432 PGUSER=coinkeeper
export PGPASSWORD=$(ssh coinkeeper 'grep -m1 "^POSTGRES_PASSWORD=" ~/coinkeeper/.env | cut -d= -f2-')
```

`DATABASE_URL` шаги требуют непустым, но всё остальное libpq берёт из
`PG*` — так пароль не попадает ни в строку подключения, ни в логи, ни в
историю команд. Он лежит на сервере в `~/coinkeeper/.env`, локально его
хранить незачем.

Проверка канала:

```
python -c "import psycopg,os; print(psycopg.connect(os.environ['DATABASE_URL']).execute('select current_database(), count(*) from coin_series').fetchone())"
```

**Туннель, поднятый через `ssh -f`, живёт в фоне и может не пережить
смену сессии оболочки.** Если шаг падает с `Connection refused` —
туннель отвалился, поднимите заново. Надёжнее держать туннель и команду
в одном терминале.

---

## 2. Порядок шагов

Слева направо, менять местами нельзя.

| # | Шаг | Куда пишет | Сеть |
|---|---|---|---|
| 1 | `--step series` | `countries/ua/series.json` | bank.gov.ua |
| 2 | `--step all` | `staging/ua/<slug>/` | bank.gov.ua, ua-coins |
| 3 | `--step fetch-prices` | `staging/ua/_ua_coins/prices/` | ua-coins |
| 4 | `--step load-series` | БД: `coin_series` | — |
| 5 | rsync + `mc mirror` | бакет MinIO | сервер |
| 6 | `--step load-cards` | БД: `catalog_items`, `media_files`, `price_source_links` | — |
| 7 | `--step load-prices` | БД: `market_price_snapshots` | — |

Шаги 1–3 только читают сеть и пишут на диск; 4–7 пишут в прод.

`--step update-prices` в эту таблицу не входит: он не про заливку серии,
а про то, что происходит с ценами дальше, каждую ночь и без человека —
раздел 8.

**Шаг 5 обязан идти до шага 6.** `load-cards` удаляет старые общие
строки `media_files` и ставит свои, указывающие на ключи в бакете. Если
объектов там ещё нет — на живом сайте будут карточки без картинок.
Наоборот безопасно: лишние объекты в бакете никому не мешают.

---

## 3. Сбор (локально, сеть)

```
python -m collector ua --step series          # только если словарь мог устареть
python -m collector ua --series "<назва серії>" --step all
python -m collector ua --series "<назва серії>" --step fetch-prices
```

`<назва серії>` — **точно** `names.uk` из `series.json`, байт в байт.
Не «как принято писать», а как написано в справочнике: НБУ использует в
названиях разные апострофоподобные символы, и от того, какой вы
набрали, зависит имя каталога в `staging/`. Набрали не тот — соберёте
серию в один каталог, а `load-cards` пойдёт искать в другой и скажет
«no parsed cards». Уже наступали: `antychni-pamiatky-ukrainy` на диске
против `antychni-pam-iatky-ukrainy` в справочнике.

Надёжный способ узнать написание:

```
python -c "import json;print('\n'.join(e['names']['uk'] for e in json.load(open('collector/countries/ua/series.json'))['series']))"
```

### Что проверить перед заливкой

```
staging/ua/<slug>/parsed/anomalies.json   пустой список — карточки все разобраны
staging/ua/<slug>/parsed/unmatched.json   тут норма не пустота, а понимание:
                                          у этих монет не будет истории цен
staging/ua/<slug>/parsed/photos.json      у каждой карточки обе роли с winner,
                                          anomalies пуст, pair_silhouette_iou > 0.93
```

Непустой `anomalies.json` чинится дописыванием значения в `vocab.py` и
повторным `--step parse` — но не угадыванием.

---

## 4. Серии в БД

Нужен, только если `series.json` менялся (шаг 1 что-то поправил) или
серия новая. Идёт по всему справочнику сразу, не по одной серии.

```
python -m collector ua --step load-series
python -m collector ua --step load-series      # проверка: updated 0
```

Если серия новая, шаг вставит её и допишет id в `db_map.json` —
**этот файл надо закоммитить**, без него `load-cards` не найдёт
`series_id` и остановится с «run load-series first».

---

## 5. Медиа в бакет

```
SLUG=<slug>
ssh coinkeeper "mkdir -p ~/media-drop/$SLUG"
rsync -av staging/ua/$SLUG/media/out/ coinkeeper:~/media-drop/$SLUG/

ssh coinkeeper "cd ~/coinkeeper && docker compose run --rm -v ~/media-drop:/drop \
  --entrypoint /bin/sh minio-init -c \
  'mc alias set local http://minio:9000 \$S3_ACCESS_KEY \$S3_SECRET_KEY && \
   mc mirror --overwrite /drop/$SLUG/ local/\$S3_BUCKET/catalog-src/'"
```

Экранированные `\$S3_*` — не опечатка: переменные должны раскрыться
**внутри контейнера** `minio-init`, где они и заданы, а не в оболочке
сервера, где их нет.

`mc mirror` копирует каталогами, поэтому `media/out/nbu_88/obverse_600.webp`
становится ключом `catalog-src/nbu_88/obverse_600.webp` — ровно тем,
что `load-cards` пропишет в `media_files.storage_key`. Ключ не содержит
`catalog_item_id` именно поэтому: на момент зеркалирования строки может
ещё не быть, а у усыновляемой записи id непредсказуем.

Проверка, что ключи совпали с ожидаемыми:

```
python - <<'EOF'
from pathlib import Path
from collector.countries.ua import load_cards as lc
s = lc.LoadCardsSummary()
plans, _ = lc.collect(Path("staging/ua/<slug>"), s)
for p in plans:
    for u in p.uploads:
        for v in u.variants:
            print(v.key)
EOF
```

Сверить со списком в бакете (`mc ls --recursive local/$S3_BUCKET/catalog-src/`).

---

## 6. Монеты в БД

```
python -m collector ua --series "<назва серії>" --step load-cards
python -m collector ua --series "<назва серії>" --step load-cards   # проверка
```

Второй прогон обязан дать `updated 0, adopted 0, unchanged N` и
`media roles: written 0, unchanged 2N`. Любое другое — повод смотреть,
что за поле «меняется» каждый раз.

Что читать в отчёте:

- **`insert`** — новая запись, `status='active'`;
- **`update`** — запись уже была под `nbu:<N>`;
- **`adopt`** — нашли запись uCoin-эпохи через её NBU-ссылку в
  `price_source_links` и забрали себе: `source_key` сменился, **id
  сохранён**, коллекционные позиции на нём остались целы;
- **`unchanged`** — ничего не поменялось;
- **`SKIPPED, listed in edited_fields`** — поле правил человек, шаг его
  не трогает.

`source links written 0` — норма, а не сбой: если ссылки уже указывают
куда надо, они не переписываются, чтобы не двигать `matched_at`.

---

## 7. Цены

```
python -m collector ua --series "<назва серії>" --step load-prices --drop-ucoin-prices
```

`--drop-ucoin-prices` удаляет легаси-историю uCoin, но **только у тех
монет, которым этот же прогон залил историю ua-coins**. Монета без пары
на ua-coins в этот список не попадает, и её строки uCoin — единственные
цены, какие у неё есть — остаются на месте.

Зачем это вообще: витрина показывает самый свежий снапшот независимо от
источника, а легаси-строки uCoin датированы летом 2026 и на драгметалле
несут мусор (8.13 ₴ там, где ua-coins даёт 100 600 ₴). Оставленная
строка перебивает нашу историю и показывается как цена монеты.

**Перед удалением снимите бэкап** затрагиваемых строк:

```
ssh coinkeeper "cd ~/coinkeeper && docker compose exec -T postgres sh -c '
psql -U \$POSTGRES_USER -d \$POSTGRES_DB -c \"
COPY (select m.* from market_price_snapshots m
      where m.source = \\\$\\\$uCoin\\\$\\\$
        and m.catalog_item_id in (select id from catalog_items
                                  where series_id = <id> and created_by is null)
      order by m.id) TO STDOUT WITH CSV HEADER\"'" > ucoin-backup-<slug>.csv
```

Без флага шаг ничего не удаляет, но считает оставшиеся строки и пишет,
сколько их, — забыть про них нельзя.

Второй прогон: `inserted 0, already present N`.

### Последний шаг: отметить серию доведённой

```
# collector/countries/ua/db_map.json, ключ "completed"
"completed": ["2000-littia-rizdva-khrystovoho", "<новый slug>"]
```

**Этот файл надо закоммитить и раскатать на сервер** (`git pull` +
пересборка образа, раздел 8). До этого момента ночной `update-prices`
серию не трогает вовсе — и это правильный порядок: сначала человек
довёл серию до конца и посмотрел на неё глазами, потом её берёт крон.

---

## 8. Суточные цены (крон)

Всё до этого места — разовая проводка серии руками. Дальше цены живут
сами: раз в сутки шаг `update-prices` берёт с ua-coins сегодняшнюю
котировку **каждой** монеты каталога и дописывает по одному снапшоту.
Это единственный шаг, рассчитанный на запуск без человека, и работает он
на сервере, а не с ноутбука — туннель ему не нужен, postgres рядом.

Скоуп он берёт **из базы**, но только по доведённым сериям:

```sql
select id, source_key, issue_year from catalog_items
where source_key like 'nbu:%' and created_by is null and status = 'active'
  and series_id = any(<id доведённых серий>);
```

Список доведённых лежит в `collector/countries/ua/db_map.json`, ключ
`completed` (слаги, id берутся из того же файла). **Это не оптимизация,
а граница ответственности.** В каталоге лежат и NBU-монеты, которых
никто здесь не собирал: их завёл собственный конвейер coin_keeper, и
UA-Coins-ссылки на них никем из нас не подтверждены. Котировать монету
по непроверенной ссылке — это как раз способ повесить цену не на ту
монету. Поэтому серия попадает в `completed` руками, **последним шагом
её заливки** (раздел 7), и не раньше.

Пустой или отсутствующий `completed` — ошибка с кодом 2, а не пустая
ночь: крон, который месяц молча ничего не делает из-за описки в
конфиге, хуже крона, который скажет об этом первым же утром.

Серию, залитую вчера и дописанную в `completed`, шаг подхватит сам —
ничего досыпать в staging на сервере не надо, и то, что там лежит (или
не лежит), на него не влияет. Монету с ua-coins он ищет по `price_source_links`
(`source='UA-Coins'`, `external_id` — URL страницы, из слага которого
берётся id), то есть по тому матчу, который однажды подтвердил человек.
Ночью ничего не переоткрывается: монета без UA-Coins-ссылки — это
`no_link`, а не повод матчить заново.

Скачивает он не страницы монет, а годовые таблицы —
`/ua/catalog/all/<год>` по каждому году выпуска монет скоупа и по
соседним (`год±1`, то же правило year_shift, что в матчинге). В таблице
уже есть и цена, и **дата среза** в заголовке колонки («Вартість
08.09.2026»). Пишется именно эта дата, а не дата запуска: в 03:15 сайт
обычно отдаёт вчерашний срез, и проставить ему сегодняшнее число значило
бы придумать котировку, которой не было.

Запросы идут последовательно, с той же паузой 2.5–4.5 с, что и в
матчинге: полный каталог — это порядка тридцати годов, то есть пара
минут ночью. Если пять годов подряд не скачались, шаг бросает остальные
и заканчивает — крон, который в семь утра всё ещё долбится в лежащий
сайт, не помогает никому; брошенные годы попадают в список несчитанных.

Скачанный HTML остаётся в `staging/ua/_ua_coins/daily/<дата>/<год>.html`
(относительно чекаута, то есть `~/coin-parser/staging/...`) — это не кэш,
а материал для разбора инцидентов: следующий прогон его не читает
никогда, ему нужны свежие таблицы. Папки старше 14 дней шаг удаляет сам.

### Установка

Шаг едет на сервер **контейнером рядом с coin_keeper**, а не вторым
питоном на хосте. Так исчезает и venv, и `pip install` на сервере, и
добывание IP контейнера postgres: контейнер стоит в той же docker-сети,
где postgres зовут просто `postgres`.

На чистом сервере нужны только docker и git — ни питона, ни venv, ни
`pip` на хосте (и не пытайтесь: системный python помечен
externally-managed, а прод-образ `api` собран с read-only `$HOME` — обе
попытки обойти это уже сделаны и обе отказали, см. `01_findings.md`).

```
ssh coinkeeper
git clone <repo> ~/coin-parser && cd ~/coin-parser   # или git pull, если уже есть
mkdir -p ~/logs                    # cron открывает лог ДО скрипта, каталог должен быть
docker network ls                  # найти сеть коинкипера, обычно coinkeeper_default
docker compose --env-file ~/coinkeeper/.env -f deploy/docker-compose.collector.yml build
./deploy/run-update-prices.sh; echo "exit=$?"     # ручной прогон
crontab deploy/crontab                            # включить ночной
crontab -l
```

Обновление после правок кода: `git pull` и та же команда `build`.

**Где лежит пароль: только в `~/coinkeeper/.env`, и больше нигде.**
`--env-file` отдаёт compose'у `POSTGRES_USER`/`POSTGRES_PASSWORD` самого
стека, они уезжают в `PG*` контейнера, а `DATABASE_URL` остаётся
`postgresql:///coinkeeper`. Пароль не попадает ни в строку подключения,
ни в лог, ни в `ps` — то же разделение, что в разделе 1, и по той же
причине его нет во втором dotenv: копия пароля означает второе место,
где его надо менять при ротации. `deploy/.env` (по образцу
`deploy/.env.example`) заводится, только если надо переопределить
что-то **несекретное** — имя сети или uid; скрипт подхватывает его
вторым `--env-file`, поверх коинкиперовского:

```
COINKEEPER_NETWORK=<имя из docker network ls>
COLLECTOR_UID=$(id -u)
```

`COLLECTOR_UID` важен: контейнер пишет скачанный за ночь HTML в
`~/coin-parser/staging/ua/_ua_coins/daily/<дата>/`, и если uid не совпадёт
с владельцем чекаута, каталог станет нечитаемым с хоста. По умолчанию
1000, что верно для `deploy`. Смотреть — обычным `ls`, докер не нужен;
папки старше 14 дней шаг чистит сам.

Образ ставит только `httpx`, `selectolax` и `psycopg` — 177 МБ.
Фотопайплайн (opencv, Pillow, numpy) вынесен в extra `photos` и на
сервер не едет вообще; локально ставится как
`pip install -e ".[photos,dev]"`.

### Разовая уборка хоста

После неудачных попыток поставить шаг на хост там могло остаться:

```
pip list --user                      # посмотреть, ЧТО там, прежде чем сносить
rm -rf ~/.local/lib/python3.12 ~/.local/bin/pip*

docker ps -a --filter name=coinkeeper-api-run- -q | xargs -r docker rm
```

Вторая команда убирает сирот от прежних `docker compose run`. Точечно и
именно так: `docker compose down --remove-orphans` на compose-файле
коинкипера **уронит сайт**, а `run --remove-orphans` без имени сервиса
просто не запустится.

### Проверка назавтра

```
tail -n 40 ~/logs/update-prices.log
grep '^update-prices' ~/logs/update-prices.log | tail -5
```

Последняя строка каждого прогона — одна, машиночитаемая, всегда с теми же
ключами в том же порядке:

```
update-prices ok series=1 scope=6 years=3 matched=5 inserted=5 dup=0 no_quote=1 no_link=0 errors=0
```

- `series` — сколько серий в `completed`; если тут внезапно больше, чем
  вы доводили, кто-то дописал лишнего;
- `scope` = `no_link` + `no_quote` + `matched`, а `matched` = `inserted` + `dup`
  — сходится всегда, это способ проверить отчёт на месте;
- `no_quote` — монета есть, котировки нет: «немає даних» на сайте или
  строки нет в скачанных таблицах. Штатное состояние, не ошибка (nbu:88
  не котируется с 2024 года и будет там каждую ночь);
- `no_link` — монету никогда не матчили к ua-coins;
- `dup` — котировка за этот день уже лежит в базе.

И в базе:

```sql
select observed_at::date as day, count(*)
from market_price_snapshots
where source = 'UA-Coins' and observed_at >= now() - interval '7 days'
group by 1 order by 1;
```

Должна появляться строка на каждый день, где `inserted > 0`. Если
`inserted = 0` несколько ночей подряд, а `dup` растёт — сайт перестал
пересчитывать колонку; это его дело, не наше, и не сбой.

### Коды возврата

| Код | Что значит | Что делать |
|---|---|---|
| 0 | штатно, **включая `inserted=0`** — выходной у ua-coins не ошибка | ничего |
| 1 | частично: какие-то годы не скачались после ретраев, остальное залито | посмотреть, какие годы, в отчёте над итоговой строкой; обычно проходит само на следующую ночь |
| 2 | ничего не сделано: БД недоступна, или ua-coins не отдал **ни одного** года | смотреть лог: `docker compose ps`, сеть, `~/coinkeeper/.env` |

Никаких вопросов и подсказок «перезапустите с флагом» шаг не печатает —
их некому читать.

### Уживается ли это с ручным load-prices

Да, и это не совпадение. `update-prices` и `load-prices` пишут в одну
таблицу **одинаковые строки**: `source='UA-Coins'`, `grade=NULL`,
`currency='UAH'`, `observed_at` — полночь UTC того дня, к которому
относится цена, `source_url` — страница монеты. Оба идут через один и тот
же батч (`build_copy_rows` → `insert_from_tmp`), поэтому день, который уже
есть в базе, вторым не встанет: его снимает `NOT EXISTS` по
`(catalog_item_id, source, grade, observed_at)` c `IS NOT DISTINCT FROM`
на `grade` (почему не `ON CONFLICT` — раздел про NULLS в
`01_findings.md`).

Практически это значит: заливать серию по разделам 3–7 можно в любой
день и в любом порядке относительно крона. `load-prices` принесёт монете
всю историю с графика, крон продолжит её дальше по одной точке в сутки, и
на пересечении они дадут `dup`, а не второй ряд цен.

Единственное, чего `update-prices` не делает намеренно: не заводит монет,
не трогает `price_source_links`, не чистит uCoin (это флаг у
`load-prices`) и не размечает `is_suspect`.

---

## 9. Проверка результата

```sql
-- монет в серии, и у всех ли заполнено
select i.id, i.source_key, i.series_id, i.material, i.quality,
       i.descriptions is not null, i.artists is not null,
       (select count(*) from media_files m
        where m.catalog_item_id = i.id and m.owner_id is null) as media
from catalog_items i
where i.series_id = <id> and i.created_by is null order by i.id;

-- что покажет витрина как цену
select i.id, i.source_key, p.source, p.price, p.observed_at::date
from catalog_items i
join lateral (select m.source, m.price, m.observed_at
              from market_price_snapshots m
              where m.catalog_item_id = i.id and not m.is_suspect
              order by m.observed_at desc, m.id desc limit 1) p on true
where i.series_id = <id> and i.created_by is null order by i.id;
```

Дальше — глазами на сайте: серия, число монет, фото, цена и ссылка
«джерело».

---

## 10. Что эти шаги НЕ делают

- **не удаляют записи каталога** — никогда, ни при каких условиях;
- **не трогают** `catalog_km`, `catalog_uc`, `catalog_numista`, `notes`,
  `subtype`, архивные поля и всё, что перечислено в `edited_fields`;
- **не меняют** `collection_group` и `created_by` у существующих
  записей (у новых — ставят один раз при создании);
- **не чистят** `price_source_links` источника uCoin и не создают их;
- **не удаляют** объекты из бакета: заменённые фото остаются сиротами,
  чистка бакета — отдельная задача;
- **не меняют схему БД** — это зона coin_keeper.

## 11. Известные расхождения, принятые сознательно

**Вес драгметалла.** НБУ публикует только вагу у чистоті, а колонка
`catalog_items.weight_grams` означает вес монеты. Для золота и серебра
шаг пишет туда чистоту — то есть у 10 грн срібла в базе окажется 31,1 г
вместо реальных 33,62. Решение: ждём отдельную колонку под чистоту в
coin_keeper, до тех пор оставляем как есть. Затронутых украинских
записей с уже проставленным весом — 281.

**Подпись источника цены и ссылка «джерело» независимы.** Подпись берётся
из источника показанного снапшота, ссылка — из `price_source_links` с
предпочтением UA-Coins. Пока источники совпадают, разницы не видно; как
разъедутся — на карточке будет одно название и ссылка в другое место.
