# Прогон: «Безсмертна моя Україно», 2026-09-08

`python -m collector ua --series "Безсмертна моя Україно" --step all`

Сохранён как образец: одна серия вскрыла проблемы сразу в трёх местах —
в матчинге, в назначении ролей и в обработке. Первый раздел — то, что
видно прямо из лога, без разбора причин. Второй — одна причина, которую
разобрали сразу, потому что она бьёт по главному требованию: в выход
попала фотография монеты в упаковке.

## Что бросается в глаза

**Матчинг: 9/16, 6 конфликтов, 1 без пары.**

Все шесть конфликтов — это пары «обычная монета» и «та же монета у
сувенірній упаковці» с одинаковым названием:

| | 5 ₴ обычная | 5 ₴ у сувенірній упаковці | 10 ₴ срібло |
|---|---|---|---|
| В єдності - сила | nbu:1560 CONFLICT | nbu:1561 CONFLICT | nbu:1562 exact |
| Ой у лузі червона калина | nbu:1564 CONFLICT | nbu:1565 CONFLICT | nbu:1566 exact |
| Сміливість бути. UA | — | nbu:1589 CONFLICT | nbu:1588 exact |
| Захисниці | — | nbu:1591 CONFLICT | nbu:1617 exact |

То есть конфликтуют ровно те карточки, у которых номинал и название
совпадают с соседом, а различает их только хвост «у сувенірній
упаковці». Пользователь отмечает, что на ua-coins для упакованных монет
**есть отдельные страницы**, то есть кандидат существует и его есть чем
отличить — значит, дело в правиле сопоставления, а не в отсутствии
данных.

`nbu:1718` — UNMATCHED, и у него название отличается от всех остальных
сразу тремя вещами: кавычки внутри, маркер `(н)`, и «у сувенірному
**пакованні**» вместо «упаковці».

> **Починено (2026-09-09).** Парсинг и матчинг разобраны — две находки в
> `docs/01_findings.md`: «НБУ ставит кавычки и суффикс металла НЕ по
> краям заголовка, когда есть хвост про упаковку» и «Хвост „у сувенірній
> упаковці“ — это не уточнение, а вторая монета». На том же кэше серия
> стала **16/16 exact, 0 конфликтов, 0 без пары**; `nbu:1718` →
> [2659](https://www.ua-coins.info/ua/list/2659-krayina-superheroyiv-dyakuyemo-zbroyaram-u-suvenirnomu-pakovanni),
> `nbu:1591` → 2441, `nbu:1560`/`nbu:1561` → 2408/2407. Лог ниже —
> дофиксовый, он и есть предмет разбора.

**Обработка: 8 аномалий, все на упакованных монетах.**

`nbu:1561`, `nbu:1565`, `nbu:1589`, `nbu:1591` уходят как `kept_bg` с
вердиктами `boxy object in frame` / `elongated object in frame` /
`scattered object in frame`. Формально механизм отработал правильно —
это и есть фотография коробки, а не монеты, фон не белый, резать нечего.
Вопрос в другом: **у этих карточек вообще нет фотографии монеты**, и
аверс/реверс заполняются снимками упаковки. Возможно, для таких карточек
нужна отдельная роль, а не подстановка упаковки в `obverse`/`reverse`.

**Мелочи оттуда же:**

- `nbu:1591`: у одного и того же реверса миниатюра даёт `aspect=0.553`,
  а полноразмерный `aspect=0.976`. Для одного кадра это слишком большая
  разница — либо кадрирование у них разное, либо маска считается
  по-разному.
- Страницы буклета (`uacoins_03`/`uacoins_04`, alt «Буклет, сторінка
  1/2», 2105×1489) скачиваются и обрабатываются как кандидаты. Ролью они
  не становятся (правильно), но тратят трафик и время, и попадают в
  `unassigned by metadata`.
- В stdout протёк `UserWarning` от Pillow — «Palette images with
  Transparency expressed in bytes should be converted to RGBA images».
  Это наш собственный `alpha_channel()` на палитровых PNG от НБУ:
  конверсия там намеренная, предупреждение надо глушить.
- Полноразмерные PNG от НБУ здесь весят по 2-4 МБ штука, 32 файла на
  серию. Впервые видно, во что обходится чтение `a.big-image`.

## Разобрано по горячим следам: alt-текст на ua-coins врёт про роли

Проверено на `nbu:1561` / ua-coins
[2407 «В єдності - сила у сувенірній упаковці»](https://www.ua-coins.info/ua/list/2407-v-yednosti---syla-u-suvenirniy-upakovtsi).
В галерее там **четыре** изображения, и подписи стоят не на тех:

| # | alt | что на самом деле | размер | shape-вердикт |
|---|---|---|---|---|
| 1 | `Аверс ... у сувенірній упаковці` | **лицевая сторона блистера** | 880×1600 RGB | `boxy object in frame` |
| 2 | `Реверс ... у сувенірній упаковці` | **оборот блистера** с ТТХ | 846×1600 RGB | `elongated object in frame` |
| 3 | `... - додаткове фото` | **аверс монеты**, фон уже вырезан | 1600×1600 RGBA | `ok` |
| 4 | `... - додаткове фото` | **реверс монеты**, фон уже вырезан | 1600×1600 RGBA | `ok` |

То есть «Аверс»/«Реверс» подписаны сканы упаковки, а настоящие стороны
монеты лежат безымянными «додатковими фото».

`_role_from_text()` доверяет alt-тексту как источнику истины, и роль
достаётся упаковке. Хорошие снимки при этом попадают в `unassigned`, а
оттуда роль заполняется **только если пул роли пуст** — а он не пуст,
его уже занял блистер. Отсюда «взялись тупо первые две».

Существенно, что механизм эти кадры **уже различает правильно**:
классификатор говорит `boxy`/`elongated` про упаковку и `ok` про монету,
а `_rank_key` держит `is_coin` вторым членом. Не хватает только одного —
чтобы вердикт формы мог отменить подпись, а не работать лишь внутри пула,
куда подпись уже никого не пустила.

**Важно не перепутать два разных дефекта с одинаковым симптомом.** На
этом прогоне страница 2407 вообще не скачивалась: матчинг дал CONFLICT,
поэтому `ua_coins=0`, и упаковка в выходе — это фотографии самого НБУ
(у карточки «у сувенірній упаковці» на bank.gov.ua аверс/реверс это и
есть блистер). Ошибка ролей проявится, как только матчинг починят.
Чинить надо оба:

1. **Матчинг** — различать «монету» и «ту же монету у сувенірній
   упаковці». Кандидат на ua-coins существует и называется ровно так же,
   с тем же хвостом; шесть конфликтов из шестнадцати карточек — это
   ровно эти пары.
2. **Роли** — решать по тому, что на снимке, а не по подписи. Подпись
   остаётся подсказкой, но снимок, который классификатор не считает
   монетой, не должен занимать роль впереди того, который считает.

> **Починено (2026-09-09), оба пункта.** Роли — находка «Подпись
> „Аверс“/„Реверс“ — подсказка, а не доказательство» в
> `docs/01_findings.md`; правило вынесено в `_assign_roles()`. Побочно
> там же выяснилось, чем НЕ надо делить спасённую пару на стороны:
> `score()` развёл бы аверс и реверс `nbu:1561` и `nbu:1591` задом
> наперёд на разнице в тысячные — делим по порядку в галерее. После
> обоих фиксов серия идёт **16/16 exact, 16/16 полных пар, 0 аномалий**;
> `nbu:1561`/`1565`/`1589`/`1591` берут аверс и реверс с
> `uacoins_03`/`uacoins_04` (1600×1600, фон уже вырезан, pair IoU
> 0.997-1.000). Проверено глазами на выходных файлах.

## Лог

```text
[fetch] series: Безсмертна моя Україно
[fetch]   uk: 1 page(s), 16 card(s) seen, 122645 bytes
[fetch]   en: 1 page(s), 16 card(s) seen, 97517 bytes
[parse] series: Безсмертна моя Україно
[parse]   uk cards: 16, en cards: 16, matched: 16, anomalies: 0
[parse]   source_id  title                          denom      material
[parse]   nbu:1560   В єдності - сила               5 hryvnia  nickel_silver
[parse]   nbu:1561   В єдності - сила у сувенірній упаковці 5 hryvnia  nickel_silver
[parse]   nbu:1562   В єдності - сила               10 hryvnia silver
[parse]   nbu:1564   Ой у лузі червона калина       5 hryvnia  nickel_silver
[parse]   nbu:1565   Ой у лузі червона калина у сувенірній упаковці 5 hryvnia  nickel_silver
[parse]   nbu:1566   Ой у лузі червона калина       10 hryvnia silver
[parse]   nbu:1588   Сміливість бути. UA            10 hryvnia silver
[parse]   nbu:1589   Сміливість бути. UA у сувенірній упаковці 5 hryvnia  nickel_silver
[parse]   nbu:1591   Захисниці у сувенірній упаковці 5 hryvnia  nickel_silver
[parse]   nbu:1592   Країна супергероїв. Дякуємо волонтерам! 5 hryvnia  nickel_silver
[parse]   nbu:1600   Країна супергероїв. Дякуємо енергетикам! 5 hryvnia  nickel_silver
[parse]   nbu:1617   Захисниці                      10 hryvnia silver
[parse]   nbu:1619   Країна супергероїв. Дякуємо залізничникам! 5 hryvnia  nickel_silver
[parse]   nbu:1622   Набір із двох срібних монет “Дружба та братство - найбільше багатство” у футлярі 10 hryvnia silver
[parse]   nbu:1643   Країна супергероїв. Дякуємо медикам! 5 hryvnia  nickel_silver
[parse]   nbu:1718   "Країна супергероїв. Дякуємо зброярам!" (н) у сувенірному пакованні 5 hryvnia  nickel_silver
[match]   year 2021 (fetched, 1/7): 40 coin(s) found
[match]   year 2022 (fetched, 2/7): 31 coin(s) found
[match]   year 2023 (fetched, 3/7): 30 coin(s) found
[match]   year 2024 (fetched, 4/7): 30 coin(s) found
[match]   year 2025 (fetched, 5/7): 30 coin(s) found
[match]   year 2026 (fetched, 6/7): 19 coin(s) found
[match]   year 2027 (fetched, 7/7): no coins found
[match] series: Безсмертна моя Україно
[match]   source_id  title                          denom    matched_by   url
[match]   nbu:1560   В єдності - сила               5        CONFLICT     
[match]   nbu:1561   В єдності - сила у сувенірній  5        CONFLICT     
[match]   nbu:1562   В єдності - сила               10       exact        https://www.ua-coins.info/ua/list/2409-v-yednosti---syla
[match]   nbu:1564   Ой у лузі червона калина       5        CONFLICT     
[match]   nbu:1565   Ой у лузі червона калина у сув 5        CONFLICT     
[match]   nbu:1566   Ой у лузі червона калина       10       exact        https://www.ua-coins.info/ua/list/2402-oy-u-luzi-chervona-kalyna
[match]   nbu:1588   Сміливість бути. UA            10       exact        https://www.ua-coins.info/ua/list/2434-smilyvist-buty-ua
[match]   nbu:1589   Сміливість бути. UA у сувенірн 5        CONFLICT     
[match]   nbu:1591   Захисниці у сувенірній упаковц 5        CONFLICT     
[match]   nbu:1592   Країна супергероїв. Дякуємо во 5        exact        https://www.ua-coins.info/ua/list/2466-krayina-superheroyiv-dyakuyemo-volonteram
[match]   nbu:1600   Країна супергероїв. Дякуємо ен 5        exact        https://www.ua-coins.info/ua/list/2443-krayina-superheroyiv-dyakuyemo-enerhetykam
[match]   nbu:1617   Захисниці                      10       exact        https://www.ua-coins.info/ua/list/2442-zakhysnytsi
[match]   nbu:1619   Країна супергероїв. Дякуємо за 5        exact        https://www.ua-coins.info/ua/list/2467-krayina-superheroyiv-dyakuyemo-zaliznychnykam
[match]   nbu:1622   Набір із двох срібних монет “Д 10       exact        https://www.ua-coins.info/ua/list/2433-nabir-iz-dvokh-sribnykh-monet-druzhba-ta-bratstvo---naybil-she-bahatstvo-u-futlyari
[match]   nbu:1643   Країна супергероїв. Дякуємо ме 5        exact        https://www.ua-coins.info/ua/list/2488-krayina-superheroyiv-dyakuyemo-medykam
[match]   nbu:1718   "Країна супергероїв. Дякуємо з 5        UNMATCHED    
[match]   matched 9/16 (exact: 9, year_shift: 0), unmatched: 1, conflicts: 6
[fetch-photos] series: Безсмертна моя Україно -- 16 card(s)
[fetch-photos]   (1/16) nbu:1560 'В єдності - сила'
[fetch-photos]     nbu obverse: 200x200, 82936B <- https://bank.gov.ua/media/coins/1560/avers.jpg?v=19
[fetch-photos]     nbu reverse: 200x200, 28840B <- https://bank.gov.ua/media/coins/1560/revers.jpg?v=19
[fetch-photos]     nbu full-size obverse: 1600x1600, 2721843B <- https://bank.gov.ua/files/coins_images/B87a.png?v=19
[fetch-photos]     nbu full-size reverse: 1600x1600, 1470209B <- https://bank.gov.ua/files/coins_images/B87r.png?v=19
[fetch-photos]   (2/16) nbu:1561 'В єдності - сила у сувенірній упаковці'
[fetch-photos]     nbu obverse: 880x1600, 532355B <- https://bank.gov.ua/media/coins/1561/avers.jpg?v=19
[fetch-photos]     nbu reverse: 846x1600, 1004318B <- https://bank.gov.ua/media/coins/1561/revers.jpg?v=19
[fetch-photos]     nbu full-size obverse: 880x1600, 1873355B <- https://bank.gov.ua/files/coins_images/B88a.png?v=19
[fetch-photos]     nbu full-size reverse: 846x1600, 2547839B <- https://bank.gov.ua/files/coins_images/B88r.png?v=19
[fetch-photos]   (3/16) nbu:1562 'В єдності - сила'
[fetch-photos]     nbu obverse: 1600x1600, 686173B <- https://bank.gov.ua/media/coins/1562/avers.jpg?v=19
[fetch-photos]     nbu reverse: 1600x1600, 333794B <- https://bank.gov.ua/media/coins/1562/revers.jpg?v=19
[fetch-photos]     nbu full-size obverse: 1600x1600, 2409483B <- https://bank.gov.ua/files/coins_images/C00a.png?v=19
[fetch-photos]     nbu full-size reverse: 1600x1600, 1470209B <- https://bank.gov.ua/files/coins_images/C00r.png?v=19
[fetch-photos]     ua_coins page: https://www.ua-coins.info/ua/list/2409-v-yednosti---syla
[fetch-photos]     ua_coins gallery: 2 image(s) found on page
[fetch-photos]       variant 1600x1600 <- https://www.ua-coins.info/images/coins/big/2409_obverse.webp
[fetch-photos]       variant 600x600 <- https://www.ua-coins.info/images/coins/middle/2409_obverse.webp
[fetch-photos]     uacoins_01.webp: chose 1600x1600 (alt='Аверс В єдності - сила') <- https://www.ua-coins.info/images/coins/big/2409_obverse.webp
[fetch-photos]       variant 1600x1600 <- https://www.ua-coins.info/images/coins/big/2409_reverse.webp
[fetch-photos]       variant 600x600 <- https://www.ua-coins.info/images/coins/middle/2409_reverse.webp
[fetch-photos]     uacoins_02.webp: chose 1600x1600 (alt='Реверс В єдності - сила') <- https://www.ua-coins.info/images/coins/big/2409_reverse.webp
[fetch-photos]   (4/16) nbu:1564 'Ой у лузі червона калина'
[fetch-photos]     nbu obverse: 200x200, 23867B <- https://bank.gov.ua/media/coins/1564/avers.jpg?v=19
[fetch-photos]     nbu reverse: 200x200, 58888B <- https://bank.gov.ua/media/coins/1564/revers.jpg?v=19
[fetch-photos]     nbu full-size obverse: 1600x1600, 2965332B <- https://bank.gov.ua/files/coins_images/C01a.png?v=19
[fetch-photos]     nbu full-size reverse: 1600x1600, 2539268B <- https://bank.gov.ua/files/coins_images/C01r.png?v=19
[fetch-photos]   (5/16) nbu:1565 'Ой у лузі червона калина у сувенірній упаковці'
[fetch-photos]     nbu obverse: 875x1600, 389909B <- https://bank.gov.ua/media/coins/1565/avers.jpg?v=19
[fetch-photos]     nbu reverse: 875x1600, 268554B <- https://bank.gov.ua/media/coins/1565/revers.jpg?v=19
[fetch-photos]     nbu full-size obverse: 875x1600, 1595965B <- https://bank.gov.ua/files/coins_images/C02a.png?v=19
[fetch-photos]     nbu full-size reverse: 875x1600, 1045885B <- https://bank.gov.ua/files/coins_images/C02r.png?v=19
[fetch-photos]   (6/16) nbu:1566 'Ой у лузі червона калина'
[fetch-photos]     nbu obverse: 1600x1600, 1025149B <- https://bank.gov.ua/media/coins/1566/avers.jpg?v=19
[fetch-photos]     nbu reverse: 1600x1600, 1049985B <- https://bank.gov.ua/media/coins/1566/revers.jpg?v=19
[fetch-photos]     nbu full-size obverse: 1600x1600, 3055578B <- https://bank.gov.ua/files/coins_images/C03a.png?v=19
[fetch-photos]     nbu full-size reverse: 1600x1600, 3255827B <- https://bank.gov.ua/files/coins_images/C03r.png?v=19
[fetch-photos]     ua_coins page: https://www.ua-coins.info/ua/list/2402-oy-u-luzi-chervona-kalyna
[fetch-photos]     ua_coins gallery: 2 image(s) found on page
[fetch-photos]       variant 1600x1600 <- https://www.ua-coins.info/images/coins/big/2402_obverse.webp
[fetch-photos]       variant 600x600 <- https://www.ua-coins.info/images/coins/middle/2402_obverse.webp
[fetch-photos]     uacoins_01.webp: chose 1600x1600 (alt='Аверс Ой у лузі червона калина') <- https://www.ua-coins.info/images/coins/big/2402_obverse.webp
[fetch-photos]       variant 1600x1600 <- https://www.ua-coins.info/images/coins/big/2402_reverse.webp
[fetch-photos]       variant 600x600 <- https://www.ua-coins.info/images/coins/middle/2402_reverse.webp
[fetch-photos]     uacoins_02.webp: chose 1600x1600 (alt='Реверс Ой у лузі червона калина') <- https://www.ua-coins.info/images/coins/big/2402_reverse.webp
[fetch-photos]   (7/16) nbu:1588 'Сміливість бути. UA'
[fetch-photos]     nbu obverse: 198x198, 38107B <- https://bank.gov.ua/media/coins/1588/avers.jpg?v=19
[fetch-photos]     nbu reverse: 198x198, 58176B <- https://bank.gov.ua/media/coins/1588/revers.jpg?v=19
[fetch-photos]     nbu full-size obverse: 1600x1600, 2597387B <- https://bank.gov.ua/files/coins_images/C11a.png?v=19
[fetch-photos]     nbu full-size reverse: 1600x1600, 3304617B <- https://bank.gov.ua/files/coins_images/C11r.png?v=19
[fetch-photos]     ua_coins page: https://www.ua-coins.info/ua/list/2434-smilyvist-buty-ua
[fetch-photos]     ua_coins gallery: 4 image(s) found on page
[fetch-photos]       variant 1600x1600 <- https://www.ua-coins.info/images/coins/big/2434_obverse.webp
[fetch-photos]       variant 600x600 <- https://www.ua-coins.info/images/coins/middle/2434_obverse.webp
[fetch-photos]     uacoins_01.webp: chose 1600x1600 (alt='Аверс Сміливість бути. UA') <- https://www.ua-coins.info/images/coins/big/2434_obverse.webp
[fetch-photos]       variant 1600x1600 <- https://www.ua-coins.info/images/coins/big/2434_reverse.webp
[fetch-photos]       variant 600x600 <- https://www.ua-coins.info/images/coins/middle/2434_reverse.webp
[fetch-photos]     uacoins_02.webp: chose 1600x1600 (alt='Реверс Сміливість бути. UA') <- https://www.ua-coins.info/images/coins/big/2434_reverse.webp
[fetch-photos]       variant 2105x1489 <- https://www.ua-coins.info/images/coin_gallery/2434/big/nbu_pdf_d2dc782e3cc098d0e258_p01.webp
[fetch-photos]       variant 520x360 <- https://www.ua-coins.info/images/coin_gallery/2434/thumb/nbu_pdf_d2dc782e3cc098d0e258_p01.webp
[fetch-photos]     uacoins_03.webp: chose 2105x1489 (alt='Буклет, сторінка 1') <- https://www.ua-coins.info/images/coin_gallery/2434/big/nbu_pdf_d2dc782e3cc098d0e258_p01.webp
[fetch-photos]       variant 2105x1489 <- https://www.ua-coins.info/images/coin_gallery/2434/big/nbu_pdf_d2dc782e3cc098d0e258_p02.webp
[fetch-photos]       variant 520x360 <- https://www.ua-coins.info/images/coin_gallery/2434/thumb/nbu_pdf_d2dc782e3cc098d0e258_p02.webp
[fetch-photos]     uacoins_04.webp: chose 2105x1489 (alt='Буклет, сторінка 2') <- https://www.ua-coins.info/images/coin_gallery/2434/big/nbu_pdf_d2dc782e3cc098d0e258_p02.webp
[fetch-photos]   (8/16) nbu:1589 'Сміливість бути. UA у сувенірній упаковці'
[fetch-photos]     nbu obverse: 108x198, 39358B <- https://bank.gov.ua/media/coins/1589/avers.jpg?v=19
[fetch-photos]     nbu reverse: 108x198, 44066B <- https://bank.gov.ua/media/coins/1589/revers.jpg?v=19
[fetch-photos]     nbu full-size obverse: 873x1600, 2206189B <- https://bank.gov.ua/files/coins_images/C13a.png?v=19
[fetch-photos]     nbu full-size reverse: 873x1600, 2107689B <- https://bank.gov.ua/files/coins_images/C13r.png?v=19
[fetch-photos]   (9/16) nbu:1591 'Захисниці у сувенірній упаковці'
[fetch-photos]     nbu obverse: 108x198, 60587B <- https://bank.gov.ua/media/coins/1591/avers.jpg?v=19
[fetch-photos]     nbu reverse: 108x198, 45830B <- https://bank.gov.ua/media/coins/1591/revers.jpg?v=19
[fetch-photos]     nbu full-size obverse: 869x1600, 2534360B <- https://bank.gov.ua/files/coins_images/C15a.png?v=19
[fetch-photos]     nbu full-size reverse: 869x1600, 761585B <- https://bank.gov.ua/files/coins_images/C15r.png?v=19
[fetch-photos]   (10/16) nbu:1592 'Країна супергероїв. Дякуємо волонтерам!'
[fetch-photos]     nbu obverse: 198x198, 62132B <- https://bank.gov.ua/media/coins/1592/avers.jpg?v=19
[fetch-photos]     nbu reverse: 198x198, 53171B <- https://bank.gov.ua/media/coins/1592/revers.jpg?v=19
[fetch-photos]     nbu full-size obverse: 1600x1600, 3544404B <- https://bank.gov.ua/files/coins_images/C16a.png?v=19
[fetch-photos]     nbu full-size reverse: 1600x1600, 3164185B <- https://bank.gov.ua/files/coins_images/C16r.png?v=19
[fetch-photos]     ua_coins page: https://www.ua-coins.info/ua/list/2466-krayina-superheroyiv-dyakuyemo-volonteram
[fetch-photos]     ua_coins gallery: 4 image(s) found on page
[fetch-photos]       variant 1600x1600 <- https://www.ua-coins.info/images/coins/big/2466_obverse.webp
[fetch-photos]       variant 600x600 <- https://www.ua-coins.info/images/coins/middle/2466_obverse.webp
[fetch-photos]     uacoins_01.webp: chose 1600x1600 (alt='Аверс Країна супергероїв. Дякуємо волонтерам!') <- https://www.ua-coins.info/images/coins/big/2466_obverse.webp
[fetch-photos]       variant 1600x1600 <- https://www.ua-coins.info/images/coins/big/2466_reverse.webp
[fetch-photos]       variant 600x600 <- https://www.ua-coins.info/images/coins/middle/2466_reverse.webp
[fetch-photos]     uacoins_02.webp: chose 1600x1600 (alt='Реверс Країна супергероїв. Дякуємо волонтерам!') <- https://www.ua-coins.info/images/coins/big/2466_reverse.webp
[fetch-photos]       variant 2105x1489 <- https://www.ua-coins.info/images/coin_gallery/2466/big/nbu_pdf_cd822e701ed66f85f19c_p01.webp
[fetch-photos]       variant 520x360 <- https://www.ua-coins.info/images/coin_gallery/2466/thumb/nbu_pdf_cd822e701ed66f85f19c_p01.webp
[fetch-photos]     uacoins_03.webp: chose 2105x1489 (alt='Буклет, сторінка 1') <- https://www.ua-coins.info/images/coin_gallery/2466/big/nbu_pdf_cd822e701ed66f85f19c_p01.webp
[fetch-photos]       variant 2105x1489 <- https://www.ua-coins.info/images/coin_gallery/2466/big/nbu_pdf_cd822e701ed66f85f19c_p02.webp
[fetch-photos]       variant 520x360 <- https://www.ua-coins.info/images/coin_gallery/2466/thumb/nbu_pdf_cd822e701ed66f85f19c_p02.webp
[fetch-photos]     uacoins_04.webp: chose 2105x1489 (alt='Буклет, сторінка 2') <- https://www.ua-coins.info/images/coin_gallery/2466/big/nbu_pdf_cd822e701ed66f85f19c_p02.webp
[fetch-photos]   (11/16) nbu:1600 'Країна супергероїв. Дякуємо енергетикам!'
[fetch-photos]     nbu obverse: 198x198, 55162B <- https://bank.gov.ua/media/coins/1600/avers.jpg?v=19
[fetch-photos]     nbu reverse: 198x198, 40623B <- https://bank.gov.ua/media/coins/1600/revers.jpg?v=19
[fetch-photos]     nbu full-size obverse: 1600x1600, 2962096B <- https://bank.gov.ua/files/coins_images/C22a.png?v=19
[fetch-photos]     nbu full-size reverse: 1600x1600, 2636010B <- https://bank.gov.ua/files/coins_images/C22r.png?v=19
[fetch-photos]     ua_coins page: https://www.ua-coins.info/ua/list/2443-krayina-superheroyiv-dyakuyemo-enerhetykam
[fetch-photos]     ua_coins gallery: 4 image(s) found on page
[fetch-photos]       variant 1600x1600 <- https://www.ua-coins.info/images/coins/big/2443_obverse.webp
[fetch-photos]       variant 600x600 <- https://www.ua-coins.info/images/coins/middle/2443_obverse.webp
[fetch-photos]     uacoins_01.webp: chose 1600x1600 (alt='Аверс Країна супергероїв. Дякуємо енергетикам!') <- https://www.ua-coins.info/images/coins/big/2443_obverse.webp
[fetch-photos]       variant 1600x1600 <- https://www.ua-coins.info/images/coins/big/2443_reverse.webp
[fetch-photos]       variant 600x600 <- https://www.ua-coins.info/images/coins/middle/2443_reverse.webp
[fetch-photos]     uacoins_02.webp: chose 1600x1600 (alt='Реверс Країна супергероїв. Дякуємо енергетикам!') <- https://www.ua-coins.info/images/coins/big/2443_reverse.webp
[fetch-photos]       variant 2105x1489 <- https://www.ua-coins.info/images/coin_gallery/2443/big/nbu_pdf_c74a48a59f1871653bfc_p01.webp
[fetch-photos]       variant 520x360 <- https://www.ua-coins.info/images/coin_gallery/2443/thumb/nbu_pdf_c74a48a59f1871653bfc_p01.webp
[fetch-photos]     uacoins_03.webp: chose 2105x1489 (alt='Буклет, сторінка 1') <- https://www.ua-coins.info/images/coin_gallery/2443/big/nbu_pdf_c74a48a59f1871653bfc_p01.webp
[fetch-photos]       variant 2105x1489 <- https://www.ua-coins.info/images/coin_gallery/2443/big/nbu_pdf_c74a48a59f1871653bfc_p02.webp
[fetch-photos]       variant 520x360 <- https://www.ua-coins.info/images/coin_gallery/2443/thumb/nbu_pdf_c74a48a59f1871653bfc_p02.webp
[fetch-photos]     uacoins_04.webp: chose 2105x1489 (alt='Буклет, сторінка 2') <- https://www.ua-coins.info/images/coin_gallery/2443/big/nbu_pdf_c74a48a59f1871653bfc_p02.webp
[fetch-photos]   (12/16) nbu:1617 'Захисниці'
[fetch-photos]     nbu obverse: 198x198, 59100B <- https://bank.gov.ua/media/coins/1617/avers.jpg?v=19
[fetch-photos]     nbu reverse: 198x198, 74078B <- https://bank.gov.ua/media/coins/1617/revers.jpg?v=19
[fetch-photos]     nbu full-size obverse: 1600x1600, 3178138B <- https://bank.gov.ua/files/coins_images/C31a.png?v=19
[fetch-photos]     nbu full-size reverse: 1600x1600, 4005417B <- https://bank.gov.ua/files/coins_images/C31r.png?v=19
[fetch-photos]     ua_coins page: https://www.ua-coins.info/ua/list/2442-zakhysnytsi
[fetch-photos]     ua_coins gallery: 4 image(s) found on page
[fetch-photos]       variant 1600x1600 <- https://www.ua-coins.info/images/coins/big/2442_obverse.webp
[fetch-photos]       variant 600x600 <- https://www.ua-coins.info/images/coins/middle/2442_obverse.webp
[fetch-photos]     uacoins_01.webp: chose 1600x1600 (alt='Аверс Захисниці') <- https://www.ua-coins.info/images/coins/big/2442_obverse.webp
[fetch-photos]       variant 1600x1600 <- https://www.ua-coins.info/images/coins/big/2442_reverse.webp
[fetch-photos]       variant 600x600 <- https://www.ua-coins.info/images/coins/middle/2442_reverse.webp
[fetch-photos]     uacoins_02.webp: chose 1600x1600 (alt='Реверс Захисниці') <- https://www.ua-coins.info/images/coins/big/2442_reverse.webp
[fetch-photos]       variant 2105x1489 <- https://www.ua-coins.info/images/coin_gallery/2442/big/nbu_pdf_c1555a929aba62c52599_p01.webp
[fetch-photos]       variant 520x360 <- https://www.ua-coins.info/images/coin_gallery/2442/thumb/nbu_pdf_c1555a929aba62c52599_p01.webp
[fetch-photos]     uacoins_03.webp: chose 2105x1489 (alt='Буклет, сторінка 1') <- https://www.ua-coins.info/images/coin_gallery/2442/big/nbu_pdf_c1555a929aba62c52599_p01.webp
[fetch-photos]       variant 2105x1489 <- https://www.ua-coins.info/images/coin_gallery/2442/big/nbu_pdf_c1555a929aba62c52599_p02.webp
[fetch-photos]       variant 520x360 <- https://www.ua-coins.info/images/coin_gallery/2442/thumb/nbu_pdf_c1555a929aba62c52599_p02.webp
[fetch-photos]     uacoins_04.webp: chose 2105x1489 (alt='Буклет, сторінка 2') <- https://www.ua-coins.info/images/coin_gallery/2442/big/nbu_pdf_c1555a929aba62c52599_p02.webp
[fetch-photos]   (13/16) nbu:1619 'Країна супергероїв. Дякуємо залізничникам!'
[fetch-photos]     nbu obverse: 198x198, 63634B <- https://bank.gov.ua/media/coins/1619/avers.jpg?v=19
[fetch-photos]     nbu reverse: 198x198, 54929B <- https://bank.gov.ua/media/coins/1619/revers.jpg?v=19
[fetch-photos]     nbu full-size obverse: 1600x1600, 3126328B <- https://bank.gov.ua/files/coins_images/C33a.png?v=19
[fetch-photos]     nbu full-size reverse: 1600x1600, 3105782B <- https://bank.gov.ua/files/coins_images/C33r.png?v=19
[fetch-photos]     ua_coins page: https://www.ua-coins.info/ua/list/2467-krayina-superheroyiv-dyakuyemo-zaliznychnykam
[fetch-photos]     ua_coins gallery: 4 image(s) found on page
[fetch-photos]       variant 1600x1600 <- https://www.ua-coins.info/images/coins/big/2467_obverse.webp
[fetch-photos]       variant 600x600 <- https://www.ua-coins.info/images/coins/middle/2467_obverse.webp
[fetch-photos]     uacoins_01.webp: chose 1600x1600 (alt='Аверс Країна супергероїв. Дякуємо залізничникам!') <- https://www.ua-coins.info/images/coins/big/2467_obverse.webp
[fetch-photos]       variant 1600x1600 <- https://www.ua-coins.info/images/coins/big/2467_reverse.webp
[fetch-photos]       variant 600x600 <- https://www.ua-coins.info/images/coins/middle/2467_reverse.webp
[fetch-photos]     uacoins_02.webp: chose 1600x1600 (alt='Реверс Країна супергероїв. Дякуємо залізничникам!') <- https://www.ua-coins.info/images/coins/big/2467_reverse.webp
[fetch-photos]       variant 2105x1489 <- https://www.ua-coins.info/images/coin_gallery/2467/big/nbu_pdf_7341551c8acd991bddb6_p01.webp
[fetch-photos]       variant 520x360 <- https://www.ua-coins.info/images/coin_gallery/2467/thumb/nbu_pdf_7341551c8acd991bddb6_p01.webp
[fetch-photos]     uacoins_03.webp: chose 2105x1489 (alt='Буклет, сторінка 1') <- https://www.ua-coins.info/images/coin_gallery/2467/big/nbu_pdf_7341551c8acd991bddb6_p01.webp
[fetch-photos]       variant 2105x1489 <- https://www.ua-coins.info/images/coin_gallery/2467/big/nbu_pdf_7341551c8acd991bddb6_p02.webp
[fetch-photos]       variant 520x360 <- https://www.ua-coins.info/images/coin_gallery/2467/thumb/nbu_pdf_7341551c8acd991bddb6_p02.webp
[fetch-photos]     uacoins_04.webp: chose 2105x1489 (alt='Буклет, сторінка 2') <- https://www.ua-coins.info/images/coin_gallery/2467/big/nbu_pdf_7341551c8acd991bddb6_p02.webp
[fetch-photos]   (14/16) nbu:1622 'Набір із двох срібних монет “Дружба та братство - найбільше багатство” у футлярі'
[fetch-photos]     nbu obverse: 198x188, 65929B <- https://bank.gov.ua/media/coins/1622/avers.jpg?v=19
[fetch-photos]     nbu reverse: 198x188, 58537B <- https://bank.gov.ua/media/coins/1622/revers.jpg?v=19
[fetch-photos]     nbu full-size obverse: 1600x1515, 3399489B <- https://bank.gov.ua/files/coins_images/C35a.png?v=19
[fetch-photos]     nbu full-size reverse: 1600x1519, 3398723B <- https://bank.gov.ua/files/coins_images/C35r.png?v=19
[fetch-photos]     ua_coins page: https://www.ua-coins.info/ua/list/2433-nabir-iz-dvokh-sribnykh-monet-druzhba-ta-bratstvo---naybil-she-bahatstvo-u-futlyari
[fetch-photos]     ua_coins gallery: 4 image(s) found on page
[fetch-photos]       variant 1600x1515 <- https://www.ua-coins.info/images/coins/big/2433_obverse.webp
[fetch-photos]       variant 600x568 <- https://www.ua-coins.info/images/coins/middle/2433_obverse.webp
[fetch-photos]     uacoins_01.webp: chose 1600x1515 (alt='Аверс Набір із двох срібних монет “Дружба та братство - найбільше багатство” у футлярі') <- https://www.ua-coins.info/images/coins/big/2433_obverse.webp
[fetch-photos]       variant 1600x1519 <- https://www.ua-coins.info/images/coins/big/2433_reverse.webp
[fetch-photos]       variant 600x570 <- https://www.ua-coins.info/images/coins/middle/2433_reverse.webp
[fetch-photos]     uacoins_02.webp: chose 1600x1519 (alt='Реверс Набір із двох срібних монет “Дружба та братство - найбільше багатство” у футлярі') <- https://www.ua-coins.info/images/coins/big/2433_reverse.webp
[fetch-photos]       variant 2105x1489 <- https://www.ua-coins.info/images/coin_gallery/2433/big/nbu_pdf_fbdc44f631da35b598b9_p01.webp
[fetch-photos]       variant 520x360 <- https://www.ua-coins.info/images/coin_gallery/2433/thumb/nbu_pdf_fbdc44f631da35b598b9_p01.webp
[fetch-photos]     uacoins_03.webp: chose 2105x1489 (alt='Буклет, сторінка 1') <- https://www.ua-coins.info/images/coin_gallery/2433/big/nbu_pdf_fbdc44f631da35b598b9_p01.webp
[fetch-photos]       variant 2105x1489 <- https://www.ua-coins.info/images/coin_gallery/2433/big/nbu_pdf_fbdc44f631da35b598b9_p02.webp
[fetch-photos]       variant 520x360 <- https://www.ua-coins.info/images/coin_gallery/2433/thumb/nbu_pdf_fbdc44f631da35b598b9_p02.webp
[fetch-photos]     uacoins_04.webp: chose 2105x1489 (alt='Буклет, сторінка 2') <- https://www.ua-coins.info/images/coin_gallery/2433/big/nbu_pdf_fbdc44f631da35b598b9_p02.webp
[fetch-photos]   (15/16) nbu:1643 'Країна супергероїв. Дякуємо медикам!'
[fetch-photos]     nbu obverse: 198x198, 57852B <- https://bank.gov.ua/media/coins/1643/avers.jpg?v=19
[fetch-photos]     nbu reverse: 198x198, 50196B <- https://bank.gov.ua/media/coins/1643/revers.jpg?v=19
[fetch-photos]     nbu full-size obverse: 1600x1600, 3740100B <- https://bank.gov.ua/files/coins_images/C49a.png?v=19
[fetch-photos]     nbu full-size reverse: 1600x1600, 4133770B <- https://bank.gov.ua/files/coins_images/C49r.png?v=19
[fetch-photos]     ua_coins page: https://www.ua-coins.info/ua/list/2488-krayina-superheroyiv-dyakuyemo-medykam
[fetch-photos]     ua_coins gallery: 4 image(s) found on page
[fetch-photos]       variant 1600x1600 <- https://www.ua-coins.info/images/coins/big/2488_obverse.webp
[fetch-photos]       variant 600x600 <- https://www.ua-coins.info/images/coins/middle/2488_obverse.webp
[fetch-photos]     uacoins_01.webp: chose 1600x1600 (alt='Аверс Країна супергероїв. Дякуємо медикам!') <- https://www.ua-coins.info/images/coins/big/2488_obverse.webp
[fetch-photos]       variant 1600x1600 <- https://www.ua-coins.info/images/coins/big/2488_reverse.webp
[fetch-photos]       variant 600x600 <- https://www.ua-coins.info/images/coins/middle/2488_reverse.webp
[fetch-photos]     uacoins_02.webp: chose 1600x1600 (alt='Реверс Країна супергероїв. Дякуємо медикам!') <- https://www.ua-coins.info/images/coins/big/2488_reverse.webp
[fetch-photos]       variant 2105x1489 <- https://www.ua-coins.info/images/coin_gallery/2488/big/nbu_pdf_ed1690575597e9d91c4a_p01.webp
[fetch-photos]       variant 520x360 <- https://www.ua-coins.info/images/coin_gallery/2488/thumb/nbu_pdf_ed1690575597e9d91c4a_p01.webp
[fetch-photos]     uacoins_03.webp: chose 2105x1489 (alt='Буклет, сторінка 1') <- https://www.ua-coins.info/images/coin_gallery/2488/big/nbu_pdf_ed1690575597e9d91c4a_p01.webp
[fetch-photos]       variant 2105x1489 <- https://www.ua-coins.info/images/coin_gallery/2488/big/nbu_pdf_ed1690575597e9d91c4a_p02.webp
[fetch-photos]       variant 520x360 <- https://www.ua-coins.info/images/coin_gallery/2488/thumb/nbu_pdf_ed1690575597e9d91c4a_p02.webp
[fetch-photos]     uacoins_04.webp: chose 2105x1489 (alt='Буклет, сторінка 2') <- https://www.ua-coins.info/images/coin_gallery/2488/big/nbu_pdf_ed1690575597e9d91c4a_p02.webp
[fetch-photos]   (16/16) nbu:1718 '"Країна супергероїв. Дякуємо зброярам!" (н) у сувенірному пакованні'
[fetch-photos]     nbu obverse: 198x198, 30829B <- https://bank.gov.ua/media/coins/1718/avers.jpg?v=19
[fetch-photos]     nbu reverse: 198x198, 62782B <- https://bank.gov.ua/media/coins/1718/revers.jpg?v=19
[fetch-photos]     nbu full-size obverse: 1600x1600, 3293048B <- https://bank.gov.ua/files/coins_images/D00a.png?v=19
[fetch-photos]     nbu full-size reverse: 1600x1600, 4661001B <- https://bank.gov.ua/files/coins_images/D00r.png?v=19
[fetch-photos] series: Безсмертна моя Україно
[fetch-photos]   nbu:1560: nbu=4, ua_coins=0
[fetch-photos]   nbu:1561: nbu=4, ua_coins=0
[fetch-photos]   nbu:1562: nbu=4, ua_coins=2
[fetch-photos]   nbu:1564: nbu=4, ua_coins=0
[fetch-photos]   nbu:1565: nbu=4, ua_coins=0
[fetch-photos]   nbu:1566: nbu=4, ua_coins=2
[fetch-photos]   nbu:1588: nbu=4, ua_coins=4
[fetch-photos]   nbu:1589: nbu=4, ua_coins=0
[fetch-photos]   nbu:1591: nbu=4, ua_coins=0
[fetch-photos]   nbu:1592: nbu=4, ua_coins=4
[fetch-photos]   nbu:1600: nbu=4, ua_coins=4
[fetch-photos]   nbu:1617: nbu=4, ua_coins=4
[fetch-photos]   nbu:1619: nbu=4, ua_coins=4
[fetch-photos]   nbu:1622: nbu=4, ua_coins=4
[fetch-photos]   nbu:1643: nbu=4, ua_coins=4
[fetch-photos]   nbu:1718: nbu=4, ua_coins=0
[fetch-photos] 16 card(s), 96 candidate(s) on disk, 0 error(s)
[process-photos] series: Безсмертна моя Україно -- 16 card(s)
[process-photos]   (1/16) nbu:1560 'В єдності - сила'
[process-photos]     nbu_obverse.jpg: 200x200 shape=ok (solidity=0.916 extent=0.730 aspect=1.000) -> bg class 2 (cuttable, cut, outline-repaired)
[process-photos]     nbu_reverse.jpg: 200x200 shape=ok (solidity=0.990 extent=0.786 aspect=1.000) -> bg class 2 (cuttable, cut)
[process-photos]     nbu_big_obverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.781 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     nbu_big_reverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=0.999) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     obverse ranking: nbu_big_obverse.png(class 1, 1600px), nbu_obverse.jpg(class 2, 200px)
[process-photos]       obverse winner: nbu_big_obverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> obverse_300.webp@300, obverse_600.webp@600, obverse_1200.webp@1200
[process-photos]     reverse ranking: nbu_big_reverse.png(class 1, 1600px), nbu_reverse.jpg(class 2, 200px)
[process-photos]       reverse winner: nbu_big_reverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> reverse_300.webp@300, reverse_600.webp@600, reverse_1200.webp@1200
[process-photos]     pair silhouette IoU: 0.9970
[process-photos]   (2/16) nbu:1561 'В єдності - сила у сувенірній упаковці'
[process-photos]     nbu_obverse.jpg: 880x1600 shape=boxy object in frame (solidity=0.944 extent=0.934 aspect=0.550) -> bg class 3 (kept_bg, skip:not_white_bg)
[process-photos]     nbu_reverse.jpg: 846x1600 shape=elongated object in frame (solidity=0.603 extent=0.567 aspect=0.366) -> bg class 3 (kept_bg, skip:not_white_bg)
[process-photos]     nbu_big_obverse.png: 880x1600 shape=boxy object in frame (solidity=0.957 extent=0.948 aspect=0.550) -> bg class 3 (kept_bg, skip:not_white_bg)
[process-photos]     nbu_big_reverse.png: 846x1600 shape=elongated object in frame (solidity=0.589 extent=0.566 aspect=0.364) -> bg class 3 (kept_bg, skip:not_white_bg)
[process-photos]     obverse ranking: nbu_big_obverse.png(class 3, shape boxy object in frame, 880px), nbu_obverse.jpg(class 3, shape boxy object in frame, 880px, paletted)
[process-photos]       obverse winner: nbu_big_obverse.png (kept_bg, final 880x1600) -> obverse_300.webp@300, obverse_600.webp@600, obverse_1200.webp@1200
[process-photos]     reverse ranking: nbu_big_reverse.png(class 3, shape elongated object in frame, 846px), nbu_reverse.jpg(class 3, shape elongated object in frame, 846px, paletted)
[process-photos]       reverse winner: nbu_big_reverse.png (kept_bg, final 846x1600) -> reverse_300.webp@300, reverse_600.webp@600, reverse_1200.webp@1200
[process-photos]     pair silhouette IoU: 1.0000
[process-photos]   (3/16) nbu:1562 'В єдності - сила'
[process-photos]     nbu_obverse.jpg: 1600x1600 shape=ok (solidity=0.998 extent=0.785 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
/home/renat/Desktop/Work/coin-parser/.venv/lib/python3.12/site-packages/PIL/Image.py:1136: UserWarning: Palette images with Transparency expressed in bytes should be converted to RGBA images
  warnings.warn(
[process-photos]     nbu_reverse.jpg: 1600x1600 shape=ok (solidity=0.998 extent=0.783 aspect=0.999) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     nbu_big_obverse.png: 1600x1600 shape=ok (solidity=0.964 extent=0.785 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     nbu_big_reverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=0.999) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_01.webp: 1600x1600 shape=ok (solidity=0.964 extent=0.785 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_02.webp: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=0.999) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     obverse ranking: nbu_big_obverse.png(class 1, 1600px), uacoins_01.webp(class 1, 1600px), nbu_obverse.jpg(class 1, 1600px, paletted)
[process-photos]       obverse winner: nbu_big_obverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> obverse_300.webp@300, obverse_600.webp@600, obverse_1200.webp@1200
[process-photos]     reverse ranking: nbu_big_reverse.png(class 1, 1600px), uacoins_02.webp(class 1, 1600px), nbu_reverse.jpg(class 1, 1600px, paletted)
[process-photos]       reverse winner: nbu_big_reverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> reverse_300.webp@300, reverse_600.webp@600, reverse_1200.webp@1200
[process-photos]     pair silhouette IoU: 0.9974
[process-photos]   (4/16) nbu:1564 'Ой у лузі червона калина'
[process-photos]     nbu_obverse.jpg: 200x200 shape=ok (solidity=0.990 extent=0.791 aspect=1.000) -> bg class 2 (cuttable, cut)
[process-photos]     nbu_reverse.jpg: 200x200 shape=ok (solidity=0.990 extent=0.793 aspect=1.000) -> bg class 2 (cuttable, cut, outline-repaired)
[process-photos]     nbu_big_obverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     nbu_big_reverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     obverse ranking: nbu_big_obverse.png(class 1, 1600px), nbu_obverse.jpg(class 2, 200px)
[process-photos]       obverse winner: nbu_big_obverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> obverse_300.webp@300, obverse_600.webp@600, obverse_1200.webp@1200
[process-photos]     reverse ranking: nbu_big_reverse.png(class 1, 1600px), nbu_reverse.jpg(class 2, 200px)
[process-photos]       reverse winner: nbu_big_reverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> reverse_300.webp@300, reverse_600.webp@600, reverse_1200.webp@1200
[process-photos]     pair silhouette IoU: 0.9987
[process-photos]   (5/16) nbu:1565 'Ой у лузі червона калина у сувенірній упаковці'
[process-photos]     nbu_obverse.jpg: 875x1600 shape=boxy object in frame (solidity=1.000 extent=0.996 aspect=0.547) -> bg class 3 (kept_bg, skip:not_white_bg)
[process-photos]     nbu_reverse.jpg: 875x1600 shape=boxy object in frame (solidity=1.000 extent=0.996 aspect=0.547) -> bg class 3 (kept_bg, skip:not_white_bg)
[process-photos]     nbu_big_obverse.png: 875x1600 shape=boxy object in frame (solidity=1.000 extent=0.996 aspect=0.547) -> bg class 3 (kept_bg, skip:not_white_bg)
[process-photos]     nbu_big_reverse.png: 875x1600 shape=boxy object in frame (solidity=1.000 extent=0.996 aspect=0.547) -> bg class 3 (kept_bg, skip:not_white_bg)
[process-photos]     obverse ranking: nbu_big_obverse.png(class 3, shape boxy object in frame, 875px), nbu_obverse.jpg(class 3, shape boxy object in frame, 875px, paletted)
[process-photos]       obverse winner: nbu_big_obverse.png (kept_bg, final 875x1600) -> obverse_300.webp@300, obverse_600.webp@600, obverse_1200.webp@1200
[process-photos]     reverse ranking: nbu_big_reverse.png(class 3, shape boxy object in frame, 875px), nbu_reverse.jpg(class 3, shape boxy object in frame, 875px, paletted)
[process-photos]       reverse winner: nbu_big_reverse.png (kept_bg, final 875x1600) -> reverse_300.webp@300, reverse_600.webp@600, reverse_1200.webp@1200
[process-photos]     pair silhouette IoU: 1.0000
[process-photos]   (6/16) nbu:1566 'Ой у лузі червона калина'
[process-photos]     nbu_obverse.jpg: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     nbu_reverse.jpg: 1600x1600 shape=ok (solidity=0.998 extent=0.785 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     nbu_big_obverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     nbu_big_reverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.785 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_01.webp: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_02.webp: 1600x1600 shape=ok (solidity=0.998 extent=0.785 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     obverse ranking: nbu_big_obverse.png(class 1, 1600px), uacoins_01.webp(class 1, 1600px), nbu_obverse.jpg(class 1, 1600px, paletted)
[process-photos]       obverse winner: nbu_big_obverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> obverse_300.webp@300, obverse_600.webp@600, obverse_1200.webp@1200
[process-photos]     reverse ranking: nbu_big_reverse.png(class 1, 1600px), uacoins_02.webp(class 1, 1600px), nbu_reverse.jpg(class 1, 1600px, paletted)
[process-photos]       reverse winner: nbu_big_reverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> reverse_300.webp@300, reverse_600.webp@600, reverse_1200.webp@1200
[process-photos]     pair silhouette IoU: 0.9975
[process-photos]   (7/16) nbu:1588 'Сміливість бути. UA'
[process-photos]     nbu_obverse.jpg: 198x198 shape=ok (solidity=0.990 extent=0.794 aspect=1.000) -> bg class 2 (cuttable, cut)
[process-photos]     nbu_reverse.jpg: 198x198 shape=ok (solidity=0.990 extent=0.793 aspect=1.000) -> bg class 2 (cuttable, cut)
[process-photos]     nbu_big_obverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     nbu_big_reverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.785 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_01.webp: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_02.webp: 1600x1600 shape=ok (solidity=0.998 extent=0.785 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_03.webp: 2105x1489 shape=elongated object in frame (solidity=0.715 extent=0.777 aspect=0.438) -> bg class 3 (kept_bg, skip:odd_shape, outline-repaired)
[process-photos]     uacoins_04.webp: 2105x1489 shape=elongated object in frame (solidity=0.568 extent=0.677 aspect=0.421) -> bg class 2 (cuttable, cut, outline-repaired)
[process-photos]     unassigned by metadata: ['uacoins_03.webp', 'uacoins_04.webp']
[process-photos]     obverse ranking: nbu_big_obverse.png(class 1, 1600px), uacoins_01.webp(class 1, 1600px), nbu_obverse.jpg(class 2, 198px)
[process-photos]       obverse winner: nbu_big_obverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> obverse_300.webp@300, obverse_600.webp@600, obverse_1200.webp@1200
[process-photos]     reverse ranking: nbu_big_reverse.png(class 1, 1600px), uacoins_02.webp(class 1, 1600px), nbu_reverse.jpg(class 2, 198px)
[process-photos]       reverse winner: nbu_big_reverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> reverse_300.webp@300, reverse_600.webp@600, reverse_1200.webp@1200
[process-photos]     pair silhouette IoU: 0.9996
[process-photos]   (8/16) nbu:1589 'Сміливість бути. UA у сувенірній упаковці'
[process-photos]     nbu_obverse.jpg: 108x198 shape=boxy object in frame (solidity=1.000 extent=0.986 aspect=0.545) -> bg class 3 (kept_bg, skip:not_white_bg)
[process-photos]     nbu_reverse.jpg: 108x198 shape=boxy object in frame (solidity=0.993 extent=0.979 aspect=0.545) -> bg class 3 (kept_bg, skip:not_white_bg)
[process-photos]     nbu_big_obverse.png: 873x1600 shape=boxy object in frame (solidity=1.000 extent=0.996 aspect=0.545) -> bg class 3 (kept_bg, skip:not_white_bg)
[process-photos]     nbu_big_reverse.png: 873x1600 shape=boxy object in frame (solidity=1.000 extent=0.996 aspect=0.545) -> bg class 3 (kept_bg, skip:not_white_bg)
[process-photos]     obverse ranking: nbu_big_obverse.png(class 3, shape boxy object in frame, 873px), nbu_obverse.jpg(class 3, shape boxy object in frame, 108px)
[process-photos]       obverse winner: nbu_big_obverse.png (kept_bg, final 873x1600) -> obverse_300.webp@300, obverse_600.webp@600, obverse_1200.webp@1200
[process-photos]     reverse ranking: nbu_big_reverse.png(class 3, shape boxy object in frame, 873px), nbu_reverse.jpg(class 3, shape boxy object in frame, 108px)
[process-photos]       reverse winner: nbu_big_reverse.png (kept_bg, final 873x1600) -> reverse_300.webp@300, reverse_600.webp@600, reverse_1200.webp@1200
[process-photos]     pair silhouette IoU: 1.0000
[process-photos]   (9/16) nbu:1591 'Захисниці у сувенірній упаковці'
[process-photos]     nbu_obverse.jpg: 108x198 shape=boxy object in frame (solidity=0.978 extent=0.948 aspect=0.578) -> bg class 3 (kept_bg, skip:not_white_bg)
[process-photos]     nbu_reverse.jpg: 108x198 shape=scattered object in frame (solidity=0.567 extent=0.720 aspect=0.553) -> bg class 3 (kept_bg, skip:not_white_bg)
[process-photos]     nbu_big_obverse.png: 869x1600 shape=boxy object in frame (solidity=0.961 extent=0.933 aspect=0.554) -> bg class 3 (kept_bg, skip:not_white_bg)
[process-photos]     nbu_big_reverse.png: 869x1600 shape=scattered object in frame (solidity=0.583 extent=0.757 aspect=0.976) -> bg class 3 (kept_bg, skip:not_white_bg)
[process-photos]     obverse ranking: nbu_big_obverse.png(class 3, shape boxy object in frame, 869px), nbu_obverse.jpg(class 3, shape boxy object in frame, 108px)
[process-photos]       obverse winner: nbu_big_obverse.png (kept_bg, final 869x1600) -> obverse_300.webp@300, obverse_600.webp@600, obverse_1200.webp@1200
[process-photos]     reverse ranking: nbu_big_reverse.png(class 3, shape scattered object in frame, 869px), nbu_reverse.jpg(class 3, shape scattered object in frame, 108px)
[process-photos]       reverse winner: nbu_big_reverse.png (kept_bg, final 869x1600) -> reverse_300.webp@300, reverse_600.webp@600, reverse_1200.webp@1200
[process-photos]     pair silhouette IoU: 1.0000
[process-photos]   (10/16) nbu:1592 'Країна супергероїв. Дякуємо волонтерам!'
[process-photos]     nbu_obverse.jpg: 198x198 shape=ok (solidity=0.990 extent=0.793 aspect=1.000) -> bg class 2 (cuttable, cut)
[process-photos]     nbu_reverse.jpg: 198x198 shape=ok (solidity=0.990 extent=0.793 aspect=1.000) -> bg class 2 (cuttable, cut)
[process-photos]     nbu_big_obverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.783 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     nbu_big_reverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.783 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_01.webp: 1600x1600 shape=ok (solidity=0.998 extent=0.783 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_02.webp: 1600x1600 shape=ok (solidity=0.998 extent=0.783 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_03.webp: 2105x1489 shape=scattered object in frame (solidity=0.737 extent=0.830 aspect=0.799) -> bg class 3 (kept_bg, skip:odd_shape, outline-repaired)
[process-photos]     uacoins_04.webp: 2105x1489 shape=scattered object in frame (solidity=0.725 extent=0.517 aspect=0.668) -> bg class 2 (cuttable, cut, outline-repaired)
[process-photos]     unassigned by metadata: ['uacoins_03.webp', 'uacoins_04.webp']
[process-photos]     obverse ranking: nbu_big_obverse.png(class 1, 1600px), uacoins_01.webp(class 1, 1600px), nbu_obverse.jpg(class 2, 198px)
[process-photos]       obverse winner: nbu_big_obverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> obverse_300.webp@300, obverse_600.webp@600, obverse_1200.webp@1200
[process-photos]     reverse ranking: nbu_big_reverse.png(class 1, 1600px), uacoins_02.webp(class 1, 1600px), nbu_reverse.jpg(class 2, 198px)
[process-photos]       reverse winner: nbu_big_reverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> reverse_300.webp@300, reverse_600.webp@600, reverse_1200.webp@1200
[process-photos]     pair silhouette IoU: 0.9993
[process-photos]   (11/16) nbu:1600 'Країна супергероїв. Дякуємо енергетикам!'
[process-photos]     nbu_obverse.jpg: 198x198 shape=ok (solidity=0.990 extent=0.793 aspect=1.000) -> bg class 2 (cuttable, cut)
[process-photos]     nbu_reverse.jpg: 198x198 shape=ok (solidity=0.990 extent=0.793 aspect=1.000) -> bg class 2 (cuttable, cut)
[process-photos]     nbu_big_obverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.785 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     nbu_big_reverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_01.webp: 1600x1600 shape=ok (solidity=0.998 extent=0.785 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_02.webp: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_03.webp: 2105x1489 shape=elongated object in frame (solidity=0.747 extent=0.815 aspect=0.380) -> bg class 3 (kept_bg, skip:odd_shape, outline-repaired)
[process-photos]     uacoins_04.webp: 2105x1489 shape=elongated object in frame (solidity=0.584 extent=0.580 aspect=0.242) -> bg class 2 (cuttable, cut, outline-repaired)
[process-photos]     unassigned by metadata: ['uacoins_03.webp', 'uacoins_04.webp']
[process-photos]     obverse ranking: nbu_big_obverse.png(class 1, 1600px), uacoins_01.webp(class 1, 1600px), nbu_obverse.jpg(class 2, 198px)
[process-photos]       obverse winner: nbu_big_obverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> obverse_300.webp@300, obverse_600.webp@600, obverse_1200.webp@1200
[process-photos]     reverse ranking: nbu_big_reverse.png(class 1, 1600px), uacoins_02.webp(class 1, 1600px), nbu_reverse.jpg(class 2, 198px)
[process-photos]       reverse winner: nbu_big_reverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> reverse_300.webp@300, reverse_600.webp@600, reverse_1200.webp@1200
[process-photos]     pair silhouette IoU: 0.9990
[process-photos]   (12/16) nbu:1617 'Захисниці'
[process-photos]     nbu_obverse.jpg: 198x198 shape=ok (solidity=0.990 extent=0.792 aspect=1.000) -> bg class 2 (cuttable, cut)
[process-photos]     nbu_reverse.jpg: 198x198 shape=ok (solidity=0.990 extent=0.793 aspect=1.000) -> bg class 2 (cuttable, cut)
[process-photos]     nbu_big_obverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=0.999) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     nbu_big_reverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.783 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_01.webp: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=0.999) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_02.webp: 1600x1600 shape=ok (solidity=0.998 extent=0.783 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_03.webp: 2105x1489 shape=boxy object in frame (solidity=0.739 extent=0.856 aspect=0.387) -> bg class 2 (cuttable, cut, outline-repaired)
[process-photos]     uacoins_04.webp: 2105x1489 shape=scattered object in frame (solidity=0.776 extent=0.614 aspect=0.614) -> bg class 3 (kept_bg, skip:odd_shape, outline-repaired)
[process-photos]     unassigned by metadata: ['uacoins_03.webp', 'uacoins_04.webp']
[process-photos]     obverse ranking: nbu_big_obverse.png(class 1, 1600px), uacoins_01.webp(class 1, 1600px), nbu_obverse.jpg(class 2, 198px)
[process-photos]       obverse winner: nbu_big_obverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> obverse_300.webp@300, obverse_600.webp@600, obverse_1200.webp@1200
[process-photos]     reverse ranking: nbu_big_reverse.png(class 1, 1600px), uacoins_02.webp(class 1, 1600px), nbu_reverse.jpg(class 2, 198px)
[process-photos]       reverse winner: nbu_big_reverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> reverse_300.webp@300, reverse_600.webp@600, reverse_1200.webp@1200
[process-photos]     pair silhouette IoU: 1.0000
[process-photos]   (13/16) nbu:1619 'Країна супергероїв. Дякуємо залізничникам!'
[process-photos]     nbu_obverse.jpg: 198x198 shape=ok (solidity=0.990 extent=0.793 aspect=1.000) -> bg class 2 (cuttable, cut)
[process-photos]     nbu_reverse.jpg: 198x198 shape=ok (solidity=0.990 extent=0.792 aspect=1.000) -> bg class 2 (cuttable, cut)
[process-photos]     nbu_big_obverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     nbu_big_reverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.783 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_01.webp: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_02.webp: 1600x1600 shape=ok (solidity=0.998 extent=0.783 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_03.webp: 2105x1489 shape=elongated object in frame (solidity=0.699 extent=0.822 aspect=0.277) -> bg class 3 (kept_bg, skip:odd_shape, outline-repaired)
[process-photos]     uacoins_04.webp: 2105x1489 shape=scattered object in frame (solidity=0.787 extent=0.592 aspect=0.712) -> bg class 2 (cuttable, cut, outline-repaired)
[process-photos]     unassigned by metadata: ['uacoins_03.webp', 'uacoins_04.webp']
[process-photos]     obverse ranking: nbu_big_obverse.png(class 1, 1600px), uacoins_01.webp(class 1, 1600px), nbu_obverse.jpg(class 2, 198px)
[process-photos]       obverse winner: nbu_big_obverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> obverse_300.webp@300, obverse_600.webp@600, obverse_1200.webp@1200
[process-photos]     reverse ranking: nbu_big_reverse.png(class 1, 1600px), uacoins_02.webp(class 1, 1600px), nbu_reverse.jpg(class 2, 198px)
[process-photos]       reverse winner: nbu_big_reverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> reverse_300.webp@300, reverse_600.webp@600, reverse_1200.webp@1200
[process-photos]     pair silhouette IoU: 0.9989
[process-photos]   (14/16) nbu:1622 'Набір із двох срібних монет “Дружба та братство - найбільше багатство” у футлярі'
[process-photos]     nbu_obverse.jpg: 198x188 shape=ok (solidity=0.986 extent=0.781 aspect=0.949) -> bg class 2 (cuttable, cut, outline-repaired)
[process-photos]     nbu_reverse.jpg: 198x188 shape=ok (solidity=0.987 extent=0.781 aspect=0.949) -> bg class 2 (cuttable, cut, outline-repaired)
[process-photos]     nbu_big_obverse.png: 1600x1515 shape=ok (solidity=0.998 extent=0.771 aspect=0.528) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     nbu_big_reverse.png: 1600x1519 shape=ok (solidity=0.998 extent=0.771 aspect=0.523) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_01.webp: 1600x1515 shape=ok (solidity=0.998 extent=0.771 aspect=0.528) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_02.webp: 1600x1519 shape=ok (solidity=0.998 extent=0.771 aspect=0.523) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_03.webp: 2105x1489 shape=elongated object in frame (solidity=0.702 extent=0.834 aspect=0.311) -> bg class 3 (kept_bg, skip:odd_shape, outline-repaired)
[process-photos]     uacoins_04.webp: 2105x1489 shape=scattered object in frame (solidity=0.789 extent=0.619 aspect=0.729) -> bg class 3 (kept_bg, skip:odd_shape, outline-repaired)
[process-photos]     unassigned by metadata: ['uacoins_03.webp', 'uacoins_04.webp']
[process-photos]     obverse ranking: nbu_big_obverse.png(class 1, 1515px), uacoins_01.webp(class 1, 1515px), nbu_obverse.jpg(class 2, 188px)
[process-photos]       obverse winner: nbu_big_obverse.png (already_transparent, final 1600x1515) -> obverse_300.webp@300, obverse_600.webp@600, obverse_1200.webp@1200
[process-photos]     reverse ranking: nbu_big_reverse.png(class 1, 1519px), uacoins_02.webp(class 1, 1519px), nbu_reverse.jpg(class 2, 188px)
[process-photos]       reverse winner: nbu_big_reverse.png (already_transparent, final 1600x1519) -> reverse_300.webp@300, reverse_600.webp@600, reverse_1200.webp@1200
[process-photos]     pair silhouette IoU: 0.9871
[process-photos]   (15/16) nbu:1643 'Країна супергероїв. Дякуємо медикам!'
[process-photos]     nbu_obverse.jpg: 198x198 shape=ok (solidity=0.990 extent=0.793 aspect=1.000) -> bg class 2 (cuttable, cut)
[process-photos]     nbu_reverse.jpg: 198x198 shape=ok (solidity=0.990 extent=0.793 aspect=1.000) -> bg class 2 (cuttable, cut)
[process-photos]     nbu_big_obverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     nbu_big_reverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.785 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_01.webp: 1600x1600 shape=ok (solidity=0.998 extent=0.784 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_02.webp: 1600x1600 shape=ok (solidity=0.998 extent=0.785 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     uacoins_03.webp: 2105x1489 shape=elongated object in frame (solidity=0.720 extent=0.823 aspect=0.353) -> bg class 3 (kept_bg, skip:odd_shape, outline-repaired)
[process-photos]     uacoins_04.webp: 2105x1489 shape=ok (solidity=0.904 extent=0.676 aspect=0.747) -> bg class 3 (kept_bg, skip:odd_shape, outline-repaired)
[process-photos]     unassigned by metadata: ['uacoins_03.webp', 'uacoins_04.webp']
[process-photos]     obverse ranking: nbu_big_obverse.png(class 1, 1600px), uacoins_01.webp(class 1, 1600px), nbu_obverse.jpg(class 2, 198px)
[process-photos]       obverse winner: nbu_big_obverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> obverse_300.webp@300, obverse_600.webp@600, obverse_1200.webp@1200
[process-photos]     reverse ranking: nbu_big_reverse.png(class 1, 1600px), uacoins_02.webp(class 1, 1600px), nbu_reverse.jpg(class 2, 198px)
[process-photos]       reverse winner: nbu_big_reverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> reverse_300.webp@300, reverse_600.webp@600, reverse_1200.webp@1200
[process-photos]     pair silhouette IoU: 0.9995
[process-photos]   (16/16) nbu:1718 '"Країна супергероїв. Дякуємо зброярам!" (н) у сувенірному пакованні'
[process-photos]     nbu_obverse.jpg: 198x198 shape=ok (solidity=0.990 extent=0.795 aspect=1.000) -> bg class 2 (cuttable, cut)
[process-photos]     nbu_reverse.jpg: 198x198 shape=ok (solidity=0.990 extent=0.795 aspect=1.000) -> bg class 2 (cuttable, cut)
[process-photos]     nbu_big_obverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.785 aspect=1.000) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     nbu_big_reverse.png: 1600x1600 shape=ok (solidity=0.998 extent=0.785 aspect=0.999) -> bg class 1 (already_transparent, skip:already_transparent)
[process-photos]     obverse ranking: nbu_big_obverse.png(class 1, 1600px), nbu_obverse.jpg(class 2, 198px)
[process-photos]       obverse winner: nbu_big_obverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> obverse_300.webp@300, obverse_600.webp@600, obverse_1200.webp@1200
[process-photos]     reverse ranking: nbu_big_reverse.png(class 1, 1600px), nbu_reverse.jpg(class 2, 198px)
[process-photos]       reverse winner: nbu_big_reverse.png (already_transparent, final 1600x1600, outline_dip=0.000) -> reverse_300.webp@300, reverse_600.webp@600, reverse_1200.webp@1200
[process-photos]     pair silhouette IoU: 0.9993
[process-photos] series: Безсмертна моя Україно
[process-photos]   nbu:1560 | obverse: nbu 1600x1600 already_transparent | reverse: nbu 1600x1600 already_transparent | pair IoU 0.997
[process-photos]   nbu:1561 | obverse: nbu 880x1600 kept_bg shape:boxy object in frame | reverse: nbu 846x1600 kept_bg shape:elongated object in frame | pair IoU 1.000
[process-photos]     ANOMALY: nbu:1561 obverse: photo kept with background (nbu_big_obverse.png)
[process-photos]     ANOMALY: nbu:1561 reverse: photo kept with background (nbu_big_reverse.png)
[process-photos]   nbu:1562 | obverse: nbu 1600x1600 already_transparent | reverse: nbu 1600x1600 already_transparent | pair IoU 0.997
[process-photos]   nbu:1564 | obverse: nbu 1600x1600 already_transparent | reverse: nbu 1600x1600 already_transparent | pair IoU 0.999
[process-photos]   nbu:1565 | obverse: nbu 875x1600 kept_bg shape:boxy object in frame | reverse: nbu 875x1600 kept_bg shape:boxy object in frame | pair IoU 1.000
[process-photos]     ANOMALY: nbu:1565 obverse: photo kept with background (nbu_big_obverse.png)
[process-photos]     ANOMALY: nbu:1565 reverse: photo kept with background (nbu_big_reverse.png)
[process-photos]   nbu:1566 | obverse: nbu 1600x1600 already_transparent | reverse: nbu 1600x1600 already_transparent | pair IoU 0.998
[process-photos]   nbu:1588 | obverse: nbu 1600x1600 already_transparent | reverse: nbu 1600x1600 already_transparent | pair IoU 1.000
[process-photos]   nbu:1589 | obverse: nbu 873x1600 kept_bg shape:boxy object in frame | reverse: nbu 873x1600 kept_bg shape:boxy object in frame | pair IoU 1.000
[process-photos]     ANOMALY: nbu:1589 obverse: photo kept with background (nbu_big_obverse.png)
[process-photos]     ANOMALY: nbu:1589 reverse: photo kept with background (nbu_big_reverse.png)
[process-photos]   nbu:1591 | obverse: nbu 869x1600 kept_bg shape:boxy object in frame | reverse: nbu 869x1600 kept_bg shape:scattered object in frame | pair IoU 1.000
[process-photos]     ANOMALY: nbu:1591 obverse: photo kept with background (nbu_big_obverse.png)
[process-photos]     ANOMALY: nbu:1591 reverse: photo kept with background (nbu_big_reverse.png)
[process-photos]   nbu:1592 | obverse: nbu 1600x1600 already_transparent | reverse: nbu 1600x1600 already_transparent | pair IoU 0.999
[process-photos]   nbu:1600 | obverse: nbu 1600x1600 already_transparent | reverse: nbu 1600x1600 already_transparent | pair IoU 0.999
[process-photos]   nbu:1617 | obverse: nbu 1600x1600 already_transparent | reverse: nbu 1600x1600 already_transparent | pair IoU 1.000
[process-photos]   nbu:1619 | obverse: nbu 1600x1600 already_transparent | reverse: nbu 1600x1600 already_transparent | pair IoU 0.999
[process-photos]   nbu:1622 | obverse: nbu 1600x1515 already_transparent | reverse: nbu 1600x1519 already_transparent | pair IoU 0.987
[process-photos]   nbu:1643 | obverse: nbu 1600x1600 already_transparent | reverse: nbu 1600x1600 already_transparent | pair IoU 1.000
[process-photos]   nbu:1718 | obverse: nbu 1600x1600 already_transparent | reverse: nbu 1600x1600 already_transparent | pair IoU 0.999
[process-photos] complete pairs 16/16, anomalies: 8, disqualified: 0, rejected candidates (total): 0
```
